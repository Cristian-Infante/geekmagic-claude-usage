#!/usr/bin/env python3
"""System-tray app: keeps a GeekMagic SmallTV updated and lets you flip between Claude and Codex usage.

Windows/Linux: left-click the tray icon to toggle, right-click for the menu.
macOS: clicking the menu-bar icon opens the menu; pick Claude or Codex there.

    python tray.py --ip 192.168.1.77                    # run it
    python tray.py --ip 192.168.1.77 --install-startup  # run it at every login
    python tray.py --uninstall-startup
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import autostart
import geekmagic_claude as g

LOG_PATH = Path(__file__).with_name("tray.log")
STATE_PATH = Path(__file__).with_name("tray_state.json")  # remembered image slots, see App._load_state
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")],
)
log = logging.getLogger("tray")

SETTLE_SECONDS = 5  # quiet time after a click before the slow fetch + upload starts
STALE_AFTER = 30  # a switched-to image uploaded more recently than this isn't refreshed again
RETRY_SECONDS = 10  # how often to retry while the device is unreachable


def slot_file(provider: str, slot: str) -> str:
    return f"{provider}-usage-{slot}.gif"


TITLES = {"claude": "Claude", "codex": "Codex"}


def make_icon(provider: str) -> Image.Image:
    """Mascot on a transparent background, tinted per provider so you can tell them apart."""
    theme = g.THEMES[TITLES[provider]]
    image = Image.new("RGBA", (42, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(theme["bitmap"]):
        for col, cell in enumerate(line):
            if cell != "0":
                fill = theme["body"] if cell == "1" else "#0F0D0B"
                draw.rectangle((col * 3, row * 3, col * 3 + 2, row * 3 + 2), fill=fill)
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.paste(image.resize((60, 34), Image.NEAREST), (2, 15))
    return canvas


class App:
    def __init__(self, ip: str, interval: int, animation: str, provider: str = "claude") -> None:
        self.ip = ip
        self.interval = interval
        self.animation = animation
        self.provider = provider
        self.slot: dict[str, str] = self._load_state()  # provider -> "a"/"b", the complete image on the device
        self.cancel = threading.Event()
        self.last_upload: dict[str, float] = {}
        self.last_click = 0.0
        self.offline = False  # the device didn't answer on the last attempt
        self.push_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.icon = pystray.Icon(
            "geekmagic-usage", make_icon(self.provider), self._tooltip(),
            menu=pystray.Menu(
                pystray.MenuItem("Alternar Claude / Codex", self.toggle, default=True, visible=False),
                pystray.MenuItem("Claude", lambda: self.select("claude"), checked=lambda _: self.provider == "claude", radio=True),
                pystray.MenuItem("Codex", lambda: self.select("codex"), checked=lambda _: self.provider == "codex", radio=True),
                pystray.MenuItem("Actualizar ahora", lambda: self.wake.set()),
                pystray.MenuItem("Ver logs", self.open_logs),
                pystray.MenuItem("Salir", self.quit),
            ),
        )

    def _tooltip(self) -> str:
        return f"GeekMagic: mostrando {TITLES[self.provider]}"

    def select(self, provider: str) -> None:
        log.info("switch -> %s", provider)
        self.provider = provider
        self.icon.icon = make_icon(provider)
        self.icon.title = self._tooltip()
        self.icon.update_menu()
        self.last_click = time.monotonic()
        self.cancel.set()  # abort any upload in flight: the device serves one request at a time
        threading.Thread(target=self._show_uploaded, args=(provider,), daemon=True).start()
        if time.monotonic() - self.last_upload.get(provider, 0) > STALE_AFTER:
            self.wake.set()  # refresh the image we just switched to, once clicking settles

    def _upload(self, provider: str, usage: dict) -> bool:
        """Upload fresh data into the provider's spare slot, then pin it if it's still the active one.

        Each provider alternates between two files, so an upload cut short by a click can only
        damage the spare slot; the slot on screen (and the one a switch will show) stays intact.
        """
        self.cancel = cancel = threading.Event()
        with self.push_lock:
            spare = "b" if self.slot.get(provider) == "a" else "a"
            log.info("uploading %s -> %s", provider, slot_file(provider, spare))
            try:
                g.push_usage(self.ip, usage, self.animation, slot_file(provider, spare), show=False, cancel=cancel)
            except g.UploadCancelled:
                log.info("upload cancelled (%s)", provider)
                self.wake.set()  # retry once the clicking settles
                return False
            self.slot[provider] = spare
            self._save_state()
            self.last_upload[provider] = time.monotonic()
            if provider == self.provider:  # checked after the slow upload, in case the user switched meanwhile
                g.show_image(self.ip, slot_file(provider, spare))
            return True

    def _load_state(self) -> dict[str, str]:
        """Which slot holds each provider's complete image. The device keeps its files across restarts of
        this app (and of itself), so remembering this makes the very first clicks instant too."""
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            # Also remember each provider's recent animations, so a restart can't bring one back too soon.
            for title, names in saved.get("animations", {}).items():
                names = [names] if isinstance(names, str) else names  # older files stored just the last one
                g._recent_animations[title] = [n for n in names if n in g.ANIMATIONS][-g.RECENT_ANIMATIONS:]
            slots = saved.get("slots", saved)  # older files were just the slot map
            return {p: s for p, s in slots.items() if p in TITLES and s in ("a", "b")}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_state(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps({"slots": self.slot, "animations": g._recent_animations}), encoding="utf-8")
        except OSError:
            log.warning("could not save %s", STATE_PATH)

    def _sync_with_device(self) -> None:
        """Forget remembered images the device no longer has, and drop leftovers of the old single-file scheme."""
        try:
            files = g.list_images(self.ip)
            for provider, slot in list(self.slot.items()):
                if slot_file(provider, slot) not in files:
                    log.info("%s image is gone from the device, will re-upload", provider)
                    del self.slot[provider]
            for legacy in g.IMAGE_NAMES.values():
                if legacy in files:
                    g.delete_image(self.ip, legacy)
            self._save_state()
        except OSError:
            pass  # device unreachable right now; keep what we remember

    def _show_uploaded(self, provider: str) -> None:
        """Instant switch: the provider's last image is already on the device, so just pin it."""
        slot = self.slot.get(provider)
        if slot is None:
            log.info("no %s image on the device yet; it appears after its first upload", provider)
            return
        try:
            with self.push_lock:
                if provider == self.provider:
                    g.show_image(self.ip, slot_file(provider, slot))
                    log.info("showed %s", provider)
        except Exception:
            log.exception("show failed (%s)", provider)

    @staticmethod
    def open_logs() -> None:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(LOG_PATH)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(LOG_PATH)])  # in the default text editor
        else:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])

    def toggle(self) -> None:
        self.select("codex" if self.provider == "claude" else "claude")

    def quit(self) -> None:
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    def worker(self) -> None:
        pending: tuple[str, dict, float] | None = None  # (provider, usage, fetched at) awaiting delivery
        self._sync_with_device()
        self._show_uploaded(self.provider)  # remembered image up right away, before the first (slow) refresh
        while not self.stop.is_set():
            self.wake.clear()
            # An upload keeps the device busy for seconds; wait out rapid toggling so a click never queues behind it.
            while not self.stop.is_set() and time.monotonic() - self.last_click < SETTLE_SECONDS:
                time.sleep(0.5)
            provider = self.provider
            wait = self.interval
            usage = None
            try:
                if pending and pending[0] == provider and time.monotonic() - pending[2] < self.interval:
                    usage = pending[1]  # fetched moments ago but not delivered yet: don't query again
                else:
                    usage = g.PROVIDERS[provider]()
            except Exception as e:  # keep the tray alive no matter what
                log.exception("fetch failed (%s)", provider)
                self.icon.title = f"GeekMagic: {e}"[:120]
            pending = None
            if usage is not None:
                try:
                    if self._upload(provider, usage):
                        log.info("pushed %s", provider)
                        if self.offline:
                            log.info("device is back")
                            self.offline = False
                        self.icon.title = self._tooltip()
                    else:  # an upload cancelled by a click: retry with the same data once things settle
                        pending = (provider, usage, time.monotonic())
                except OSError as e:  # the device is off or unreachable: retry soon, without re-querying usage
                    if not self.offline:
                        log.warning("device unreachable (%s), retrying every %ss", e, RETRY_SECONDS)
                        self.offline = True
                    self.icon.title = "GeekMagic: sin conexion con la pantalla, reintentando"
                    pending = (provider, usage, time.monotonic())
                    wait = RETRY_SECONDS
                except Exception as e:
                    log.exception("upload failed (%s)", provider)
                    self.icon.title = f"GeekMagic: {e}"[:120]
            other = "codex" if provider == "claude" else "claude"
            if other not in self.slot and not self.offline:  # prefetch the other provider so the first click is instant too
                try:
                    self._upload(other, g.PROVIDERS[other]())
                except Exception:
                    log.exception("warm-up failed (%s)", other)
            self.wake.wait(wait)

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        self.icon.run()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", help="GeekMagic device IP (required unless --uninstall-startup)")
    parser.add_argument("--interval", type=int, default=30, metavar="SECONDS")
    parser.add_argument("--provider", default="claude", choices=list(g.PROVIDERS))
    parser.add_argument("--animation", default="auto", choices=[*g.ANIMATIONS, "auto", "random"])
    parser.add_argument("--install-startup", action="store_true", help="start this app automatically at login, then exit")
    parser.add_argument("--uninstall-startup", action="store_true", help="remove the automatic start, then exit")
    args = parser.parse_args()

    if args.uninstall_startup:
        print(autostart.uninstall())
        return
    if not args.ip:
        parser.error("--ip is required")
    if args.install_startup:
        print(autostart.install([
            "--ip", args.ip, "--interval", str(args.interval),
            "--provider", args.provider, "--animation", args.animation,
        ]))
        return
    App(args.ip, args.interval, args.animation, args.provider).run()


if __name__ == "__main__":
    main()
