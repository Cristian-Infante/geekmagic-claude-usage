"""Tray behaviour with the device, the network and the providers faked out."""
import json
import logging
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    import tray
except Exception as e:  # pystray needs a desktop session
    raise unittest.SkipTest(f"tray can't be imported here: {e}")

import discover
import geekmagic_claude as g


def usage(current=10.0, weekly=5.0, title="Claude"):
    now = datetime.now().astimezone()
    return {"title": title, "current_pct": current, "current_reset": now + timedelta(hours=2),
            "weekly_pct": weekly, "weekly_reset": now + timedelta(days=3), "now": now}


class TrayLogicTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)  # the tests provoke errors on purpose; keep their log lines off the console
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            patch.object(tray, "STATE_PATH", self.tmp / "state.json"),
            patch.object(g, "list_images", return_value=set()),
            patch.object(g, "delete_image"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notes = []
        g._recent_animations.clear()

    _alive = []  # pystray registers a Windows window class per icon object; keep them from being recycled

    def make(self, ip="192.168.1.50", provider=None):
        app = tray.App(ip, 30, "idle", provider)
        self._alive.append(app)
        app.icon.notify = lambda message, title=None: self.notes.append((title, message))
        return app

    # --- alerts ---------------------------------------------------------------------------------------

    def test_notifies_when_thresholds_are_crossed_and_on_reset(self):
        app = self.make()
        for pct in (50, 82, 83, 96, 97, 3):
            app._check_alerts("claude", usage(current=pct))
        messages = [m for _, m in self.notes]
        self.assertEqual(len(messages), 3, messages)
        self.assertIn("80 %", messages[0])
        self.assertIn("95 %", messages[1])
        self.assertIn("reinició", messages[2])
        self.assertTrue(all(title == "Claude" for title, _ in self.notes))

    def test_weekly_and_the_other_provider_are_tracked_separately(self):
        app = self.make()
        app._check_alerts("claude", usage(current=10, weekly=85))
        app._check_alerts("codex", usage(current=82, weekly=5, title="Codex"))
        self.assertEqual([t for t, _ in self.notes], ["Claude", "Codex"])
        self.assertIn("semana", self.notes[0][1])
        self.assertIn("sesión", self.notes[1][1])

    def test_alerts_that_already_fired_dont_fire_again_after_a_restart(self):
        first = self.make()
        first._check_alerts("claude", usage(current=85))
        self.assertEqual(len(self.notes), 1)
        second = self.make()  # the app restarts: state comes back from disk
        second._check_alerts("claude", usage(current=86))
        self.assertEqual(len(self.notes), 1)

    def test_notifications_can_be_switched_off_but_state_keeps_tracking(self):
        app = self.make()
        app.toggle_notifications()
        self.assertFalse(app.notify_enabled)
        app._check_alerts("claude", usage(current=96))
        self.assertEqual(self.notes, [])
        self.assertEqual(app.alerts["claude/current"]["level"], 2)
        self.assertFalse(self.make().notify_enabled)  # the choice is remembered too

    def test_a_failing_notification_backend_never_breaks_the_loop(self):
        app = self.make()
        app.icon.notify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no toast support"))
        app._check_alerts("claude", usage(current=96))  # must not raise

    # --- stale data -----------------------------------------------------------------------------------

    def test_stale_image_after_a_few_minutes_without_fresh_data(self):
        app = self.make()
        uploaded = []
        with patch.object(g, "push_usage", side_effect=lambda ip, u, *a, **k: uploaded.append(u)), \
                patch.object(g, "show_image"):
            app.last_good["claude"] = (usage(current=40), time.monotonic() - 60)
            app._mark_stale("claude")
            self.assertEqual(uploaded, [], "only a minute old: still fine")

            app.last_good["claude"] = (usage(current=40), time.monotonic() - tray.STALE_SECONDS - 5)
            app._mark_stale("claude")
            self.assertEqual(len(uploaded), 1)
            self.assertTrue(uploaded[0]["stale"])
            self.assertEqual(uploaded[0]["current_pct"], 40)  # the last known numbers, not made-up ones

            app._mark_stale("claude")
            self.assertEqual(len(uploaded), 1, "already marked: don't re-upload every cycle")

            with patch.dict(g.PROVIDERS, {"claude": lambda: usage(current=41)}):
                self.assertEqual(app._fetch_usage("claude")["current_pct"], 41)
            self.assertNotIn("claude", app.stale)  # fresh data clears it

    def test_failing_fetch_marks_stale_and_returns_nothing(self):
        app = self.make()
        app.last_good["claude"] = (usage(current=40), time.monotonic() - tray.STALE_SECONDS - 5)
        uploaded = []

        def boom():
            raise g.UsageError("claude not logged in")

        with patch.dict(g.PROVIDERS, {"claude": boom}), \
                patch.object(g, "push_usage", side_effect=lambda ip, u, *a, **k: uploaded.append(u)), \
                patch.object(g, "show_image"):
            self.assertIsNone(app._fetch_usage("claude"))
        self.assertEqual(len(uploaded), 1)
        self.assertTrue(uploaded[0]["stale"])

    def test_no_stale_upload_while_the_device_is_unreachable(self):
        app = self.make()
        app.offline = True
        app.last_good["claude"] = (usage(current=40), time.monotonic() - 1000)
        with patch.object(g, "push_usage") as push:
            app._mark_stale("claude")
        push.assert_not_called()

    # --- finding the device ---------------------------------------------------------------------------

    def test_rediscovers_a_device_that_moved_to_another_ip(self):
        app = self.make("192.168.1.50")
        app.offline, app.offline_since = True, time.monotonic() - tray.REDISCOVER_AFTER - 1
        with patch.object(discover, "find_device", return_value="192.168.1.77") as find:
            app._maybe_rediscover()
            find.assert_called_once_with(prefer="192.168.1.50")
        self.assertEqual(app.ip, "192.168.1.77")
        self.assertEqual(self.notes[-1][0], "Pantalla encontrada")
        self.assertEqual(self.make(None).ip, "192.168.1.77")  # remembered for the next start

    def test_doesnt_scan_right_away_or_too_often(self):
        app = self.make("192.168.1.50")
        app.offline, app.offline_since = True, time.monotonic() - 5  # just went offline
        with patch.object(discover, "find_device", return_value=None) as find:
            app._maybe_rediscover()
            app.offline_since = time.monotonic() - tray.REDISCOVER_AFTER - 1
            app._maybe_rediscover()  # due: scans
            app._maybe_rediscover()  # but not again straight away
        self.assertEqual(find.call_count, 1)

    def test_keeps_the_address_when_the_device_is_still_there(self):
        app = self.make("192.168.1.50")
        app.offline, app.offline_since = True, time.monotonic() - 1000
        with patch.object(discover, "find_device", return_value="192.168.1.50"):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "192.168.1.50")
        self.assertEqual(self.notes, [])

    def test_no_address_at_all_scans_until_it_finds_the_device(self):
        app = self.make(None)
        self.assertEqual(app.ip, "")
        with patch.object(discover, "find_device", return_value=None):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "")
        app.last_scan = time.monotonic() - tray.DISCOVER_RETRY - 1
        with patch.object(discover, "find_device", return_value="192.168.1.77"):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "192.168.1.77")

    def test_startup_prefers_given_ip_then_remembered_then_scans(self):
        (self.tmp / "state.json").write_text('{"ip": "192.168.1.99"}')
        with patch.object(discover, "probe", side_effect=lambda ip, t=0: ip == "192.168.1.50"):
            app = self.make("192.168.1.50")
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.50")
        with patch.object(discover, "probe", side_effect=lambda ip, t=0: ip == "192.168.1.99"):
            app = self.make("192.168.1.50")  # --ip is dead, but the remembered address answers
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.99")
        with patch.object(discover, "probe", return_value=False), \
                patch.object(discover, "find_device", return_value="192.168.1.77"):
            app = self.make("192.168.1.50")
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.77")

    # --- split view -----------------------------------------------------------------------------------

    def test_split_view_is_only_turned_on_from_the_menu_and_left_click_leaves_it(self):
        app = self.make()
        with patch.object(g, "show_image"):
            self.assertEqual(app._active_key(), "claude")
            app.toggle()  # the click only ever alternates Claude <-> Codex
            self.assertEqual((app.split, app._active_key()), (False, "codex"))
            app.toggle_split()
            self.assertEqual((app.split, app._active_key()), (True, "split"))
            self.assertIn("dividida", app._tooltip())
            app.toggle()  # from the split view the click goes back to where you were
            self.assertEqual((app.split, app._active_key()), (False, "codex"))
            app.toggle_split()
            app.toggle_split()  # the menu item toggles it off again
            self.assertEqual((app.split, app._active_key()), (False, "codex"))
            app.toggle_split()
            app.select("claude")  # picking a provider in the menu leaves it too
            self.assertEqual((app.split, app._active_key()), (False, "claude"))

    def test_menu_has_the_split_item_next_to_the_other_buttons(self):
        app = self.make()
        names = [str(item) for item in app.icon.menu.items]
        self.assertIn("Vista dividida", names)
        self.assertLess(names.index("Codex"), names.index("Vista dividida"))
        self.assertLess(names.index("Vista dividida"), names.index("Ver logs"))

    def test_split_update_reads_both_providers_and_uploads_one_screen(self):
        app = self.make()
        app.split = True
        uploaded, shown = [], []
        with patch.dict(g.PROVIDERS, {"claude": lambda: usage(82, 5), "codex": lambda: usage(10, 96, title="Codex")}), \
                patch.object(g, "push_split", side_effect=lambda ip, panels, name, **k: uploaded.append((panels, name))), \
                patch.object(g, "show_image", side_effect=lambda ip, name: shown.append(name)):
            self.assertEqual(app._update_split(), "ok")
        (panels, name), = uploaded
        self.assertEqual([p["title"] for p in panels], ["Claude", "Codex"])
        self.assertEqual(name, "split-usage-a.gif")
        self.assertEqual(shown, ["split-usage-a.gif"])
        self.assertEqual(app.slot["split"], "a")
        self.assertEqual(len(self.notes), 2, "alerts for both providers fire while the split view is on")

    def test_split_update_dims_only_the_provider_that_stopped_answering(self):
        app = self.make()
        app.split = True
        old = time.monotonic() - tray.STALE_SECONDS - 5
        app.last_good["codex"] = (usage(10, 20, title="Codex"), old)
        uploaded = []

        def codex_down():
            raise g.UsageError("codex not logged in")

        with patch.dict(g.PROVIDERS, {"claude": lambda: usage(30, 5), "codex": codex_down}), \
                patch.object(g, "push_split", side_effect=lambda ip, panels, name, **k: uploaded.append(panels)), \
                patch.object(g, "show_image"):
            app._update_split()
            app.last_good["codex"] = (app.last_good["codex"][0], old)  # the failed read left it untouched
        claude, codex = uploaded[0]
        self.assertFalse(claude.get("stale"))
        self.assertTrue(codex["stale"])
        self.assertEqual(codex["current_pct"], 10)

    def test_split_update_keeps_working_with_one_provider_missing(self):
        app = self.make()
        app.split = True
        uploaded = []

        def codex_missing():
            raise g.UsageError("codex not installed")

        with patch.dict(g.PROVIDERS, {"claude": lambda: usage(30, 5), "codex": codex_missing}), \
                patch.object(g, "push_split", side_effect=lambda ip, panels, name, **k: uploaded.append(panels)), \
                patch.object(g, "show_image"):
            self.assertEqual(app._update_split(), "ok")
            self.assertEqual(len(uploaded[0]), 1)
            app.last_good.clear()
            with patch.dict(g.PROVIDERS, {"claude": codex_missing}):
                self.assertEqual(app._update_split(), "error")  # nothing to show at all

    def test_instant_switch_into_split_shows_its_last_image_only_while_active(self):
        app = self.make()
        app.slot["split"] = "b"
        shown = []
        with patch.object(g, "show_image", side_effect=lambda ip, name: shown.append(name)):
            app._show_uploaded("split")  # not in split view: nothing happens
            self.assertEqual(shown, [])
            app.split = True
            app._show_uploaded("split")
        self.assertEqual(shown, ["split-usage-b.gif"])

    def test_split_images_are_remembered_across_restarts_and_synced_with_the_device(self):
        app = self.make()
        app.slot["split"] = "a"
        app._save_state()
        self.assertEqual(self.make().slot.get("split"), "a")
        with patch.object(g, "list_images", return_value=set()):  # the device lost its files
            again = self.make()
            again._sync_with_device()
        self.assertNotIn("split", again.slot)

    def test_a_click_cancels_an_upload_of_the_split_view_too(self):
        app = self.make()
        app.split = True
        with patch.object(g, "push_split", side_effect=g.UploadCancelled()):
            self.assertEqual(app._deliver("split", {"panels": [usage()]}), "cancelled")
        self.assertNotIn("split", app.slot)

    # --- coming back to the view you left --------------------------------------------------------------

    def test_the_view_you_left_is_restored_after_a_restart(self):
        with patch.object(g, "show_image"):
            first = self.make()
            self.assertEqual((first.split, first.provider), (False, "claude"))  # first ever run
            first.select("codex")
            again = self.make()
            self.assertEqual((again.split, again.provider, again._active_key()), (False, "codex", "codex"))
            self.assertIn("Codex", again._tooltip())

            again.toggle_split()
            back = self.make()
            self.assertEqual((back.split, back._active_key()), (True, "split"))
            self.assertEqual(back.provider, "codex")  # what leaving the split view returns to
            self.assertIn("dividida", back._tooltip())
            back.toggle()
            self.assertEqual((back.split, back.provider), (False, "codex"))

            back.select("claude")
            self.assertEqual(self.make()._active_key(), "claude")

    def test_provider_argument_is_only_the_first_runs_choice(self):
        first_run = self.make(provider="codex")  # nothing remembered yet
        self.assertEqual(first_run.provider, "codex")
        with patch.object(g, "show_image"):
            self.make().select("claude")
        self.assertEqual(self.make(provider="codex").provider, "claude")  # the remembered one wins

    def test_startup_puts_the_remembered_view_back_on_screen(self):
        for view, expected in (("split", "split-usage-b.gif"), ("codex", "codex-usage-b.gif")):
            (self.tmp / "state.json").write_text(json.dumps({
                "slots": {"claude": "a", "codex": "b", "split": "b"}, "view": view, "provider": "codex",
                "render": tray.RENDER_ID,
            }))
            app = self.make()
            app.stop.set()  # run just the start-up part of the worker
            shown = []
            with patch.object(discover, "probe", return_value=True), \
                    patch.object(g, "list_images", return_value={"claude-usage-a.gif", "codex-usage-b.gif", "split-usage-b.gif"}), \
                    patch.object(g, "show_image", side_effect=lambda ip, name: shown.append(name)):
                app.worker()
            self.assertEqual(shown, [expected], view)

    def test_a_garbled_state_file_falls_back_to_the_defaults(self):
        (self.tmp / "state.json").write_text(json.dumps({"view": "nonsense", "provider": 7, "slots": "x"}))
        app = self.make()
        self.assertEqual((app.split, app.provider), (False, "claude"))

    def test_when_the_device_comes_back_the_remembered_view_is_pinned_before_the_upload(self):
        app = self.make()
        app.provider, app.slot["codex"] = "codex", "b"
        app.offline = True
        events = []
        with patch.object(g, "show_image", side_effect=lambda ip, name: events.append(("show", name))), \
                patch.object(g, "push_usage", side_effect=lambda *a, **k: events.append(("upload",))):
            outcome = app._deliver("codex", usage(title="Codex"))
        self.assertEqual(outcome, "ok")
        self.assertEqual(events[0], ("show", "codex-usage-b.gif"))  # the view is back on screen right away
        self.assertIn(("upload",), events)
        self.assertFalse(app.offline)

    def test_while_the_device_is_still_off_one_small_request_is_all_it_costs(self):
        app = self.make()
        app.slot["claude"] = "a"
        app.offline = True
        with patch.object(g, "show_image", side_effect=OSError("timed out")), \
                patch.object(g, "push_usage") as upload:
            self.assertEqual(app._deliver("claude", usage()), "offline")
        upload.assert_not_called()
        self.assertTrue(app.offline)

    # --- a click must never queue behind an upload ------------------------------------------------------------

    def test_no_upload_starts_right_after_a_click(self):
        app = self.make()
        with patch.object(g, "push_usage") as push, patch.object(g, "show_image"):
            app.last_click = time.monotonic()  # the user just clicked
            self.assertFalse(app._upload("claude", usage()))
            push.assert_not_called()
            self.assertTrue(app.wake.is_set(), "the worker must come back for it once clicking settles")
            app.last_click = time.monotonic() - tray.SETTLE_SECONDS - 1
            self.assertTrue(app._upload("claude", usage()))
            push.assert_called_once()

    def test_a_click_leaves_the_device_free_for_its_own_request(self):
        """The log showed: click -> upload cancelled -> *another* upload started 13 ms later -> click waited 4 s."""
        app = self.make()
        app.slot["split"] = "a"
        app.last_good = {p: (usage(title=TITLE), time.monotonic()) for p, TITLE in (("claude", "Claude"), ("codex", "Codex"))}
        app.outdated = {"claude", "codex", "split"}
        uploads = []
        with patch.object(g, "push_usage", side_effect=lambda *a, **k: uploads.append("one")), \
                patch.object(g, "push_split", side_effect=lambda *a, **k: uploads.append("split")), \
                patch.object(g, "show_image"):
            app.toggle_split()  # the click
            app._refresh_other("claude")  # what the worker does next in its cycle
            app._refresh_outdated_views()
        self.assertEqual(uploads, [], "nothing may take the device away from the click")

    # --- images drawn by older code ------------------------------------------------------------------------

    def test_images_drawn_by_older_code_are_redrawn_once_in_the_background(self):
        (self.tmp / "state.json").write_text(json.dumps({
            "slots": {"claude": "a", "codex": "a", "split": "a"}, "render": "an-older-version",
        }))
        app = self.make()
        self.assertEqual(app.outdated, {"claude", "codex", "split"})
        app.last_good = {"claude": (usage(), time.monotonic()), "codex": (usage(title="Codex"), time.monotonic())}
        uploaded = []
        with patch.object(g, "push_usage", side_effect=lambda ip, u, a, name, **k: uploaded.append(name)), \
                patch.object(g, "push_split", side_effect=lambda ip, panels, name, **k: uploaded.append(name)), \
                patch.object(g, "show_image") as show:
            app._refresh_outdated_views()  # claude is on screen: the normal cycle redraws it, so these two are left
            app._refresh_outdated_views()
            app._refresh_outdated_views()
            self.assertEqual(uploaded, ["codex-usage-b.gif", "split-usage-b.gif"])  # one per call, then nothing left
            show.assert_not_called()  # redrawn behind the scenes: the screen isn't touched
            self.assertEqual(app.outdated, {"claude"})
            self.assertIsNone(json.loads((self.tmp / "state.json").read_text())["render"])
            app._upload("claude", usage())  # what the regular cycle does
        self.assertEqual(app.outdated, set())
        self.assertEqual(json.loads((self.tmp / "state.json").read_text())["render"], tray.RENDER_ID)
        self.assertEqual(self.make().outdated, set(), "up to date: nothing to redo after the next restart")

    def test_nothing_is_redrawn_that_has_no_reading_yet(self):
        (self.tmp / "state.json").write_text(json.dumps({"slots": {"codex": "a", "split": "a"}, "render": "old"}))
        app = self.make()
        with patch.object(g, "push_usage") as one, patch.object(g, "push_split") as both:
            app._refresh_outdated_views()
        one.assert_not_called()
        both.assert_not_called()

    def test_delivery_without_an_address_reports_offline(self):
        app = self.make(None)
        self.assertEqual(app._deliver("claude", usage()), "offline")
        self.assertTrue(app.offline)


if __name__ == "__main__":
    unittest.main()
