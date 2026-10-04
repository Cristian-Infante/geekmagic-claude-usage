#!/usr/bin/env python3
"""System-tray app: keeps a GeekMagic SmallTV updated and lets you flip between Claude and Codex usage.

Windows/Linux: left-click the tray icon to toggle, right-click for the menu.
macOS: clicking the menu-bar icon opens the menu; pick Claude or Codex there.

    python tray.py                                      # finds the screen on your network by itself
    python tray.py --ip 192.168.1.20                    # or tell it where the screen is
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
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import agent_activity
import alerts
import autostart
import discover
import geekmagic_claude as g
import login
import notifier
import pace
import session_lock
import single_instance
import usage_stats

# Identifies the code that draws the screens. The device keeps the images it was sent, so after an update (new
# labels, colours, animations...) what's stored is out of date; a changed id makes the app re-upload every view once.
RENDER_ID = hashlib.sha1(b"".join(
    Path(module.__file__).read_bytes() for module in (g, pace, alerts, usage_stats, agent_activity)
)).hexdigest()[:12]

LOG_PATH = Path(__file__).with_name("tray.log")
LOCK_PATH = Path(__file__).with_name("tray.lock")  # held by the running app, see single_instance
_instance_lock = None
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
LOGIN_COOLDOWN = 300  # a sign-in window isn't opened again for the same provider within this many seconds
LOGIN_POLL = 10  # while one is being signed in, how often its usage is tried again (so the screen fills in at once)
LOCAL_STATS_EVERY = 600  # the providers' local activity counts are refreshed at most this often (seconds)
LOCAL_STATS_RETRY = 60  # ...or this often while one of them is still missing (it failed)
BACKGROUND_STATS = True  # count in a thread (the first count of a month of logs takes seconds); tests turn it off
AGENT_POLL = 2  # seconds between looks at whether an agent is working (a read of a few log tails: cheap)
AGENT_IDLE_POLLS = 2  # polls in a row that must say "idle" before a run counts as finished (no flapping)
AGENT_WAIT_POLLS = 2  # polls in a row that must say "waiting for you" before it's shown and notified
AGENT_MIN_RUN = 60  # a run shorter than this (seconds) ends without a notification
HISTORY_MAX = 150  # readings kept per usage window, for the recent-pace estimate
HISTORY_EVERY = {"current": 300, "weekly": 1800}  # ...adding one at least this often (seconds), or when the % changed
DEFAULT_BRIGHTNESS_CHOICES = (100, 75, 50, 25, 10, 0)  # the menu's brightness levels (the device takes -10..100)
LOCK_DIM_LEVEL = 0  # backlight level while the computer is locked, if "dim when locked" is on
NIGHT_DEFAULT = {"enabled": False, "start": 22, "end": 7, "level": 10}  # 10 PM - 7 AM at 10 %


def slot_file(provider: str, slot: str) -> str:
    return f"{provider}-usage-{slot}.gif"


TITLES = {"claude": "Claude", "codex": "Codex"}
SPLIT = "split"  # key of the third view (both providers on one screen); it has its own image slots on the device
STATS = "stats"  # the fourth: activity numbers for both providers
BREAKDOWN = "breakdown"  # which projects and models the requests went to
HOURS = "hours"  # at what hours of the day you work
# views of both providers -> the geekmagic_claude function that uploads them
PANEL_PUSH = {SPLIT: "push_split", STATS: "push_stats", BREAKDOWN: "push_breakdown", HOURS: "push_hours"}
STATS_VIEWS = {STATS, BREAKDOWN, HOURS}  # the ones drawn from the providers' local activity logs
VIEW_NAMES = {SPLIT: "vista dividida", STATS: "estadísticas", BREAKDOWN: "proyectos y modelos", HOURS: "horas pico"}
PROVIDER_OF = {"Claude": "claude", "Codex": "codex"}  # a reading's title -> its provider key
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


def make_breakdown_icon() -> Image.Image:
    """Three horizontal bars of different lengths: shares of a total."""
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for i, (length, color) in enumerate(((46, g.THEMES["Claude"]["body"]), (32, g.THEMES["Codex"]["body"]), (18, g.THEMES["Codex"]["weekly"]))):
        y = 12 + i * 15
        draw.rectangle((8, y, 8 + length, y + 9), fill=color)
    return canvas


def make_hours_icon() -> Image.Image:
    """A little histogram with its busy stretch in the middle: the hours of the day."""
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for i, height in enumerate((8, 12, 22, 38, 44, 30, 16, 10)):
        x = 6 + i * 7
        draw.rectangle((x, 54 - height, x + 5, 54), fill=g.THEMES["Claude"]["body"] if 3 <= i <= 4 else g._blend(g.THEMES["Claude"]["body"], g.BG, 0.45))
    return canvas


def _icon_for(key: str) -> Image.Image:
    return {SPLIT: make_split_icon, STATS: make_stats_icon, BREAKDOWN: make_breakdown_icon,
            HOURS: make_hours_icon}.get(key, lambda: make_icon(key))()


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
        self.notify_done = True  # tell me when an agent that has been working a while finishes
        self.agent: dict[str, dict] = {}  # provider -> latest agent_activity state (is it working right now?)
        # provider -> session id -> what's known of that session (see _track_session): one per project you work in
        self.tracks: dict[str, dict[str, dict]] = {}
        self.agent_dirty = False  # an agent started or stopped: redraw from what's already read
        self._counting = False  # the local activity counts are being computed in a thread
        self._shown_failed = False  # the last attempt to put a remembered image on screen didn't get through
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
        # provider -> its activity from its local logs (see usage_stats); {} = it has none, missing = not counted yet
        self.local_stats: dict[str, dict] = {}
        self.local_stats_at = 0.0
        self.ip = ip or self.saved_ip or ""  # "" until the device is found
        self.cancel = threading.Event()
        self.last_upload: dict[str, float] = {}
        self.last_fetch: dict[str, float] = {}
        self.last_good: dict[str, tuple[dict, float]] = {}  # provider -> (last usage that was read OK, when)
        self.stale: set[str] = set()  # providers whose image on the device is the dimmed "no fresh data" one
        self.needs_login: set[str] = set()  # providers whose last read failed because you're signed out
        self.login_at: dict[str, float] = {}  # provider -> when its sign-in window was last opened (monotonic)
        self.login_notes: dict[str, str] = {}  # provider -> what its screen says about that window
        self.login_missing: set[str] = set()  # providers whose CLI isn't installed, so there's nothing to sign in to
        self.errors: dict[str, str] = {}  # provider -> why its last read failed (gone once a read works)
        self.error_shown: dict[str, str] = {}  # provider -> the error its image on the device says
        self.last_click = 0.0
        self.last_scan = 0.0
        self.offline = False  # the device didn't answer on the last attempt
        self.offline_since: float | None = None
        self.push_lock = threading.Lock()
        # Guards the readings, history, alerts and the state file: the two providers are read in parallel threads.
        self._data_lock = threading.RLock()
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
                pystray.MenuItem("Más vistas", pystray.Menu(
                    pystray.MenuItem("Proyectos y modelos", self.toggle_breakdown, checked=lambda _: self.mode == BREAKDOWN),
                    pystray.MenuItem("Horas pico", self.toggle_hours, checked=lambda _: self.mode == HOURS),
                )),
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
                    pystray.MenuItem("Iniciar sesión", pystray.Menu(
                        *(pystray.MenuItem(TITLES[key], self._login_action(key)) for key in TITLES))),
                    pystray.MenuItem("Notificaciones", self.toggle_notifications, checked=lambda _: self.notify_enabled),
                    pystray.MenuItem("Avisar cuando un agente termine o te espere", self.toggle_notify_done, checked=lambda _: self.notify_done),
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
        view = VIEW_NAMES[self.mode] if self.mode else f"mostrando {TITLES[self.provider]}"
        notes = [f"{TITLES[p]} te espera" if self._waiting(p) else f"{TITLES[p]} trabajando"
                 for p in TITLES if self._working(p)]
        if notes:
            view += " · " + " y ".join(notes)
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
            self.notify_done = bool(saved.get("notify_done", True))
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
        with self._data_lock:
            self._write_state()

    def _write_state(self) -> None:
        state = {
            "slots": self.slot, "animations": g._recent_animations, "ip": self.ip or None,
            "alerts": self.alerts, "notify": self.notify_enabled,
            "view": self._active_key(), "provider": self.provider,
            "pause_on_lock": self.pause_on_lock, "dim_on_lock": self.dim_on_lock, "notify_done": self.notify_done,
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
        self._run_in_background(self.ask_login, provider)  # (only does anything if it's signed out)

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

    def toggle_breakdown(self) -> None:
        self._toggle_mode(BREAKDOWN)

    def toggle_hours(self) -> None:
        self._toggle_mode(HOURS)

    def toggle_notify_done(self) -> None:
        self.notify_done = not self.notify_done
        log.info("agent-finished notifications %s", "on" if self.notify_done else "off")
        self._save_state()
        self.icon.update_menu()

    # --- agents: is Claude / Codex working right now? ----------------------------------------------------------

    def _watch_agents(self) -> None:
        """Thread: look at the agents' logs every few seconds, even while paused or locked (that's when you want
        to hear that one finished)."""
        while not self.stop.is_set():
            self._poll_agents()
            self.stop.wait(AGENT_POLL)

    def _poll_agents(self) -> None:
        for provider, state_of in agent_activity.STATES.items():
            try:
                state = state_of(time.time())
            except Exception:
                log.warning("could not read %s's activity", provider, exc_info=True)
                continue
            self._agent_update(provider, state)

    def _agent_update(self, provider: str, state: dict) -> None:
        """Follow a provider's agents, one session at a time (you may be working in several projects at once, and
        each one's run starts, asks you something and finishes on its own). Without a list of sessions the whole
        state is treated as a single one. The screens only need the sum: is any of them working / waiting?"""
        sessions = state["sessions"] if "sessions" in state else [{**state, "id": "-"}]  # (an empty list is "none")
        tracks = self.tracks.setdefault(provider, {})
        seen = {s.get("id") or "-" for s in sessions}
        changed = False
        for s in sessions:
            changed |= self._track_session(provider, s.get("id") or "-", s, tracks)
        for gone in [sid for sid in tracks if sid not in seen]:  # its log went quiet for good: it can only be idle
            changed |= self._track_session(provider, gone, {"working": False, "waiting": False}, tracks)
        for idle in [sid for sid, t in tracks.items() if not t["working"]]:
            del tracks[idle]  # an idle session needs no memory: the debounce counters only matter while it works
        busy = [t for t in tracks.values() if t["working"]]
        waiting = [t for t in busy if t["waiting"]]
        lead = (waiting or busy or [{}])[0]  # the one that needs you, else any that works (for the screens' summary)
        self.agent[provider] = {
            "working": bool(busy), "waiting": bool(waiting), "waiting_for": lead.get("waiting_for") if waiting else None,
            "project": lead.get("project") if busy else state.get("project"), "since": min((t["started"] for t in busy), default=None),
            "sessions": len(busy),
        }
        if changed:
            self._agent_changed()

    def _track_session(self, provider: str, sid: str, s: dict, tracks: dict) -> bool:
        """One session: a run starting shows at once; a question is believed after AGENT_WAIT_POLLS polls in a row and
        a run ending after AGENT_IDLE_POLLS quiet ones, so the gaps between its steps don't flicker or notify early.
        Returns whether something visible changed."""
        t = tracks.setdefault(sid, {"working": False, "waiting": False, "waiting_for": None, "started": None,
                                    "idle_polls": 0, "wait_polls": 0, "project": None})
        if s.get("project"):
            t["project"] = s["project"]
        changed = False
        if s.get("working"):
            t["idle_polls"] = 0
            if not t["working"]:
                t.update(working=True, started=s.get("since") or time.time())
                log.info("%s started working (%s)", provider, t["project"])
                changed = True
            t["wait_polls"] = t["wait_polls"] + 1 if s.get("waiting") else 0
            waiting_now = bool(s.get("waiting")) and (t["waiting"] or t["wait_polls"] >= AGENT_WAIT_POLLS)
            if waiting_now != t["waiting"]:
                t["waiting"], t["waiting_for"] = waiting_now, (s.get("waiting_for") if waiting_now else None)
                log.info("%s (%s) %s", provider, t["project"],
                         f"is waiting for you ({t['waiting_for']})" if waiting_now else "is no longer waiting for you")
                if waiting_now:
                    self._notify_waiting(provider, t)
                changed = True
        elif t["working"]:
            t["idle_polls"] += 1
            if t["idle_polls"] >= AGENT_IDLE_POLLS:
                lasted = time.time() - t["started"] if t["started"] else 0
                log.info("%s (%s) finished (worked %s)", provider, t["project"], agent_activity.duration_text(lasted))
                if self.notify_done and lasted >= AGENT_MIN_RUN:
                    self._notify(self._agent_title(provider, t["project"]), f"Terminó (trabajó {agent_activity.duration_text(lasted)})")
                t.update(working=False, waiting=False, waiting_for=None, started=None, wait_polls=0)
                changed = True
        return changed

    @staticmethod
    def _agent_title(provider: str, project: str | None) -> str:
        """"Claude · Acme App": the project is in the title, which is what you read first when you have several going."""
        return f"{TITLES[provider]} · {project}" if project else TITLES[provider]

    def _notify_waiting(self, provider: str, session: dict) -> None:
        """It stopped and needs you: say what for (a question, a plan to approve, a tool to allow); the title says where."""
        reason = session.get("waiting_for") or ""
        if reason == "question":
            what = "Te hizo una pregunta"
        elif reason == "plan":
            what = "Espera que apruebes su plan"
        elif reason.startswith("approval"):
            what = "Espera tu aprobación" + reason[len("approval"):]  # "approval (Edit)" -> "...aprobación (Edit)"
        else:
            what = "Espera tu respuesta"
        if self.notify_done:
            self._notify(self._agent_title(provider, session.get("project")), what)

    def _agent_changed(self) -> None:
        """Redraw with (or without) the working indicator, from the readings we already have."""
        self.agent_dirty = True
        self.icon.title = self._tooltip()
        self.wake.set()

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
        usage = self._flag_working(key, usage)
        self.cancel = cancel = threading.Event()
        with self.push_lock:
            spare = "b" if self.slot.get(key) == "a" else "a"
            filename = slot_file(key, spare)
            log.info("uploading %s -> %s", key, filename)
            try:
                if key in PANEL_PUSH:
                    extra = {"animation": self.animation} if key == SPLIT else {}  # the stats view is a still
                    getattr(g, PANEL_PUSH[key])(self.ip, usage["panels"], filename, show=False, cancel=cancel, **extra)
                elif usage.get("error"):  # a provider that has never been read: say why instead of showing nothing
                    g.push_error(self.ip, usage, filename, show=False, cancel=cancel)
                else:
                    g.push_usage(self.ip, usage, self.animation, filename, show=False, cancel=cancel)
            except g.UploadCancelled:
                log.info("upload cancelled (%s)", key)
                self.wake.set()  # retry once the clicking settles
                return False
            self.slot[key] = spare
            self.outdated.discard(key)
            if key in TITLES and usage.get("error"):
                self.error_shown[key] = usage["error"]
            self._save_state()
            self.last_upload[key] = time.monotonic()
            if key == self._active_key():  # checked after the slow upload, in case the user switched meanwhile
                g.show_image(self.ip, filename)
            return True

    def _working(self, provider: str | None) -> bool:
        return bool(provider and self.agent.get(provider, {}).get("working"))

    def _waiting(self, provider: str | None) -> bool:
        return bool(provider and self.agent.get(provider, {}).get("waiting"))

    def _flag_working(self, key: str, usage: dict) -> dict:
        """What's about to be drawn, with each provider's "its agent is working right now" flag set from the latest
        look at its logs (the screen draws a green dot and the mascot works)."""
        if key in TITLES:
            return {**usage, "working": self._working(key), "waiting": self._waiting(key)}
        if key in PANEL_PUSH:
            return {**usage, "panels": [{**p, "working": self._working(PROVIDER_OF.get(p.get("title"))),
                                         "waiting": self._waiting(PROVIDER_OF.get(p.get("title")))}
                                        for p in usage["panels"]]}
        return usage

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
                    self._shown_failed = False
                    log.info("showed %s", key)
        except OSError as e:  # the device not answering is routine (it's off, or being searched for)
            self._shown_failed = True
            log.warning("could not show %s: %s", key, e)
        except Exception:
            self._shown_failed = True
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
            if isinstance(e, g.UsageError):  # an expected problem (not signed in...): its message says it all
                log.warning("fetch failed (%s): %s", provider, e)
            else:
                log.exception("fetch failed (%s)", provider)
            if isinstance(e, g.SignInNeeded):
                self.needs_login.add(provider)
            self.errors[provider] = self._login_note(provider) or str(e)  # (while a sign-in window is open it says so)
            if provider == self.provider:
                self.icon.title = f"GeekMagic: {e}"[:120]
            self._mark_stale(provider)
            return None
        with self._data_lock:  # (the slow part, the read itself, was above and ran unlocked)
            self._record_history(provider, usage)
            pace.annotate(usage, self._recent_rates(provider, usage))  # the pace that goes under each bar
            self.last_good[provider] = (usage, time.monotonic())
            self.stale.discard(provider)
            self.errors.pop(provider, None)
            self.error_shown.pop(provider, None)
            self.needs_login.discard(provider)
            self.login_notes.pop(provider, None)
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
            if not points or points[-1][1] != pct or now - points[-1][0] >= HISTORY_EVERY[window]:
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
            try:
                self.icon.notify(message, title)  # the tray app's own notification (the one shown as "Python")
                return
            except Exception:
                log.warning("the tray's own notification failed; trying the system's", exc_info=True)
            notifier.send(title, message)  # backup only: sending both would show every alert twice
        except Exception:  # it must never break the update loop
            log.warning("could not show the notification", exc_info=True)

    # --- signing in -----------------------------------------------------------------------------------------------

    def _signed_out(self, provider: str) -> bool:
        """Did its last read fail because you're signed out? (Only a read can tell: each CLI says so in its own way.)"""
        return provider in self.needs_login

    def _login_pending(self, provider: str) -> bool:
        """Its sign-in window was opened a moment ago and the usage can't be read yet."""
        return provider in self.login_notes and time.monotonic() - self.login_at.get(provider, 0) < LOGIN_COOLDOWN

    def _login_note(self, provider: str) -> str | None:
        return self.login_notes.get(provider) if self._login_pending(provider) else None

    def ask_login(self, provider: str, force: bool = False) -> None:
        """Open the provider's sign-in in a terminal window when it's signed out (the menu's Options > Sign in does it
        even if we can't tell). Not again for a few minutes after, so cycling past it with the click doesn't pile up
        windows. The provider's screen says what to do, and fills in by itself once you've signed in."""
        if not force and (not self._signed_out(provider) or self._login_pending(provider)):
            return
        outcome = login.launch(provider)
        title = TITLES[provider]
        self.login_at[provider] = time.monotonic()
        if outcome == "started":
            self.login_missing.discard(provider)
            note = f"A window opened: finish signing in to {title} there. This screen fills in by itself."
            self._notify(title, "Inicia sesión en la ventana que se abrió")
        elif outcome == "missing":
            self.login_missing.add(provider)
            note = f"{title}'s CLI isn't installed. {login.INSTALL_HINTS[provider]}"
            self._notify(title, "No se encontró su CLI: instálalo para poder iniciar sesión")
        else:
            note = f"Couldn't open a terminal. Sign in to {title} by running its CLI yourself."
        log.info("sign-in for %s: %s", provider, outcome)
        self.login_notes[provider] = note
        self.errors[provider] = note
        self.error_shown.pop(provider, None)
        self.wake.set()

    def _login_action(self, provider: str):
        return lambda: self._run_in_background(self.ask_login, provider, True)

    def _error_usage(self, provider: str) -> dict | None:
        """What to put on a provider's screen when it has never been read: the reason it failed. None when there's
        nothing to say, or the screen already says it (it would only be uploaded again every cycle for nothing)."""
        message = self.errors.get(provider)
        if not message or provider in self.last_good:
            return None  # (one that has been read before is dimmed as stale instead, see _mark_stale)
        if self.error_shown.get(provider) == message and provider in self.slot and provider not in self.outdated:
            return None
        return {"title": TITLES[provider], "error": message, "now": datetime.now().astimezone(),
                "signin": self._signed_out(provider) and provider not in self.login_missing}

    def _refresh_other(self, provider: str) -> None:
        """The provider that isn't on screen: read it now and then (its alerts matter too) and keep its image fresh."""
        other = "codex" if provider == "claude" else "claude"
        # nothing (or only an out-of-date image) of it on the device yet: do it now, so the first click is instant too
        needs_image = (other not in self.slot or other in self.outdated) and not self.offline
        if not needs_image and time.monotonic() - self.last_fetch.get(other, 0) < OTHER_REFRESH:
            return
        usage = self._fetch_usage(other) or self._error_usage(other)
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
                if provider in self.errors and any(p in self.last_good for p in TITLES):
                    # it can't be read: its panel says so with dashes rather than vanishing
                    usage = {"title": TITLES[provider], "current_pct": None, "current_reset": None, "weekly_pct": None,
                             "weekly_reset": None, "now": datetime.now().astimezone()}
                    if key in STATS_VIEWS:
                        usage["activity"] = self.local_stats.get(provider)
                    panels.append(usage)
                continue
            usage, read_at = good
            usage = dict(usage, stale=True) if time.monotonic() - read_at >= STALE_SECONDS else dict(usage)
            if key in STATS_VIEWS:
                usage["activity"] = self.local_stats.get(provider)
            panels.append(usage)
        return panels

    def _refresh_local_stats(self) -> None:
        """Both providers' activity comes from their local logs (up to a month of them): count now and then, not every
        cycle, and in a thread, since the first count takes seconds. Until it's done the screens say "Counting...";
        when it is, the loop is woken to redraw."""
        since = time.monotonic() - self.local_stats_at
        complete = len(self.local_stats) == len(TITLES)
        if self._counting or since < (LOCAL_STATS_EVERY if complete else LOCAL_STATS_RETRY):
            return
        self.local_stats_at = time.monotonic()
        if BACKGROUND_STATS:
            self._counting = True
            threading.Thread(target=self._count_local_stats, daemon=True).start()
        else:
            self._count_local_stats()

    def _count_local_stats(self) -> None:
        try:
            for provider, count in usage_stats.STATS.items():
                try:
                    stats = count(time.time())
                    self.local_stats[provider] = stats if stats is not None else {}
                except Exception:
                    log.warning("could not count %s's local activity", provider, exc_info=True)
        finally:
            self._counting = False
            if BACKGROUND_STATS:
                self.wake.set()  # redraw the screen that was saying "Counting..."

    def _update_panels(self, key: str, refetch: bool = True) -> str:
        """A view of both providers: read both (so both get alerts) and upload one screen with the two of them.
        `refetch=False` redraws from the readings we already have (only an agent started or stopped)."""
        if refetch:
            self._fetch_all()
        if key in STATS_VIEWS:
            self._refresh_local_stats()
        panels = self._panels(key)
        if not panels:
            return "error"
        return self._deliver(key, {"panels": panels})

    def _fetch_all(self) -> None:
        """Read both providers at the same time (Claude's `/usage` takes ~3.5 s, Codex's ~1 s): a view of both waits
        for the slower one instead of for the two added up."""
        threads = [threading.Thread(target=self._fetch_usage, args=(provider,), daemon=True) for provider in TITLES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

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
                if key in STATS_VIEWS:
                    self._refresh_local_stats()
                usage = {"panels": self._panels(key)}
                ready = len(usage["panels"]) == len(TITLES)  # (half a view would be stored as if it were whole)
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
        if self.ip:  # the remembered view goes up first, in one request: it needs neither the search nor the file list
            self._show_uploaded(self._active_key())
        self._resolve_device()
        self._sync_with_device()
        if not self.slot.get(self._active_key()) or self._shown_failed:
            self._show_uploaded(self._active_key())  # (the address changed, or the first try didn't get through)
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
            if any(self._login_pending(p) for p in (TITLES if self.mode else [provider])):
                wait = LOGIN_POLL  # someone is signing in right now: notice it as soon as it works
            # An agent started or stopped (and nothing else changed): redraw from the readings we already have.
            redraw_only, self.agent_dirty = self.agent_dirty, False
            if self.mode:
                recent = all(self._recently_read(p) for p in TITLES)
                if self._update_panels(self.mode, refetch=not (redraw_only and recent)) == "offline":
                    wait = RETRY_SECONDS
                    self._maybe_rediscover()
                self._refresh_outdated_views()
                self.wake.wait(wait)
                continue
            if pending and pending[0] == provider and time.monotonic() - pending[2] < self.interval:
                usage = pending[1]  # read moments ago but not delivered yet: don't query again
            elif redraw_only and self._recently_read(provider):
                usage = self.last_good[provider][0]
            else:
                usage = self._fetch_usage(provider) or self._error_usage(provider)
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

    def _recently_read(self, provider: str) -> bool:
        good = self.last_good.get(provider)
        return good is not None and time.monotonic() - good[1] < self.interval

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self._watch_lock, daemon=True).start()
        threading.Thread(target=self._watch_agents, daemon=True).start()
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
    try:
        global _instance_lock  # kept for as long as the app runs: the lock goes with it
        _instance_lock = single_instance.acquire(LOCK_PATH)
    except single_instance.AlreadyRunning:
        print("The tray app is already running; not starting a second copy.", file=sys.stderr)
        return
    setup_logging()
    if sys.platform == "darwin":
        import AppKit

        # pystray creates an NSApplication for the status item. Keep it out of the Dock.
        if not AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        ):
            log.warning("could not hide Python Dock icon")
    App(args.ip, args.interval, args.animation, args.provider, args.night, args.night_brightness).run()


if __name__ == "__main__":
    main()
