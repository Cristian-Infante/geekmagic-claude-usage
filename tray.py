#!/usr/bin/env python3
"""System-tray app: keeps a GeekMagic SmallTV updated and lets you flip between Claude and Codex usage.

Windows/Linux: left-click the tray icon to toggle, right-click for the menu.
macOS: clicking the menu-bar icon opens the menu; pick Claude or Codex there.

    python tray.py                                      # finds the screen on your network by itself
    python tray.py --ip 192.168.1.77                    # or tell it where the screen is
    python tray.py --install-startup                    # run it at every login
    python tray.py --uninstall-startup

Besides drawing the screen it sends a desktop notification when either provider's usage reaches 80 % / 95 %
(and when the limit resets), and it dims the screen if the numbers can't be refreshed for a few minutes. The
menu also has a split view and a stats view (both providers at once), pause (also while the computer is locked),
and brightness / night mode / dim-when-locked for the device's backlight.
"""

from __future__ import annotations

import argparse
import hashlib
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

import alerts
import autostart
import discover
import geekmagic_claude as g
import pace
import session_lock
import usage_stats

# Identifies the code that draws the screens. The device keeps the images it was sent, so after an update (new
# labels, colours, animations...) what's stored is out of date; a changed id makes the app re-upload every view once.
RENDER_ID = hashlib.sha1(b"".join(
    Path(module.__file__).read_bytes() for module in (g, pace, alerts, usage_stats)
)).hexdigest()[:12]

LOG_PATH = Path(__file__).with_name("tray.log")
STATE_PATH = Path(__file__).with_name("tray_state.json")  # remembered between runs, see App._load_state
log = logging.getLogger("tray")


def setup_logging() -> None:
    """Log to tray.log (rotated). Done from main() so importing this module, e.g. from tests, writes nothing."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")],
    )

SETTLE_SECONDS = 5  # quiet time after a click before the slow fetch + upload starts
STALE_AFTER = 30  # a switched-to image uploaded more recently than this isn't refreshed again
RETRY_SECONDS = 10  # how often to retry while the device is unreachable
STALE_SECONDS = 180  # numbers that couldn't be refreshed for this long get the dimmed "no fresh data" image
OTHER_REFRESH = 120  # how often the provider that isn't on screen is queried (for its alerts and fresh image)
REDISCOVER_AFTER = 60  # unreachable for this long -> look for the device on the network (it may have a new IP)
REDISCOVER_EVERY = 120  # ...and then at most this often
DISCOVER_RETRY = 30  # while there's no address at all, how often to scan
LOCK_POLL = 3  # seconds between checks of whether the computer got locked / unlocked
PAUSE_POLL = 5  # while paused, how often the worker looks again
LOCAL_STATS_EVERY = 600  # Codex's local activity counts are refreshed at most this often (seconds)
HISTORY_MAX = 150  # readings kept per usage window, for the recent-pace estimate
HISTORY_EVERY = 300  # ...adding one at least this often (seconds), or whenever the percentage changed
DEFAULT_BRIGHTNESS_CHOICES = (100, 75, 50, 25, 10, 0)  # the menu's brightness levels (the device takes -10..100)
LOCK_DIM_LEVEL = 0  # backlight level while the computer is locked, if "dim when locked" is on
NIGHT_DEFAULT = {"enabled": False, "start": 22, "end": 7, "level": 10}  # 10 PM - 7 AM at 10 %


def slot_file(provider: str, slot: str) -> str:
    return f"{provider}-usage-{slot}.gif"


TITLES = {"claude": "Claude", "codex": "Codex"}
SPLIT = "split"  # key of the third view (both providers on one screen); it has its own image slots on the device
STATS = "stats"  # the fourth: activity numbers for both providers
PANEL_PUSH = {SPLIT: "push_split", STATS: "push_stats"}  # views of both providers -> the geekmagic_claude function that uploads them
WINDOWS = (("current", "sesión"), ("weekly", "semana"))  # usage window keys -> how notifications name them


def _mascot_image(provider: str) -> Image.Image:
    theme = g.THEMES[TITLES[provider]]
    image = Image.new("RGBA", (42, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(theme["bitmap"]):
        for col, cell in enumerate(line):
            if cell != "0":
                fill = theme["body"] if cell == "1" else "#0F0D0B"
                draw.rectangle((col * 3, row * 3, col * 3 + 2, row * 3 + 2), fill=fill)
    return image


def make_icon(provider: str) -> Image.Image:
    """Mascot on a transparent background, tinted per provider so you can tell them apart."""
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.paste(_mascot_image(provider).resize((60, 34), Image.NEAREST), (2, 15))
    return canvas


def make_split_icon() -> Image.Image:
    """Both mascots stacked: the icon shown while the split view is on."""
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.paste(_mascot_image("claude"), (11, 4))
    canvas.paste(_mascot_image("codex"), (11, 36))
    return canvas


def make_stats_icon() -> Image.Image:
    """Three bars in the providers' colours: the icon shown while the stats view is on."""
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for i, (height, color) in enumerate(((26, g.THEMES["Claude"]["body"]), (40, g.THEMES["Codex"]["body"]), (18, g.THEMES["Codex"]["weekly"]))):
        x = 10 + i * 17
        draw.rectangle((x, 54 - height, x + 12, 54), fill=color)
    return canvas


