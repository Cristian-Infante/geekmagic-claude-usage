"""The Ui port, as a system-tray icon (pystray): Windows/Linux tray, macOS menu bar."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pystray

from geekmagic.adapters.tray.icons import icon_for
from geekmagic.adapters.tray.menu import build_menu


class TrayUi:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.icon: pystray.Icon | None = None
        self.titles: dict[str, str] = {}

    def attach(self, controller) -> None:
        """Build the icon and its menu for `controller` (which already exists: the menu shows and changes its state)."""
        self.titles = controller.providers.titles
        self.icon = pystray.Icon("geekmagic-usage", icon_for(controller.view.key, self.titles), controller.tooltip(),
                                 menu=build_menu(controller, self.open_logs))

    # --- the Ui port --------------------------------------------------------------------------------------------

    def set_title(self, title: str) -> None:
        if self.icon is not None:
            self.icon.title = title

    def set_icon(self, key: str) -> None:
        if self.icon is not None:
            self.icon.icon = icon_for(key, self.titles)

    def refresh_menu(self) -> None:
        if self.icon is not None:
            self.icon.update_menu()

    def send(self, title: str, message: str) -> None:
        """The tray's own notification (the one Windows shows as "Python")."""
        self.icon.notify(message, title)

    def run(self) -> None:
        self.icon.run()

    def stop(self) -> None:
        self.icon.stop()

    # --- the menu's own commands --------------------------------------------------------------------------------

    def open_logs(self) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(self.log_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(self.log_path)])  # in the default text editor
        else:
            subprocess.Popen(["xdg-open", str(self.log_path)])
