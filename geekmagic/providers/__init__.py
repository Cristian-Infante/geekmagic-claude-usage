"""The providers the app can show, in the order they cycle."""

from geekmagic.providers.base import Provider
from geekmagic.providers.claude import ClaudeProvider
from geekmagic.providers.codex import CodexProvider
from geekmagic.errors import SignInNeeded, UsageError

ALL: tuple[Provider, ...] = (ClaudeProvider(), CodexProvider())
PROVIDERS: dict[str, Provider] = {p.key: p for p in ALL}  # by key
TITLES: dict[str, str] = {p.key: p.title for p in ALL}  # key -> display name
KEY_OF: dict[str, str] = {p.title: p.key for p in ALL}  # display name -> key (a reading says which provider it is by title)

__all__ = ["ALL", "KEY_OF", "PROVIDERS", "Provider", "SignInNeeded", "TITLES", "UsageError"]
