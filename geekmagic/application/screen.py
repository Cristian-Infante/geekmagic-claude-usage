"""The screen itself: where it is on the network, which of its stored images is current, and putting pictures on it.

Every view has two image files on the device (slot "a" and "b") and uploads alternate between them, so an upload cut
short by a click can only damage the spare one: the one on screen, and the one a switch will show, stay intact.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from geekmagic.application import config
from geekmagic.application import image_files
from geekmagic.core.errors import UploadCancelled
from geekmagic.core.model import ErrorScreen, Usage
from geekmagic.core.ports import Display, DeviceLocator, Renderer
from geekmagic.core.registry import ProviderRegistry
from geekmagic.core.views import PANEL_KEYS

log = logging.getLogger("tray")


class Screen:
    def __init__(self, ip: str | None, animation: str, providers: ProviderRegistry, renderer: Renderer,
                 display_factory: Callable[[str], Display], locator: DeviceLocator, *,
                 active_key: Callable[[], str], flag: Callable[[str, object], object], notify: Callable[[str, str], None],
                 save: Callable[[], None], set_title: Callable[[str], None], tooltip: Callable[[], str],
                 wake: threading.Event) -> None:
        self.animation = animation  # "auto" / "random", or the name of one animation to pin
        self.providers = providers
        self.renderer = renderer
        self.render_id = renderer.version  # identifies the code that draws the screens, see snapshot
        self.display_factory = display_factory  # (ip) -> the screen at that address
        self.locator = locator
        self.active_key = active_key
        self.flag = flag  # (key, data) -> data with the "its agent is working" flags set
        self.notify = notify
        self.save = save
        self.set_title = set_title
        self.tooltip = tooltip
        self.wake = wake
        self.given_ip = ip  # --ip
        self.saved_ip: str | None = None  # last address the device answered on, see restore
        self.ip = ""  # "" until the device is found
        self.device: Display | None = None
        self.slots: dict[str, str] = {}  # view -> "a"/"b": the file holding its complete image on the device
        self.outdated: set[str] = set()  # views whose stored image was drawn by an older version of the code
        self.cancel = threading.Event()  # set by a click: abort the upload in flight
        self.last_upload: dict[str, float] = {}
        self.last_click = 0.0
        self.last_scan = 0.0
        self.offline = False  # the device didn't answer on the last attempt
        self.offline_since: float | None = None
        self.shown_failed = False  # the last attempt to put a remembered image on screen didn't get through
        self.error_shown: dict[str, str] = {}  # provider -> the error its image on the device says
        self.push_lock = threading.Lock()  # the device serves one request at a time

    # --- remembered ---------------------------------------------------------------------------------------------

    def restore(self, saved: dict) -> None:
        self.saved_ip = saved.get("ip") or None
        slots = saved.get("slots", saved)  # older files were just the slot map
        self.slots = {p: s for p, s in slots.items() if p in (*self.providers, *PANEL_KEYS) and s in ("a", "b")}
        if saved.get("render") != self.render_id:
            self.outdated = set(self.slots)  # still shown at once when switched to, and refreshed in the background
        self.set_ip_quietly(self.given_ip or self.saved_ip or "")

    def snapshot(self) -> dict:
        return {
            "slots": self.slots, "ip": self.ip or None,
            "render": self.render_id if not self.outdated else None,  # only claimed once every stored image is current
        }

    # --- finding the device -------------------------------------------------------------------------------------

    def set_ip_quietly(self, ip: str) -> None:
        self.ip = ip
        self.device = self.display_factory(ip) if ip else None

    def set_ip(self, ip: str, announce: bool = False) -> None:
        changed = ip != self.ip
        self.set_ip_quietly(ip)
        self.save()
        if announce and changed:
            log.info("device found at %s", ip)
            self.notify("Pantalla encontrada", f"GeekMagic en {ip}")
        if changed:
            self.sync_with_device()  # a different device may not have our images

    def resolve_device(self) -> None:
        """At startup: use --ip or the remembered address if it answers, otherwise look for the device."""
        for candidate in dict.fromkeys(filter(None, (self.ip, self.saved_ip))):
            if self.locator.probe(candidate, 2.0):
                self.set_ip_quietly(candidate)
                return
        self.set_title("GeekMagic: buscando la pantalla en la red...")
        self.last_scan = time.monotonic()
        found = self.locator.find_device(prefer=self.ip or None)
        if found:
            self.set_ip(found, announce=True)
        else:
            log.warning("no GeekMagic device found yet (tried %s); will keep looking", self.ip or "a network scan")

    def maybe_rediscover(self) -> None:
        """Called while the device doesn't answer: it may have a new IP, so look for it now and then."""
        now = time.monotonic()
        if self.ip:
            due = (self.offline_since is not None and now - self.offline_since >= config.REDISCOVER_AFTER
                   and now - self.last_scan >= config.REDISCOVER_EVERY)
        else:
            due = now - self.last_scan >= config.DISCOVER_RETRY
        if not due:
            return
        self.last_scan = now
        found = self.locator.find_device(prefer=self.ip or None)
        if found and found != self.ip:
            self.set_ip(found, announce=True)

    # --- what's on the device -----------------------------------------------------------------------------------

    def upload(self, key: str, data: Usage | list[Usage] | ErrorScreen) -> bool:
        """Upload fresh data into the view's spare slot, then pin it if it's still the active view.

        `key` is a provider (`data` is its Usage, or the ErrorScreen saying why it can't be read) or one of the views of
        two (`data` is the list of the two Usage).
        """
        if time.monotonic() - self.last_click < config.SETTLE_SECONDS:
            # A click just happened. Cancelling only stops the upload in flight, and the rest of this cycle's uploads
            # would start straight away and keep the device (which serves one request at a time) busy for seconds
            # while the click's own request waits. Stand down; the worker retries once clicking settles.
            self.wake.set()
            return False
        data = self.flag(key, data)
        self.cancel = cancel = threading.Event()
        with self.push_lock:
            spare = "b" if self.slots.get(key) == "a" else "a"
            filename = image_files.slot_file(key, spare)
            log.info("uploading %s -> %s", key, filename)
            try:
                rendered = self.renderer.render(key, data, self.animation)
                self.device.upload(rendered.gif, filename, cancel=cancel)
            except UploadCancelled:
                log.info("upload cancelled (%s)", key)
                self.wake.set()  # retry once the clicking settles
                return False
            self.renderer.confirm(rendered)  # now it really is on the device
            self.slots[key] = spare
            self.outdated.discard(key)
            if key in self.providers and isinstance(data, ErrorScreen):
                self.error_shown[key] = data.message
            self.save()
            self.last_upload[key] = time.monotonic()
            if key == self.active_key():  # checked after the slow upload, in case the user switched meanwhile
                self.device.show_image(filename)
            return True

    def sync_with_device(self) -> None:
        """Forget remembered images the device no longer has, and drop leftovers of the old single-file scheme."""
        if not self.ip:
            return
        try:
            stored = self.device.list_images()
            for key, slot in list(self.slots.items()):
                if image_files.slot_file(key, slot) not in stored:
                    log.info("%s image is gone from the device, will re-upload", key)
                    del self.slots[key]
            for key in self.providers:
                if image_files.image_name(key) in stored:
                    self.device.delete_image(image_files.image_name(key))
            self.save()
        except OSError:
            pass  # device unreachable right now; keep what we remember

    def show_uploaded(self, key: str) -> None:
        """Instant switch: the view's last image is already on the device, so just pin it."""
        slot = self.slots.get(key)
        if slot is None:
            log.info("no %s image on the device yet; it appears after its first upload", key)
            return
        try:
            with self.push_lock:
                if key == self.active_key():
                    self.device.show_image(image_files.slot_file(key, slot))
                    self.shown_failed = False
                    log.info("showed %s", key)
        except OSError as e:  # the device not answering is routine (it's off, or being searched for)
            self.shown_failed = True
            log.warning("could not show %s: %s", key, e)
        except Exception:
            self.shown_failed = True
            log.exception("show failed (%s)", key)

    # --- the device being there or not --------------------------------------------------------------------------

    def mark_offline(self, reason: object) -> None:
        if not self.offline:
            log.warning("device unreachable (%s), retrying every %ss", reason, config.RETRY_SECONDS)
            self.offline = True
            self.offline_since = time.monotonic()
        self.set_title("GeekMagic: sin conexion con la pantalla, reintentando")

    def device_is_back(self) -> bool:
        """While offline: pin the remembered view with one small request. If it works the device is back and its
        screen is on the right view again at once, without waiting for a whole upload; if not, we know it without
        trying a (slow) upload."""
        key = self.active_key()
        slot = self.slots.get(key)
        try:
            with self.push_lock:
                if slot:
                    self.device.show_image(image_files.slot_file(key, slot))
                elif not self.locator.probe(self.ip, 2.0):
                    raise OSError("no answer")
        except OSError as e:
            self.mark_offline(e)
            return False
        log.info("device is back")
        self.offline, self.offline_since = False, None
        return True

    def deliver(self, key: str, data: Usage | list[Usage] | ErrorScreen) -> str:
        """Upload `data` for a view: "ok", "cancelled" (a click got in the way), "offline" or "error"."""
        if not self.ip:
            self.mark_offline("no device address yet")
            return "offline"
        if self.offline and not self.device_is_back():
            return "offline"
        try:
            if not self.upload(key, data):
                return "cancelled"
        except OSError as e:  # the device is off or unreachable
            self.mark_offline(e)
            return "offline"
        except Exception as e:  # keep the tray alive no matter what
            log.exception("upload failed (%s)", key)
            self.set_title(f"GeekMagic: {e}"[:120])
            return "error"
        log.info("pushed %s", key)
        if self.offline:
            log.info("device is back")
            self.offline, self.offline_since = False, None
        self.set_title(self.tooltip())
        return "ok"

    # --- clicks -------------------------------------------------------------------------------------------------

    def note_click(self) -> None:
        """A click: abort any upload in flight (the device serves one request at a time) and keep new ones off until
        clicking settles."""
        self.last_click = time.monotonic()
        self.cancel.set()
