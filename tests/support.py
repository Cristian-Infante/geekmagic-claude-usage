"""Helpers shared by the tests of the tray app: a fully faked app (no real device, providers, notifications or state file),
readings to feed it, and a recorder for what it renders and sends to the device."""

from __future__ import annotations

import logging
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    from geekmagic.app.tray_app import TrayApp
except Exception as e:  # pystray needs a desktop session
    raise unittest.SkipTest(f"the tray app can't be imported here: {e}")

from geekmagic import paths
from geekmagic.app import config
from geekmagic.device.client import GeekMagicDevice
from geekmagic.providers import PROVIDERS
from geekmagic.render import views
from geekmagic.render.views import Rendered
from geekmagic.system import notifier


def usage(current=10.0, weekly=5.0, title="Claude", **extra) -> dict:
    """A provider's reading, as the screens and the app take it."""
    now = datetime.now().astimezone()
    return {"title": title, "current_pct": current, "current_reset": now + timedelta(hours=2),
            "weekly_pct": weekly, "weekly_reset": now + timedelta(days=3), "now": now, **extra}


def failing(error: Exception):
    """A reader that raises `error` (for fake_fetchers)."""
    def read():
        raise error
    return read


@contextmanager
def fake_fetchers(**readers):
    """Replace what each provider reads: `fake_fetchers(claude=lambda: usage(...), codex=failing(UsageError("...")))`."""
    patches = [patch.object(PROVIDERS[key], "fetch", side_effect=reader) for key, reader in readers.items()]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


class Capture:
    """What the app rendered and sent to the device while a `capture_screen` block was open."""

    def __init__(self) -> None:
        self.single: list[tuple[dict, str]] = []  # (reading, animation asked for) of each single-provider screen
        self.errors: list[dict] = []  # each "can't read it" screen
        self.panels: list[tuple[str, list[dict], str]] = []  # (view, the readings, animation asked for) of each view of two
        self.uploads: list[str] = []  # file names uploaded to the device
        self.shown: list[str] = []  # file names pinned on its screen


@contextmanager
def capture_screen():
    """Render nothing for real and send nothing: record what would have been drawn and uploaded."""
    cap = Capture()
    patches = [
        patch.object(views.SINGLE, "render", side_effect=lambda u, animation, rotation: (cap.single.append((u, animation)), Rendered(b"GIF89a", [animation]))[1]),
        patch.object(views.ERROR, "render", side_effect=lambda u, *a, **k: (cap.errors.append(u), Rendered(b"GIF89a"))[1]),
        patch.object(GeekMagicDevice, "upload", autospec=True, side_effect=lambda self, gif, filename, *a, **k: cap.uploads.append(filename)),
        patch.object(GeekMagicDevice, "show_image", autospec=True, side_effect=lambda self, filename: cap.shown.append(filename)),
    ]
    for key, view in views.PANEL_VIEWS.items():
        patches.append(patch.object(
            view, "render", side_effect=lambda panels, animation="idle", rotation=None, key=key:
            (cap.panels.append((key, panels, animation)), Rendered(b"GIF89a", [animation]))[1]))
    for p in patches:
        p.start()
    try:
        yield cap
    finally:
        for p in patches:
            p.stop()


class AppTestCase(unittest.TestCase):
    """A tray app with the device, the network and the providers faked out. `self.notes` are the notifications shown
    (title, message); `self.backups` those the system route had to send instead."""

    _alive: list = []  # pystray registers a Windows window class per icon object: keep them from being recycled

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)  # the tests provoke errors on purpose; keep their log lines off the console
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = Path(tempfile.mkdtemp())
        self.notes: list[tuple[str, str]] = []
        self.backups: list[tuple[str, str]] = []
        patches = [
            patch.object(paths, "STATE_PATH", self.tmp / "state.json"),
            patch.object(GeekMagicDevice, "list_images", return_value=set()),
            patch.object(GeekMagicDevice, "delete_image"),
            patch.object(config, "BACKGROUND_STATS", False),  # count the activity inline so tests are deterministic
            patch.object(notifier, "send", side_effect=lambda title, message: self.backups.append((title, message)) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make(self, ip: str = "192.168.1.50", provider: str | None = None, *, night_hours=None, night_level=None) -> TrayApp:
        app = TrayApp(ip, 30, "idle", provider, night_hours, night_level)
        self._alive.append(app)
        app.icon.notify = lambda message, title=None: self.notes.append((title, message))
        app.rotation.clear()
        return app
