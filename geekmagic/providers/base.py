"""What every provider (Claude, Codex...) is to the rest of the app.

A provider bundles everything that is specific to one AI tool: how to read its usage, where its local logs are, how to
tell whether its agent is busy, and how to sign in to it. The app only talks to this interface, so supporting another
provider is one new module that implements it and an entry in geekmagic.providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    key: str  # "claude": what is stored on disk and used as a file name
    title: str  # "Claude": what the screens and notifications call it
    login_args: tuple[str, ...]  # the arguments of its CLI that start the sign-in
    install_hint: str  # what to tell someone whose CLI isn't installed

    @abstractmethod
    def fetch(self) -> dict:
        """Its current usage (see geekmagic.render for the shape). Raises UsageError, or SignInNeeded when signing in
        again is what's needed. Never spends a token."""

    @abstractmethod
    def find_cli(self) -> str | None:
        """Where its command-line tool is installed, or None."""

    @abstractmethod
    def local_stats(self, now_epoch: float) -> dict | None:
        """Its activity counted from its local logs (see geekmagic.insights.usage_stats), None without logs."""

    @abstractmethod
    def agent_state(self, now_epoch: float) -> dict:
        """Whether its agent is working or waiting for you right now (see geekmagic.insights.agent_activity)."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.key}>"
