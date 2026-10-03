"""Start the tray app automatically at login: Task Scheduler (Windows), LaunchAgent (macOS), autostart (Linux).

Used through tray.py:
    python tray.py --ip 192.168.1.18 --install-startup
    python tray.py --uninstall-startup
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from geekmagic.paths import TRAY_SCRIPT

TASK_NAME = "GeekMagicClaude"  # Windows scheduled task
LABEL = "com.geekmagic.claude-usage"  # macOS LaunchAgent label
DESKTOP_FILE = "geekmagic-claude-usage.desktop"  # Linux autostart entry

# launchd doesn't load your shell's PATH, so `claude` / `codex` in these folders would otherwise be missed.
MAC_PATH = ":".join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", str(Path.home() / ".local" / "bin")])


def _command(script: Path, tray_args: list[str]) -> list[str]:
    python = Path(sys.executable)
    if sys.platform == "win32":  # pythonw = no console window
        windowless = python.with_name("pythonw.exe")
        python = windowless if windowless.exists() else python
    return [str(python), str(script), *tray_args]


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _powershell(script: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def _install_windows(command: list[str], workdir: Path) -> str:
    _powershell(f"""
$act = New-ScheduledTaskAction -Execute {_ps_quote(command[0])} -Argument {_ps_quote(subprocess.list2cmdline(command[1:]))} -WorkingDirectory {_ps_quote(str(workdir))}
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName {TASK_NAME} -Action $act -Trigger $trg -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName {TASK_NAME}
""")
    return f"Scheduled task '{TASK_NAME}' created and started (runs at every logon)."


def _uninstall_windows() -> str:
    _powershell(f"""
Stop-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName {TASK_NAME} -Confirm:$false -ErrorAction SilentlyContinue
""")
    return f"Scheduled task '{TASK_NAME}' removed."


def mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_mac_plist(command: list[str], workdir: Path) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, but not after "Salir"
        "EnvironmentVariables": {"PATH": MAC_PATH},
    })


def _install_mac(command: list[str], workdir: Path) -> str:
    path = mac_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_mac_plist(command, workdir))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)  # reload if already loaded
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    return f"LaunchAgent {path} loaded (runs at every login)."


def _uninstall_mac() -> str:
    path = mac_plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
    return f"LaunchAgent {path} removed."


def linux_desktop_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "autostart" / DESKTOP_FILE


def build_desktop_entry(command: list[str], workdir: Path) -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=GeekMagic Claude/Codex usage\n"
        f"Exec={shlex.join(command)}\nPath={workdir}\nX-GNOME-Autostart-enabled=true\n"
    )


def _install_linux(command: list[str], workdir: Path) -> str:
    path = linux_desktop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_desktop_entry(command, workdir), encoding="utf-8")
    return f"Autostart entry {path} created (runs at your next login)."


def _uninstall_linux() -> str:
    linux_desktop_path().unlink(missing_ok=True)
    return "Autostart entry removed."


def install(tray_args: list[str]) -> str:
    script = TRAY_SCRIPT
    command, workdir = _command(script, tray_args), script.parent
    if sys.platform == "win32":
        return _install_windows(command, workdir)
    if sys.platform == "darwin":
        return _install_mac(command, workdir)
    return _install_linux(command, workdir)


def uninstall() -> str:
    if sys.platform == "win32":
        return _uninstall_windows()
    if sys.platform == "darwin":
        return _uninstall_mac()
    return _uninstall_linux()