def _icon_for(key: str) -> Image.Image:
    return {SPLIT: make_split_icon, STATS: make_stats_icon}.get(key, lambda: make_icon(key))()


def _hour12(hour: int) -> str:
    """22 -> "10 PM"."""
    return f"{hour % 12 or 12} {'AM' if hour % 24 < 12 else 'PM'}"


class App:
    def __init__(self, ip: str | None, interval: int, animation: str, provider: str | None = None,
                 night_hours: tuple[int, int] | None = None, night_level: int | None = None) -> None:
        self.interval = interval
        self.animation = animation
        self.saved_ip: str | None = None  # last address the device answered on, see _load_state
        self.saved_view: str | None = None  # what was on screen last time: "claude", "codex" or SPLIT
        self.saved_provider: str | None = None  # the last single provider (what the split view returns to)
        self.alerts: dict[str, dict] = {}  # "provider/window" -> {"level": 0..2, "pct": last reading}
        self.notify_enabled = True
        self.history: dict[str, list] = {}  # "provider/window" -> [[epoch, pct, reset epoch], ...] for the recent pace
        self.pause_on_lock = True  # stop querying and uploading while the computer is locked
        self.dim_on_lock = False  # ...and also turn the screen's backlight down
        self.brightness: int | None = None  # the day backlight level we last set; the device can't be asked
        self.night: dict = dict(NIGHT_DEFAULT)  # the device's own night schedule, see set_night_mode
        self.paused = False  # manual pause (not remembered: a forgotten pause would be a nasty surprise)
        self.locked = False
        self.outdated: set[str] = set()  # views whose stored image was drawn by an older version of the code
        self.slot: dict[str, str] = self._load_state()  # provider -> "a"/"b", the complete image on the device
        if night_hours:  # --night START-END / --night-brightness: the schedule the menu's night mode will use
            self.night["start"], self.night["end"] = night_hours
        if night_level is not None:
            self.night["level"] = night_level
        # The view comes back exactly as it was left; `provider` (--provider) is only the very first run's choice.
        view = self.saved_view or provider or "claude"
        # showing a view of both providers instead of one (only the menu turns these on): SPLIT, STATS or None
        self.mode: str | None = view if view in PANEL_PUSH else None
        # the last single-provider view, also what the left-click toggles from
        self.provider = (self.saved_provider or provider or "claude") if self.mode else view
        self.local_stats: dict | None = None  # Codex's activity counts from its local logs, see _update_panels
        self.local_stats_at = 0.0
        self.ip = ip or self.saved_ip or ""  # "" until the device is found
        self.cancel = threading.Event()
        self.last_upload: dict[str, float] = {}
        self.last_fetch: dict[str, float] = {}
        self.last_good: dict[str, tuple[dict, float]] = {}  # provider -> (last usage that was read OK, when)
        self.stale: set[str] = set()  # providers whose image on the device is the dimmed "no fresh data" one
        self.last_click = 0.0
        self.last_scan = 0.0
        self.offline = False  # the device didn't answer on the last attempt
        self.offline_since: float | None = None
        self.push_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.icon = pystray.Icon(
            "geekmagic-usage", _icon_for(self._active_key()), self._tooltip(),
            menu=pystray.Menu(
                pystray.MenuItem("Alternar Claude / Codex", self.toggle, default=True, visible=False),
                pystray.MenuItem("Claude", lambda: self.select("claude"), checked=lambda _: self.mode is None and self.provider == "claude", radio=True),
                pystray.MenuItem("Codex", lambda: self.select("codex"), checked=lambda _: self.mode is None and self.provider == "codex", radio=True),
                pystray.MenuItem("Vista dividida", self.toggle_split, checked=lambda _: self.split),
                pystray.MenuItem("Estadísticas", self.toggle_stats, checked=lambda _: self.mode == STATS),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Actualizar ahora", lambda: self.wake.set()),
                pystray.MenuItem("Pausar", self.toggle_pause, checked=lambda _: self.paused),
                pystray.MenuItem("Pantalla", pystray.Menu(
                    *(pystray.MenuItem(f"Brillo {level} %", self._brightness_action(level),
                                       checked=lambda _, level=level: self.brightness == level, radio=True)
                      for level in DEFAULT_BRIGHTNESS_CHOICES),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(lambda _: self._night_label(), self.toggle_night, checked=lambda _: self.night["enabled"]),
                    pystray.MenuItem("Atenuar al bloquear el PC", self.toggle_dim_on_lock, checked=lambda _: self.dim_on_lock),
                )),
                pystray.MenuItem("Opciones", pystray.Menu(
                    pystray.MenuItem("Notificaciones", self.toggle_notifications, checked=lambda _: self.notify_enabled),
                    pystray.MenuItem("Pausar al bloquear el PC", self.toggle_pause_on_lock, checked=lambda _: self.pause_on_lock),
                )),
                pystray.MenuItem("Ver logs", self.open_logs),
                pystray.MenuItem("Salir", self.quit),
            ),
        )

    # The view-mode flag the split view was built on, kept as a property so `app.split` still reads and writes naturally.
    @property
    def split(self) -> bool:
        return self.mode == SPLIT

    @split.setter
    def split(self, value: bool) -> None:
        self.mode = SPLIT if value else (None if self.mode == SPLIT else self.mode)

    def _active_key(self) -> str:
        """What the screen should be showing: a provider, SPLIT or STATS."""
        return self.mode or self.provider

    def _tooltip(self) -> str:
        if self.mode == SPLIT:
            view = "vista dividida"
        elif self.mode == STATS:
            view = "estadísticas"
        else:
            view = f"mostrando {TITLES[self.provider]}"
        if self.paused:
            return f"GeekMagic: {view} (en pausa)"
        if self.pause_on_lock and self.locked:
            return f"GeekMagic: {view} (en pausa: PC bloqueado)"
        return f"GeekMagic: {view}"

    # --- remembered state ---------------------------------------------------------------------------

    def _load_state(self) -> dict[str, str]:
        """Restore what survives a restart: which slot holds each provider's complete image (the device keeps
        its files, so the very first clicks are instant), recent animations (so one can't come back too soon),
        the device's last address, and which alerts already fired. Returns the slot map."""
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for title, names in saved.get("animations", {}).items():
                names = [names] if isinstance(names, str) else names  # older files stored just the last one
                g._recent_animations[title] = [n for n in names if n in g.ANIMATIONS][-g.RECENT_ANIMATIONS:]
            self.saved_ip = saved.get("ip") or None
            self.alerts = {k: v for k, v in saved.get("alerts", {}).items() if isinstance(v, dict)}
            self.notify_enabled = bool(saved.get("notify", True))
            self.saved_view = saved.get("view") if saved.get("view") in (*TITLES, *PANEL_PUSH) else None
            self.saved_provider = saved.get("provider") if saved.get("provider") in TITLES else None
            self.pause_on_lock = bool(saved.get("pause_on_lock", True))
            self.dim_on_lock = bool(saved.get("dim_on_lock", False))
            level = saved.get("brightness")
            self.brightness = level if isinstance(level, int) and not isinstance(level, bool) else None
            night = saved.get("night")
            if isinstance(night, dict):
                self.night = {**NIGHT_DEFAULT, **{k: v for k, v in night.items() if k in NIGHT_DEFAULT}}
            self.history = {k: [list(p) for p in v if isinstance(p, (list, tuple)) and len(p) == 3][-HISTORY_MAX:]
                            for k, v in saved.get("history", {}).items() if isinstance(v, list)}
            slots = saved.get("slots", saved)  # older files were just the slot map
            slots = {p: s for p, s in slots.items() if p in (*TITLES, *PANEL_PUSH) and s in ("a", "b")}
            if saved.get("render") != RENDER_ID:
                self.outdated = set(slots)  # still shown at once when switched to, and refreshed in the background
            return slots
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_state(self) -> None:
        state = {
            "slots": self.slot, "animations": g._recent_animations, "ip": self.ip or None,
            "alerts": self.alerts, "notify": self.notify_enabled,
            "view": self._active_key(), "provider": self.provider,
            "pause_on_lock": self.pause_on_lock, "dim_on_lock": self.dim_on_lock,
            "brightness": self.brightness, "night": self.night, "history": self.history,
            "render": RENDER_ID if not self.outdated else None,  # only claimed once every stored image is current
        }
        try:
            STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            log.warning("could not save %s", STATE_PATH)

    # --- finding the device ---------------------------------------------------------------------------

    def _set_ip(self, ip: str, announce: bool = False) -> None:
        changed = ip != self.ip
        self.ip = ip
        self._save_state()
        if announce and changed:
            log.info("device found at %s", ip)
            self._notify("Pantalla encontrada", f"GeekMagic en {ip}")
        if changed:
            self._sync_with_device()  # a different device may not have our images

    def _resolve_device(self) -> None:
        """At startup: use --ip or the remembered address if it answers, otherwise look for the device."""
        for candidate in dict.fromkeys(filter(None, (self.ip, self.saved_ip))):
            if discover.probe(candidate, 2.0):
                self.ip = candidate
                return
        self.icon.title = "GeekMagic: buscando la pantalla en la red..."
        self.last_scan = time.monotonic()
        found = discover.find_device(prefer=self.ip or None)
        if found:
            self._set_ip(found, announce=True)
        else:
            log.warning("no GeekMagic device found yet (tried %s); will keep looking", self.ip or "a network scan")

    def _maybe_rediscover(self) -> None:
        """Called while the device doesn't answer: it may have a new IP, so look for it now and then."""
        now = time.monotonic()
        if self.ip:
            due = (self.offline_since is not None and now - self.offline_since >= REDISCOVER_AFTER
                   and now - self.last_scan >= REDISCOVER_EVERY)
        else:
            due = now - self.last_scan >= DISCOVER_RETRY
        if not due:
            return
        self.last_scan = now
        found = discover.find_device(prefer=self.ip or None)
        if found and found != self.ip:
            self._set_ip(found, announce=True)

    # --- clicks ---------------------------------------------------------------------------------------

    def _switched(self, key: str) -> None:
        """Common to every switch: pin the view's last image at once and refresh it in the background."""
        self._save_state()  # so a restart or reconnection comes back to this view
        self.icon.title = self._tooltip()
        self.icon.update_menu()
        self.last_click = time.monotonic()
        self.cancel.set()  # abort any upload in flight: the device serves one request at a time
        threading.Thread(target=self._show_uploaded, args=(key,), daemon=True).start()
        if time.monotonic() - self.last_upload.get(key, 0) > STALE_AFTER:
            self.wake.set()  # refresh the image we just switched to, once clicking settles

    def select(self, provider: str) -> None:
        log.info("switch -> %s", provider)
        self.provider = provider
        self.mode = None
        self.icon.icon = make_icon(provider)
        self._switched(provider)

    def toggle(self) -> None:
        """Left-click: Claude <-> Codex. From the split or stats view it goes back to the provider you were on."""
        if self.mode:
            self.select(self.provider)
        else:
            self.select("codex" if self.provider == "claude" else "claude")

    def _toggle_mode(self, mode: str) -> None:
        """Menu: a view of both providers at once. Turning it off returns to the last single provider."""
        if self.mode == mode:
            self.select(self.provider)
            return
        log.info("switch -> %s view", mode)
        self.mode = mode
        self.icon.icon = _icon_for(mode)
        self._switched(mode)

    def toggle_split(self) -> None:
        self._toggle_mode(SPLIT)

    def toggle_stats(self) -> None:
        self._toggle_mode(STATS)

    def toggle_notifications(self) -> None:
        self.notify_enabled = not self.notify_enabled
        log.info("notifications %s", "on" if self.notify_enabled else "off")
        self._save_state()
        self.icon.update_menu()

    # --- pause, and the computer being locked ---------------------------------------------------------------------

    def _is_paused(self) -> bool:
        return self.paused or (self.pause_on_lock and self.locked)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("paused" if self.paused else "resumed")
        self.icon.title = self._tooltip()
        self.icon.update_menu()
        if not self.paused:
            self.wake.set()  # catch up at once

    def toggle_pause_on_lock(self) -> None:
        self.pause_on_lock = not self.pause_on_lock
        log.info("pause when locked %s", "on" if self.pause_on_lock else "off")
        self._save_state()
        self.icon.title = self._tooltip()
        self.icon.update_menu()
        self.wake.set()

    def _watch_lock(self) -> None:
        """Thread: notice the computer being locked or unlocked, within a few seconds."""
        while not self.stop.is_set():
            locked = session_lock.is_locked()
            if locked != self.locked:
                self._set_locked(locked)
            self.stop.wait(LOCK_POLL)

    def _set_locked(self, locked: bool) -> None:
        self.locked = locked
        log.info("computer %s", "locked" if locked else "unlocked")
        self.icon.title = self._tooltip()
        if self.dim_on_lock and self.brightness is not None:
            self._run_in_background(self._apply_brightness, LOCK_DIM_LEVEL if locked else self.brightness)
        if not locked:
            self.wake.set()  # refresh at once and put the view back
            self._run_in_background(self._show_uploaded, self._active_key())

    # --- backlight: brightness, the device's night schedule, dimming while locked --------------------------------

    def _brightness_action(self, level: int):
        return lambda: self.set_brightness(level)

    def _run_in_background(self, func, *args) -> None:
        threading.Thread(target=func, args=args, daemon=True).start()

    def _apply_brightness(self, level: int) -> bool:
        """Send a backlight level to the device. The device serves one request at a time, so an upload in flight is
        cancelled first (the worker redoes it)."""
        if not self.ip:
            return False
        self.cancel.set()
        try:
            with self.push_lock:
                g.set_brightness(self.ip, level)
            return True
        except OSError as e:
            log.warning("could not set the brightness: %s", e)
            self._notify("Brillo", f"No se pudo cambiar el brillo: {e}")
            return False

    def set_brightness(self, level: int) -> None:
        """Menu: the normal (day) brightness. Remembered, because the device can't tell us what it's set to."""
        log.info("brightness -> %s", level)
        self.last_click = time.monotonic()  # leave the device free for this request
        if self._apply_brightness(level):
            self.brightness = level
            self._save_state()
            self.icon.update_menu()
            if self.night["enabled"]:  # the schedule carries the day level too, keep it in step
                self._run_in_background(self._apply_night)

    def _night_label(self) -> str:
        n = self.night
        return f"Modo nocturno ({_hour12(n['start'])} - {_hour12(n['end'])}, brillo {n['level']} %)"

    def _apply_night(self) -> bool:
        if not self.ip or self.brightness is None:
            return False
        self.cancel.set()
        try:
            with self.push_lock:
                g.set_night_mode(self.ip, start_hour=self.night["start"], end_hour=self.night["end"],
                                 night_level=self.night["level"], day_level=self.brightness, enabled=self.night["enabled"])
            return True
        except OSError as e:
            log.warning("could not set the night mode: %s", e)
            self._notify("Modo nocturno", f"No se pudo cambiar el modo nocturno: {e}")
            return False

    def _need_day_brightness(self, what: str) -> bool:
        """Night mode and dimming restore the normal brightness afterwards, and the device can't report it: it has
        to be one we set. Ask for that first instead of guessing and changing it behind the user's back."""
        if self.brightness is not None:
            return True
        self._notify(what, "Elige primero tu brillo normal en Pantalla > Brillo, para que pueda restaurarlo.")
        return False

    def toggle_night(self) -> None:
        """Menu: the device's own night schedule (works even with the computer off)."""
        enable = not self.night["enabled"]
        if enable and not self._need_day_brightness("Modo nocturno"):
            return
        self.last_click = time.monotonic()
        self.night["enabled"] = enable
        if self._apply_night():
            log.info("night mode %s", "on" if enable else "off")
        else:
            self.night["enabled"] = not enable  # the device didn't take it
        self._save_state()
        self.icon.update_menu()

    def toggle_dim_on_lock(self) -> None:
        if not self.dim_on_lock and not self._need_day_brightness("Atenuar al bloquear"):
            return
        self.dim_on_lock = not self.dim_on_lock
        log.info("dim when locked %s", "on" if self.dim_on_lock else "off")
        self._save_state()
        self.icon.update_menu()

    @staticmethod
    def open_logs() -> None:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(LOG_PATH)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(LOG_PATH)])  # in the default text editor
        else:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])

    def quit(self) -> None:
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    # --- talking to the device ---------------------------------------------------------------------------

    def _upload(self, key: str, usage: dict) -> bool:
        """Upload fresh data into the view's spare slot, then pin it if it's still the active view.

        `key` is a provider (`usage` is its reading) or SPLIT / STATS (`usage` is {"panels": [...]}).
        Each view alternates between two files, so an upload cut short by a click can only
        damage the spare slot; the slot on screen (and the one a switch will show) stays intact.
        """
        if time.monotonic() - self.last_click < SETTLE_SECONDS:
            # A click just happened. Cancelling only stops the upload in flight, and the rest of this cycle's uploads
            # would start straight away and keep the device (which serves one request at a time) busy for seconds
            # while the click's own request waits. Stand down; the worker retries once clicking settles.
            self.wake.set()
            return False
        self.cancel = cancel = threading.Event()
        with self.push_lock:
            spare = "b" if self.slot.get(key) == "a" else "a"
            filename = slot_file(key, spare)
            log.info("uploading %s -> %s", key, filename)
            try:
                if key in PANEL_PUSH:
                    extra = {"animation": self.animation} if key == SPLIT else {}  # the stats view is a still
                    getattr(g, PANEL_PUSH[key])(self.ip, usage["panels"], filename, show=False, cancel=cancel, **extra)
                else:
                    g.push_usage(self.ip, usage, self.animation, filename, show=False, cancel=cancel)
            except g.UploadCancelled:
                log.info("upload cancelled (%s)", key)
                self.wake.set()  # retry once the clicking settles
                return False
            self.slot[key] = spare
            self.outdated.discard(key)
            self._save_state()
            self.last_upload[key] = time.monotonic()
            if key == self._active_key():  # checked after the slow upload, in case the user switched meanwhile
                g.show_image(self.ip, filename)
            return True

    def _sync_with_device(self) -> None:
        """Forget remembered images the device no longer has, and drop leftovers of the old single-file scheme."""
        if not self.ip:
            return
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

    def _show_uploaded(self, key: str) -> None:
        """Instant switch: the view's last image is already on the device, so just pin it."""
        slot = self.slot.get(key)
        if slot is None:
            log.info("no %s image on the device yet; it appears after its first upload", key)
            return
        try:
            with self.push_lock:
                if key == self._active_key():
                    g.show_image(self.ip, slot_file(key, slot))
                    log.info("showed %s", key)
        except Exception:
            log.exception("show failed (%s)", key)

    def _mark_offline(self, reason: object) -> None:
        if not self.offline:
            log.warning("device unreachable (%s), retrying every %ss", reason, RETRY_SECONDS)
            self.offline = True
            self.offline_since = time.monotonic()
        self.icon.title = "GeekMagic: sin conexion con la pantalla, reintentando"

    def _device_is_back(self) -> bool:
        """While offline: pin the remembered view with one small request. If it works the device is back and its
        screen is on the right view again at once, without waiting for a whole upload; if not, we know it without
        trying a (slow) upload."""
        key = self._active_key()
        slot = self.slot.get(key)
        try:
            with self.push_lock:
                if slot:
                    g.show_image(self.ip, slot_file(key, slot))
                elif not discover.probe(self.ip, 2.0):
                    raise OSError("no answer")
        except OSError as e:
            self._mark_offline(e)
            return False
        log.info("device is back")
        self.offline, self.offline_since = False, None
        return True

    def _deliver(self, key: str, usage: dict) -> str:
        """Upload `usage` for a view: "ok", "cancelled" (a click got in the way), "offline" or "error"."""
        if not self.ip:
            self._mark_offline("no device address yet")
            return "offline"
        if self.offline and not self._device_is_back():
            return "offline"
        try:
            if not self._upload(key, usage):
                return "cancelled"
        except OSError as e:  # the device is off or unreachable
            self._mark_offline(e)
            return "offline"
        except Exception as e:  # keep the tray alive no matter what
            log.exception("upload failed (%s)", key)
            self.icon.title = f"GeekMagic: {e}"[:120]
            return "error"
        log.info("pushed %s", key)
        if self.offline:
            log.info("device is back")
            self.offline, self.offline_since = False, None
        self.icon.title = self._tooltip()
        return "ok"

    # --- reading usage, alerts, stale data ------------------------------------------------------------------

    def _fetch_usage(self, provider: str) -> dict | None:
        """Query the provider. Every successful read also feeds the alerts; a failing one may mark the data stale."""
        self.last_fetch[provider] = time.monotonic()
        try:
            usage = g.PROVIDERS[provider]()
        except Exception as e:  # keep the tray alive no matter what
            log.exception("fetch failed (%s)", provider)
            if provider == self.provider:
                self.icon.title = f"GeekMagic: {e}"[:120]
            self._mark_stale(provider)
            return None
        self._record_history(provider, usage)
        pace.annotate(usage, self._recent_rates(provider, usage))  # the pace that goes under each bar
        self.last_good[provider] = (usage, time.monotonic())
        self.stale.discard(provider)
        self._check_alerts(provider, usage)
        return usage

    def _record_history(self, provider: str, usage: dict) -> None:
        """Keep the readings of each window (when the percentage changed, or every few minutes) so the recent pace can
        be measured. Readings of an earlier window are dropped as soon as it resets."""
        now = time.time()
        for window, _ in WINDOWS:
            pct, reset = usage.get(f"{window}_pct"), usage.get(f"{window}_reset")
            if pct is None or reset is None:
                continue
            points = self.history.setdefault(f"{provider}/{window}", [])
            points[:] = [p for p in points if abs(p[2] - reset.timestamp()) < 120]
            if not points or points[-1][1] != pct or now - points[-1][0] >= HISTORY_EVERY:
                points.append([now, pct, reset.timestamp()])
            del points[:-HISTORY_MAX]

    def _recent_rates(self, provider: str, usage: dict) -> dict[str, float | None]:
        rates = {}
        for window, _ in WINDOWS:
            reset = usage.get(f"{window}_reset")
            rates[window] = pace.recent_rate(
                self.history.get(f"{provider}/{window}", []), reset.timestamp() if reset else None,
                time.time(), pace.LOOKBACK_MIN[window],
            )
        return rates

    def _mark_stale(self, provider: str) -> None:
        """Numbers that can't be refreshed for a few minutes: put the last ones on screen, dimmed and dated."""
        good = self.last_good.get(provider)
        if not good or provider in self.stale or not self.ip or self.offline or self.mode:
            return  # (the split and stats views dim stale panels themselves, see _panels)
        usage, read_at = good
        if time.monotonic() - read_at < STALE_SECONDS:
            return
        self.stale.add(provider)
        log.warning("no fresh %s data for %d s; showing the last reading dimmed", provider, STALE_SECONDS)
        try:
            self._upload(provider, dict(usage, stale=True))
        except Exception:
            log.exception("could not upload the stale image (%s)", provider)

    def _check_alerts(self, provider: str, usage: dict) -> None:
        """Notify when a window newly reaches 80 % / 95 %, and when it resets after that."""
        for window, label in WINDOWS:
            pct = usage.get(f"{window}_pct")
            if pct is None:
                continue
            key = f"{provider}/{window}"
            previous = self.alerts.get(key, {})
            level, event = alerts.evaluate(previous.get("level", 0), previous.get("pct"), pct)
            self.alerts[key] = {"level": level, "pct": pct}
            if event is None:
                continue
            kind, threshold = event
            if kind == "rise":
                self._notify(TITLES[provider], f"La {label} llegó al {pct:.0f} % (umbral {threshold} %)")
            else:
                self._notify(TITLES[provider], f"La {label} se reinició ({pct:.0f} % usado)")
        self._save_state()

    def _notify(self, title: str, message: str) -> None:
        log.info("notification: %s - %s", title, message)
        if not self.notify_enabled:
            return
        try:
            self.icon.notify(message, title)
        except Exception:  # not every platform/backend supports it, and it must never break the update loop
            log.warning("could not show the notification", exc_info=True)

    def _refresh_other(self, provider: str) -> None:
        """The provider that isn't on screen: read it now and then (its alerts matter too) and keep its image fresh."""
        other = "codex" if provider == "claude" else "claude"
        # nothing (or only an out-of-date image) of it on the device yet: do it now, so the first click is instant too
        needs_image = (other not in self.slot or other in self.outdated) and not self.offline
        if not needs_image and time.monotonic() - self.last_fetch.get(other, 0) < OTHER_REFRESH:
            return
        usage = self._fetch_usage(other)
        if usage is not None and self.ip and not self.offline:
            try:
                self._upload(other, usage)
            except Exception:
                log.warning("could not upload the %s image", other, exc_info=True)

    def _panels(self, key: str) -> list[dict]:
        """The two providers' latest readings for a view of both. One whose read failed keeps its last good numbers,
        dimmed and dated once they're a few minutes old. The stats view also carries Codex's local activity counts."""
        panels = []
        for provider in TITLES:
            good = self.last_good.get(provider)
            if good is None:
                continue
            usage, read_at = good
            usage = dict(usage, stale=True) if time.monotonic() - read_at >= STALE_SECONDS else dict(usage)
            if key == STATS and provider == "codex":
                usage["local_stats"] = self.local_stats
            panels.append(usage)
        return panels

    def _refresh_local_stats(self) -> None:
        """Codex's activity counts come from its local logs (~90 MB of them): count now and then, not every cycle."""
        if time.monotonic() - self.local_stats_at < LOCAL_STATS_EVERY and self.local_stats is not None:
            return
        self.local_stats_at = time.monotonic()
        try:
            self.local_stats = usage_stats.codex_local_stats(time.time())
        except Exception:
            log.warning("could not count Codex's local activity", exc_info=True)

    def _update_panels(self, key: str) -> str:
        """Split / stats view: read both providers (so both get alerts) and upload one screen with the two of them."""
        for provider in TITLES:
            self._fetch_usage(provider)
        if key == STATS:
            self._refresh_local_stats()
        panels = self._panels(key)
        if not panels:
            return "error"
        return self._deliver(key, {"panels": panels})

    def _update_split(self) -> str:
        return self._update_panels(SPLIT)

    def _refresh_outdated_views(self) -> None:
        """After an update the images stored on the device were drawn by the old code (old labels, colours...), and a
        view is only redrawn while it's on screen. Redraw the ones not on screen, from the latest readings, without
        showing them. One per cycle, so the device stays free for clicks."""
        if self.offline or not self.ip:
            return
        for key in sorted(self.outdated - {self._active_key()}):
            if key in PANEL_PUSH:
                if key == STATS:
                    self._refresh_local_stats()
                usage = {"panels": self._panels(key)}
                ready = bool(usage["panels"])
            else:
                good = self.last_good.get(key)
                usage, ready = (good[0] if good else None), good is not None
            if not ready:
                continue  # nothing read for it yet; a later cycle will have something
            try:
                self._upload(key, usage)
            except Exception:
                log.warning("could not refresh the stored %s image", key, exc_info=True)
            return

    # --- main loop -------------------------------------------------------------------------------------------

    def worker(self) -> None:
        pending: tuple[str, dict, float] | None = None  # (provider, usage, read at) awaiting delivery
        self._resolve_device()
        self._sync_with_device()
        self._show_uploaded(self._active_key())  # the remembered view up right away, before the first (slow) refresh
        while not self.stop.is_set():
            self.wake.clear()
            # An upload keeps the device busy for seconds; wait out rapid toggling so a click never queues behind it.
            while not self.stop.is_set() and time.monotonic() - self.last_click < SETTLE_SECONDS:
                time.sleep(0.5)
            if self._is_paused():  # nothing is read or uploaded; coming back (resume / unlock) wakes the loop
                self.icon.title = self._tooltip()
                self.wake.wait(PAUSE_POLL)
                continue
            provider = self.provider
            wait = self.interval
            if self.mode:
                if self._update_panels(self.mode) == "offline":
                    wait = RETRY_SECONDS
                    self._maybe_rediscover()
                self._refresh_outdated_views()
                self.wake.wait(wait)
                continue
            if pending and pending[0] == provider and time.monotonic() - pending[2] < self.interval:
                usage = pending[1]  # read moments ago but not delivered yet: don't query again
            else:
                usage = self._fetch_usage(provider)
            pending = None
            if usage is not None:
                outcome = self._deliver(provider, usage)
                if outcome == "cancelled":  # retry with the same data once the clicking settles
                    pending = (provider, usage, time.monotonic())
                elif outcome == "offline":  # retry soon, without querying usage again
                    pending = (provider, usage, time.monotonic())
                    wait = RETRY_SECONDS
                    self._maybe_rediscover()
            self._refresh_other(provider)
            self._refresh_outdated_views()
            self.wake.wait(wait)

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self._watch_lock, daemon=True).start()
        self.icon.run()


