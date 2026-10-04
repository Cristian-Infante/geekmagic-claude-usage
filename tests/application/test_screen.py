"""The screen: finding the device on the network, delivering pictures to it, images drawn by older code, and a click
never queuing behind an upload. The device, the network and the renderers are faked out."""
import json
import time
import unittest

from geekmagic.application import config
from geekmagic.core.views import SPLIT
from tests.support import AppTestCase, capture_screen, fake_fetchers, usage


class FindingTheDeviceTests(AppTestCase):
    def test_rediscovers_a_device_that_moved_to_another_ip(self):
        app = self.make("192.168.1.50")
        app.screen.offline, app.screen.offline_since = True, time.monotonic() - config.REDISCOVER_AFTER - 1
        self.locator.found = "192.168.1.20"
        app.screen.maybe_rediscover()
        self.assertEqual(self.locator.searches, ["192.168.1.50"], "it looks for the device, preferring the address it knows")
        self.assertEqual(app.screen.ip, "192.168.1.20")
        self.assertEqual(self.notes[-1][0], "Pantalla encontrada")
        self.assertEqual(self.make(None).screen.ip, "192.168.1.20")  # remembered for the next start

    def test_doesnt_scan_right_away_or_too_often(self):
        app = self.make("192.168.1.50")
        app.screen.offline, app.screen.offline_since = True, time.monotonic() - 5  # just went offline
        self.locator.found = None
        app.screen.maybe_rediscover()
        self.assertEqual(self.locator.searches, [])
        app.screen.offline_since = time.monotonic() - config.REDISCOVER_AFTER - 1
        app.screen.maybe_rediscover()  # due: scans
        app.screen.maybe_rediscover()  # but not again straight away
        self.assertEqual(len(self.locator.searches), 1)

    def test_keeps_the_address_when_the_device_is_still_there(self):
        app = self.make("192.168.1.50")
        app.screen.offline, app.screen.offline_since = True, time.monotonic() - 1000
        self.locator.found = "192.168.1.50"
        app.screen.maybe_rediscover()
        self.assertEqual(app.screen.ip, "192.168.1.50")
        self.assertEqual(self.notes, [])

    def test_no_address_at_all_scans_until_it_finds_the_device(self):
        app = self.make(None)
        self.assertEqual(app.screen.ip, "")
        self.locator.found = None
        app.screen.maybe_rediscover()
        self.assertEqual(app.screen.ip, "")
        app.screen.last_scan = time.monotonic() - config.DISCOVER_RETRY - 1
        self.locator.found = "192.168.1.20"
        app.screen.maybe_rediscover()
        self.assertEqual(app.screen.ip, "192.168.1.20")

    def test_startup_prefers_given_ip_then_remembered_then_scans(self):
        self.store.path.write_text('{"ip": "192.168.1.99"}')
        self.locator.probe = lambda host, timeout=2.0: host == "192.168.1.50"
        app = self.make("192.168.1.50")
        app.screen.resolve_device()
        self.assertEqual(app.screen.ip, "192.168.1.50")
        self.locator.probe = lambda host, timeout=2.0: host == "192.168.1.99"
        app = self.make("192.168.1.50")  # --ip is dead, but the remembered address answers
        app.screen.resolve_device()
        self.assertEqual(app.screen.ip, "192.168.1.99")
        self.locator.probe = lambda host, timeout=2.0: False
        self.locator.found = "192.168.1.20"
        app = self.make("192.168.1.50")
        app.screen.resolve_device()
        self.assertEqual(app.screen.ip, "192.168.1.20")


class ClicksAndUploadsTests(AppTestCase):
    def test_no_upload_starts_right_after_a_click(self):
        app = self.make()
        with capture_screen() as cap:
            app.screen.last_click = time.monotonic()  # the user just clicked
            self.assertFalse(app.screen.upload("claude", usage()))
            self.assertEqual(cap.uploads, [])
            self.assertTrue(app.wake.is_set(), "the worker must come back for it once clicking settles")
            app.screen.last_click = time.monotonic() - config.SETTLE_SECONDS - 1
            self.assertTrue(app.screen.upload("claude", usage()))
            self.assertEqual(len(cap.uploads), 1)

    def test_a_click_leaves_the_device_free_for_its_own_request(self):
        """The log showed: click -> upload cancelled -> *another* upload started 13 ms later -> click waited 4 s."""
        app = self.make()
        app.screen.slots["split"] = "a"
        now = time.monotonic()
        app.usage.last_good = {"claude": (usage(title="Claude"), now), "codex": (usage(title="Codex"), now)}
        app.screen.outdated = {"claude", "codex", "split"}
        with capture_screen() as cap, fake_fetchers(claude=lambda: usage(), codex=lambda: usage(title="Codex")):
            app.toggle_mode(SPLIT)  # the click
            app.scheduler.refresh_other("claude")  # what the worker does next in its cycle
            app.scheduler.refresh_outdated_views()
        self.assertEqual(cap.uploads, [], "nothing may take the device away from the click")


class DeliveryTests(AppTestCase):
    def test_delivery_without_an_address_reports_offline(self):
        app = self.make(None)
        self.assertEqual(app.screen.deliver("claude", usage()), "offline")
        self.assertTrue(app.screen.offline)


class ImagesDrawnByOlderCodeTests(AppTestCase):
    def test_images_drawn_by_older_code_are_redrawn_once_in_the_background(self):
        self.store.path.write_text(json.dumps({
            "slots": {"claude": "a", "codex": "a", "split": "a"}, "render": "an-older-version",
        }))
        app = self.make()
        self.assertEqual(app.screen.outdated, {"claude", "codex", "split"})
        app.usage.last_good = {"claude": (usage(), time.monotonic()), "codex": (usage(title="Codex"), time.monotonic())}
        with capture_screen() as cap:
            app.scheduler.refresh_outdated_views()  # claude is on screen: the normal cycle redraws it, so these two are left
            app.scheduler.refresh_outdated_views()
            app.scheduler.refresh_outdated_views()
            self.assertEqual(cap.uploads, ["codex-usage-b.gif", "split-usage-b.gif"])  # one per call, then nothing left
            self.assertEqual(cap.shown, [], "redrawn behind the scenes: the screen isn't touched")
            self.assertEqual(app.screen.outdated, {"claude"})
            self.assertIsNone(json.loads(self.store.path.read_text())["render"])
            app.screen.upload("claude", usage())  # what the regular cycle does
        self.assertEqual(app.screen.outdated, set())
        self.assertEqual(json.loads(self.store.path.read_text())["render"], app.renderer.version)
        self.assertEqual(self.make().screen.outdated, set(), "up to date: nothing to redo after the next restart")

    def test_nothing_is_redrawn_that_has_no_reading_yet(self):
        self.store.path.write_text(json.dumps({"slots": {"codex": "a", "split": "a"}, "render": "old"}))
        app = self.make()
        with capture_screen() as cap:
            app.scheduler.refresh_outdated_views()
        self.assertEqual(cap.uploads, [])
        self.assertEqual((cap.single, cap.panels), ([], []))


if __name__ == "__main__":
    unittest.main()
