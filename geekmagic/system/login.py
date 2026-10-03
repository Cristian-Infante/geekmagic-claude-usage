"""Open a provider's sign-in in a terminal window, for when its usage can't be read because you're signed out.

Each provider's own CLI does the sign-in (a browser opens and you approve it there); this only starts it, in a visible
terminal, since the CLIs ask questions. Once it's done the next read works and the screen fills in by itself.

    Claude  `claude auth login`        Codex  `codex login`
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import geekmagic_claude as g

TITLES = {"claude": "Claude", "codex": "Codex"}
ARGS = {"claude": ["auth", "login"], "codex": ["login"]}
INSTALL_HINTS = {
    "claude": "Install Claude Code (https://claude.ai/code), then sign in.",
    "codex": "Install the Codex CLI, then run `codex login`.",
}
LINUX_TERMINALS = (
    ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]), ("kitty", []), ("alacritty", ["-e"]), ("xterm", ["-e"]),
)


def executable(provider: str) -> str | None:
    """Where the provider's CLI is (the usual install folders are searched too, as a login task's PATH is short)."""
    if provider == "codex":
        return g._find_codex()
    return g._which("claude")


def terminal_command(argv: list[str], title: str, platform: str | None = None) -> list[str] | str | None:
    """What to run to open a terminal window that runs `argv` and stays open, or None if no terminal can be found.
    (Windows gets one string: cmd.exe wants the command line exactly as written.)"""
    platform = platform or sys.platform
    if platform == "win32":
        # `cmd /k ""C:\\a b\\x.exe" arg"`: cmd drops the outer pair of quotes, so a path with spaces survives
        return f'cmd.exe /c start "{title}" cmd.exe /k "{subprocess.list2cmdline(argv)}"'
    if platform == "darwin":
        script = " ".join(shlex.quote(a) for a in argv).replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", 'tell application "Terminal" to activate',
                "-e", f'tell application "Terminal" to do script "{script}"']
    for terminal, flags in LINUX_TERMINALS:
        if shutil.which(terminal):
            return [terminal, *flags, *argv]
    return None


def launch(provider: str) -> str:
    """Start `provider`'s sign-in in a new terminal window: "started", "missing" (its CLI isn't installed) or
    "failed" (no terminal to open it in). `GEEKMAGIC_NO_LOGIN=1` turns it off (the tests use it: no test may open a
    window)."""
    if os.environ.get("GEEKMAGIC_NO_LOGIN"):
        return "failed"
    path = executable(provider)
    if not path:
        return "missing"
    command = terminal_command([path, *ARGS[provider]], f"{TITLES[provider]} sign-in")
    if not command:
        return "failed"
    try:
        subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ})
    except OSError:
        return "failed"
    return "started"