def night_hours(text: str) -> tuple[int, int]:
    """"22-7" -> (22, 7); argparse type for --night."""
    try:
        start, end = (int(part) for part in text.split("-"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected START-END with hours 0-23, like 22-7 (got {text!r})") from None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise argparse.ArgumentTypeError(f"hours must be 0-23 (got {text!r})")
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", help="GeekMagic device IP (omit it to find the screen on your network)")
    parser.add_argument("--interval", type=int, default=30, metavar="SECONDS")
    parser.add_argument(
        "--provider", choices=list(g.PROVIDERS),
        help="which view to start on the very first run (afterwards the app comes back to the one you left)",
    )
    parser.add_argument("--animation", default="auto", choices=[*g.ANIMATIONS, "auto", "random"])
    parser.add_argument("--night", metavar="START-END", type=night_hours,
                        help="hours of the screen's night mode, 24-hour clock, e.g. 22-7 (default; turn it on from the menu)")
    parser.add_argument("--night-brightness", type=int, metavar="LEVEL", choices=range(g.BRIGHTNESS_RANGE[0], g.BRIGHTNESS_RANGE[1] + 1),
                        help=f"backlight level during the night mode ({g.BRIGHTNESS_RANGE[0]} to {g.BRIGHTNESS_RANGE[1]})")
    parser.add_argument("--install-startup", action="store_true", help="start this app automatically at login, then exit")
    parser.add_argument("--uninstall-startup", action="store_true", help="remove the automatic start, then exit")
    args = parser.parse_args()

    if args.uninstall_startup:
        print(autostart.uninstall())
        return
    if args.install_startup:
        print(autostart.install([
            *(["--ip", args.ip] if args.ip else []), "--interval", str(args.interval),
            *(["--provider", args.provider] if args.provider else []), "--animation", args.animation,
            *(["--night", f"{args.night[0]}-{args.night[1]}"] if args.night else []),
            *(["--night-brightness", str(args.night_brightness)] if args.night_brightness is not None else []),
        ]))
        return
    setup_logging()
    App(args.ip, args.interval, args.animation, args.provider, args.night, args.night_brightness).run()


if __name__ == "__main__":
    main()
