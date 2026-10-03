"""Providers that can't be read because you're signed out: opening their sign-in, and what their screens say meanwhile."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from geekmagic.app import config
from geekmagic.app.screen import Screen
from geekmagic.app.usage import UsageService
from geekmagic.providers import PROVIDERS, TITLES
from geekmagic.system import login

log = logging.getLogger("tray")


class SignInCoordinator:
    def __init__(self, usage: UsageService, screen: Screen, notify: Callable[[str, str], None],
                 wake: threading.Event) -> None:
        self.usage = usage
        self.screen = screen
        self.notify = notify
        self.wake = wake
        self.login_at: dict[str, float] = {}  # provider -> when its sign-in window was last opened (monotonic)
        self.notes: dict[str, str] = {}  # provider -> what its screen says about that window
        self.missing: set[str] = set()  # providers whose CLI isn't installed, so there's nothing to sign in to

    def pending(self, provider: str) -> bool:
        """Its sign-in window was opened a moment ago and the usage can't be read yet."""
        return provider in self.notes and time.monotonic() - self.login_at.get(provider, 0) < config.LOGIN_COOLDOWN

    def note(self, provider: str) -> str | None:
        return self.notes.get(provider) if self.pending(provider) else None

    def on_read(self, provider: str) -> None:
        """A read worked: whatever was said about signing in is over."""
        self.notes.pop(provider, None)
        self.screen.error_shown.pop(provider, None)

    def on_failure(self, provider: str, error: Exception) -> None:
        """A read failed: while a sign-in window is open the provider's screen keeps saying so."""
        note = self.note(provider)
        if note:
            self.usage.errors[provider] = note

    def ask(self, provider: str, force: bool = False) -> None:
        """Open the provider's sign-in in a terminal window when it's signed out (the menu's Options > Sign in does it
        even if we can't tell). Not again for a few minutes after, so cycling past it with the click doesn't pile up
        windows. The provider's screen says what to do, and fills in by itself once you've signed in."""
        if not force and (not self.usage.signed_out(provider) or self.pending(provider)):
            return
        outcome = login.launch(provider)
        title = TITLES[provider]
        self.login_at[provider] = time.monotonic()
        if outcome == "started":
            self.missing.discard(provider)
            note = f"A window opened: finish signing in to {title} there. This screen fills in by itself."
            self.notify(title, "Inicia sesión en la ventana que se abrió")
        elif outcome == "missing":
            self.missing.add(provider)
            note = f"{title}'s CLI isn't installed. {PROVIDERS[provider].install_hint}"
            self.notify(title, "No se encontró su CLI: instálalo para poder iniciar sesión")
        else:
            note = f"Couldn't open a terminal. Sign in to {title} by running its CLI yourself."
        log.info("sign-in for %s: %s", provider, outcome)
        self.notes[provider] = note
        self.usage.errors[provider] = note
        self.screen.error_shown.pop(provider, None)
        self.wake.set()

    def error_usage(self, provider: str) -> dict | None:
        """What to put on a provider's screen when it has never been read: the reason it failed. None when there's
        nothing to say, or the screen already says it (it would only be uploaded again every cycle for nothing)."""
        message = self.usage.errors.get(provider)
        if not message or provider in self.usage.last_good:
            return None  # (one that has been read before is dimmed as stale instead)
        if (self.screen.error_shown.get(provider) == message and provider in self.screen.slots
                and provider not in self.screen.outdated):
            return None
        return {"title": TITLES[provider], "error": message, "now": datetime.now().astimezone(),
                "signin": self.usage.signed_out(provider) and provider not in self.missing}
