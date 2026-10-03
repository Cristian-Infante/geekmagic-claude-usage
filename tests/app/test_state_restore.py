"""Coming back to the view you left: after a restart, after a damaged state file, and when the device returns."""
import json
import unittest
from unittest.mock import patch

from geekmagic.app.render_id import RENDER_ID
from geekmagic.app.viewstate import SPLIT
from geekmagic.device import discovery
from geekmagic.device.client import GeekMagicDevice
from tests.support import AppTestCase, capture_screen, usage


class ViewRestoredTests(AppTestCase):
    def test_the_view_you_left_is_restored_after_a_restart(self):
        with capture_screen():
            first = self.make()
            self.assertEqual((first.view.split, first.view.provider), (False, "claude"))  # first ever run
            first.select("codex")
            again = self.make()
            self.assertEqual((again.view.split, again.view.provider, again.view.key), (False, "codex", "codex"))
            self.assertIn("Codex", again._tooltip())

            again.toggle_mode(SPLIT)
            back = self.make()
            self.assertEqual((back.view.split, back.view.key), (True, "split"))
            self.assertEqual(back.view.provider, "codex")  # what leaving the split view returns to
            self.assertIn("dividida", back._tooltip())
            back.toggle()
            self.assertEqual((back.view.split, back.view.provider), (False, "codex"))

            back.select("claude")
            self.assertEqual(self.make().view.key, "claude")

    def test_provider_argument_is_only_the_first_runs_choice(self):
        first_run = self.make(provider="codex")  # nothing remembered yet
        self.assertEqual(first_run.view.provider, "codex")
        with capture_screen():
            self.make().select("claude")
        self.assertEqual(self.make(provider="codex").view.provider, "claude")  # the remembered one wins

    def test_startup_puts_the_remembered_view_back_on_screen(self):
        for view, expected in (("split", "split-usage-b.gif"), ("codex", "codex-usage-b.gif")):
            (self.tmp / "state.json").write_text(json.dumps({
                "slots": {"claude": "a", "codex": "b", "split": "b"}, "view": view, "provider": "codex",
                "render": RENDER_ID,
            }))
            app = self.make()
            app.stop.set()  # run just the start-up part of the worker
            with capture_screen() as cap, \
                    patch.object(discovery, "probe", return_value=True), \
                    patch.object(GeekMagicDevice, "list_images",
                                 return_value={"claude-usage-a.gif", "codex-usage-b.gif", "split-usage-b.gif"}):
                app.scheduler.run()
            self.assertEqual(cap.shown, [expected], view)

    def test_a_garbled_state_file_falls_back_to_the_defaults(self):
        (self.tmp / "state.json").write_text(json.dumps({"view": "nonsense", "provider": 7, "slots": "x"}))
        app = self.make()
        self.assertEqual((app.view.split, app.view.provider), (False, "claude"))


class DeviceComesBackTests(AppTestCase):
    def test_when_the_device_comes_back_the_remembered_view_is_pinned_before_the_upload(self):
        app = self.make()
        app.view.provider, app.screen.slots["codex"] = "codex", "b"
        app.screen.offline = True
        events = []
        with capture_screen(), \
                patch.object(GeekMagicDevice, "show_image",  # (plain mocks: capture_screen's own are already in place)
                             side_effect=lambda name: events.append(("show", name))), \
                patch.object(GeekMagicDevice, "upload", side_effect=lambda *a, **k: events.append(("upload",))):
            outcome = app.screen.deliver("codex", usage(title="Codex"))
        self.assertEqual(outcome, "ok")
        self.assertEqual(events[0], ("show", "codex-usage-b.gif"))  # the view is back on screen right away
        self.assertIn(("upload",), events)
        self.assertFalse(app.screen.offline)

    def test_while_the_device_is_still_off_one_small_request_is_all_it_costs(self):
        app = self.make()
        app.screen.slots["claude"] = "a"
        app.screen.offline = True
        with capture_screen() as cap, patch.object(GeekMagicDevice, "show_image", side_effect=OSError("timed out")):
            self.assertEqual(app.screen.deliver("claude", usage()), "offline")
        self.assertEqual(cap.uploads, [])
        self.assertTrue(app.screen.offline)


if __name__ == "__main__":
    unittest.main()
