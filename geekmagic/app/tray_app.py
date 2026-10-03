"""The tray app: wires the parts together and handles the icon's clicks and menu commands.

The parts, each with one job:  ViewState (what's on screen)  ·  UsageService (reading the providers)  ·  Screen (the
device and what's stored on it)  ·  Scheduler (the loop)  ·  AgentMonitor (is an agent working?)  ·  ActivityCounter
(stats from the local logs)  ·  Backlight  ·  Power (pause)  ·  SignInCoordinator  ·  Notifications  ·  Persistence.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

import pystray

from geekmagic import paths
from geekmagic.app import config
from geekmagic.app.activity import ActivityCounter
from geekmagic.app.agents import AgentMonitor
from geekmagic.app.backlight import Backlight, in_background
from geekmagic.app.icons import icon_for, make_icon
from geekmagic.app.menu import build_menu
from geekmagic.app.notifications import Notifications
from geekmagic.app.persistence import Persistence
from geekmagic.app.power import Power
from geekmagic.app.render_id import RENDER_ID
from geekmagic.app.scheduler import Scheduler
from geekmagic.app.screen import Screen
from geekmagic.app.signin import SignInCoordinator
from geekmagic.app.state_store import StateStore
from geekmagic.app.usage import AlertTracker, UsageHistory, UsageService
from geekmagic.app.viewstate import VIEW_NAMES, ViewState
from geekmagic.providers import TITLES
from geekmagic.render import views
from geekmagic.render.animations import Rotation

log = logging.getLogger("tray")


class TrayApp:
    def __init__(self, ip: str | None, interval: int, animation: str, provider: str | None = None,
                 night_hours: tuple[int, int] | None = None, night_level: int | None = None) -> None:
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.rotation = Rotation()
        self.notifications = Notifications()
        self.view = ViewState(first_run_provider=provider)
        self.persistence = Persistence(StateStore(paths.STATE_PATH))
        save = self.persistence.save
        notify = self.notifications.send

        alert_tracker, history = AlertTracker(notify, save), UsageHistory()
        self.usage = UsageService(history, alert_tracker, self.persistence.lock)
        self.agents = AgentMonitor(notify, self._agent_changed, self.stop, lambda: self.notifications.agent_events)
        self.activity = ActivityCounter(self.wake.set)
        self.screen = Screen(
            ip, animation, self.rotation, RENDER_ID,
            active_key=lambda: self.view.key, flag=lambda key, usage: self.agents.flag(key, usage, views.PANEL_VIEWS),
            notify=notify, save=save, set_title=self._set_title, tooltip=self._tooltip, wake=self.wake,
        )
        self.backlight = Backlight(self.screen, notify, save, self._refresh_menu, night_hours, night_level)
        self.power = Power(self._refresh_title_and_menu, self._lock_changed, self.wake, self.stop, save)
        self.signin = SignInCoordinator(self.usage, self.screen, notify, self.wake)
        self.usage.on_read = self.signin.on_read
        self.usage.on_failure = self._read_failed
        self.scheduler = Scheduler(
            interval, self.usage, self.screen, self.view, self.power, self.agents, self.activity, self.signin,
            self.wake, self.stop, self._tooltip, self._set_title,
        )

        self.persistence.register(
            self.view, self.notifications, self.rotation, alert_tracker, history, self.screen, self.backlight, self.power,
        )
        self.persistence.load()

        self.icon = pystray.Icon("geekmagic-usage", icon_for(self.view.key), self._tooltip(), menu=build_menu(self))
        self.notifications.icon = self.icon

    # --- what the icon says -----------------------------------------------------------------------------------------

    def _tooltip(self) -> str:
        view = VIEW_NAMES[self.view.mode] if self.view.mode else f"mostrando {TITLES[self.view.provider]}"
        notes = self.agents.tooltip_notes()
        if notes:
            view += " · " + " y ".join(notes)
        if self.power.paused:
            return f"GeekMagic: {view} (en pausa)"
        if self.power.pause_on_lock and self.power.locked:
            return f"GeekMagic: {view} (en pausa: PC bloqueado)"
        return f"GeekMagic: {view}"

    def _set_title(self, title: str) -> None:
        self.icon.title = title

    def _refresh_menu(self) -> None:
        self.icon.update_menu()

    def _refresh_title_and_menu(self) -> None:
        self.icon.title = self._tooltip()
        self.icon.update_menu()

    # --- what the parts tell us -------------------------------------------------------------------------------------

    def _agent_changed(self) -> None:
        """An agent started or stopped: redraw with (or without) the working indicator, from the readings we have."""
        self.icon.title = self._tooltip()
        self.wake.set()

    def _read_failed(self, provider: str, error: Exception) -> None:
        self.signin.on_failure(provider, error)
        if provider == self.view.provider:
            self.icon.title = f"GeekMagic: {error}"[:120]
        self.scheduler.mark_stale(provider)

    def _lock_changed(self, locked: bool) -> None:
        self.backlight.on_lock_change(locked)
        if not locked:
            in_background(self.screen.show_uploaded, self.view.key)  # put the view back

    # --- clicks and menu commands -----------------------------------------------------------------------------------

    def _switched(self, key: str) -> None:
        """Common to every switch: pin the view's last image at once and refresh it in the background."""
        self.persistence.save()  # so a restart or reconnection comes back to this view
        self.icon.title = self._tooltip()
        self.icon.update_menu()
        self.screen.note_click()  # abort any upload in flight: the device serves one request at a time
        in_background(self.screen.show_uploaded, key)
        if time.monotonic() - self.screen.last_upload.get(key, 0) > config.STALE_AFTER:
            self.wake.set()  # refresh the image we just switched to, once clicking settles

    def select(self, provider: str) -> None:
        log.info("switch -> %s", provider)
        self.view.provider = provider
        self.view.mode = None
        self.icon.icon = make_icon(provider)
        self._switched(provider)
        in_background(self.signin.ask, provider)  # (only does anything if it's signed out)

    def toggle(self) -> None:
        """Left-click: Claude <-> Codex. From a view of both it goes back to the provider you were on."""
        if self.view.mode:
            self.select(self.view.provider)
        else:
            self.select("codex" if self.view.provider == "claude" else "claude")

    def toggle_mode(self, mode: str) -> None:
        """Menu: a view of both providers at once. Turning it off returns to the last single provider."""
        if self.view.mode == mode:
            self.select(self.view.provider)
            return
        log.info("switch -> %s view", mode)
        self.view.mode = mode
        self.icon.icon = icon_for(mode)
        self._switched(mode)

    def select_action(self, provider: str):
        return lambda: self.select(provider)

    def mode_action(self, mode: str):
        return lambda: self.toggle_mode(mode)

    def sign_in_action(self, provider: str):
        return lambda: in_background(self.signin.ask, provider, True)

    def toggle_notifications(self) -> None:
        self.notifications.enabled = not self.notifications.enabled
        log.info("notifications %s", "on" if self.notifications.enabled else "off")
        self.persistence.save()
        self.icon.update_menu()

    def toggle_agent_events(self) -> None:
        self.notifications.agent_events = not self.notifications.agent_events
        log.info("agent-finished notifications %s", "on" if self.notifications.agent_events else "off")
        self.persistence.save()
        self.icon.update_menu()

    @staticmethod
    def open_logs() -> None:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(paths.LOG_PATH)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(paths.LOG_PATH)])  # in the default text editor
        else:
            subprocess.Popen(["xdg-open", str(paths.LOG_PATH)])

    def quit(self) -> None:
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    def run(self) -> None:
        threading.Thread(target=self.scheduler.run, daemon=True).start()
        threading.Thread(target=self.power.watch_lock, daemon=True).start()
        threading.Thread(target=self.agents.watch, daemon=True).start()
        self.icon.run()
