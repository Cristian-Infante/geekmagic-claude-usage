"""Reading the providers and marking the data stale when it can't be refreshed."""
import time

from geekmagic.app import config
from geekmagic.errors import UsageError
from tests.support import AppTestCase, capture_screen, failing, fake_fetchers, usage


class UsageServiceTests(AppTestCase):
    def test_stale_image_after_a_few_minutes_without_fresh_data(self):
        app = self.make()
        with capture_screen() as cap:
            app.usage.last_good["claude"] = (usage(current=40), time.monotonic() - 60)
            app.scheduler.mark_stale("claude")
            self.assertEqual(cap.single, [], "only a minute old: still fine")

            app.usage.last_good["claude"] = (usage(current=40), time.monotonic() - config.STALE_SECONDS - 5)
            app.scheduler.mark_stale("claude")
            self.assertEqual(len(cap.single), 1)
            shown = cap.single[0][0]
            self.assertTrue(shown.stale)
            self.assertEqual(shown.current.pct, 40)  # the last known numbers, not made-up ones

            app.scheduler.mark_stale("claude")
            self.assertEqual(len(cap.single), 1, "already marked: don't re-upload every cycle")

            with fake_fetchers(claude=lambda: usage(current=41)):
                self.assertEqual(app.usage.fetch("claude").current.pct, 41)
            self.assertNotIn("claude", app.usage.stale)  # fresh data clears it

    def test_failing_fetch_marks_stale_and_returns_nothing(self):
        app = self.make()
        app.usage.last_good["claude"] = (usage(current=40), time.monotonic() - config.STALE_SECONDS - 5)
        with fake_fetchers(claude=failing(UsageError("claude not logged in"))), capture_screen() as cap:
            self.assertIsNone(app.usage.fetch("claude"))
        self.assertEqual(len(cap.single), 1)
        self.assertTrue(cap.single[0][0].stale)

    def test_no_stale_upload_while_the_device_is_unreachable(self):
        app = self.make()
        app.screen.offline = True
        app.usage.last_good["claude"] = (usage(current=40), time.monotonic() - 1000)
        with capture_screen() as cap:
            app.scheduler.mark_stale("claude")
        self.assertEqual((cap.single, cap.uploads), ([], []))
