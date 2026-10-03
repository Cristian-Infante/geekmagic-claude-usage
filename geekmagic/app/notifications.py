"""Desktop notifications, and the two switches that turn them off."""

from __future__ import annotations

import logging

from geekmagic.system import notifier

log = logging.getLogger("tray")


class Notifications:
    def __init__(self) -> None:
        self.icon = None  # the tray icon, whose own notification is the one shown (set by the app once it exists)
        self.enabled = True  # every notification
        self.agent_events = True  # the ones about an agent finishing or waiting for you

    def send(self, title: str, message: str) -> None:
        log.info("notification: %s - %s", title, message)
        if not self.enabled:
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

    def restore(self, saved: dict) -> None:
        self.enabled = bool(saved.get("notify", True))
        self.agent_events = bool(saved.get("notify_done", True))

    def snapshot(self) -> dict:
        return {"notify": self.enabled, "notify_done": self.agent_events}
