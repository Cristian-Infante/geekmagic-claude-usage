"""Tray behaviour with the device, the network and the providers faked out."""
import json
import logging
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    import tray
except Exception as e:  # pystray needs a desktop session
    raise unittest.SkipTest(f"tray can't be imported here: {e}")

from geekmagic.device import discovery
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
            patch.object(tray, "BACKGROUND_STATS", False),  # count the activity inline so tests are deterministic
            # notifications are recorded (title, message), not sent; the system-route backup is recorded apart
            patch.object(tray.notifier, "send", side_effect=lambda title, message: self.backups.append((title, message)) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notes, self.backups = [], []
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
        with patch.object(tray.notifier, "send", return_value=False):  # the system's route fails too
            app._check_alerts("claude", usage(current=96))  # must not raise
        with patch.object(tray.notifier, "send", side_effect=OSError("helper exploded")):
            app._check_alerts("codex", usage(current=96, title="Codex"))  # nor must this

    def test_each_alert_is_shown_once_by_the_tray_and_the_system_route_is_only_a_backup(self):
        app = self.make()
        app._notify("Claude", "hola")
        self.assertEqual((self.notes, self.backups), ([("Claude", "hola")], []), "one notification, never two")
        app.icon.notify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no balloon here"))
        app._notify("Claude", "otra vez")
        self.assertEqual(self.backups, [("Claude", "otra vez")], "the tray couldn't: the system's own route steps in")

    def test_turned_off_notifications_send_nothing_at_all(self):
        app = self.make()
        app.toggle_notifications()
        app._notify("Claude", "hola")
        self.assertEqual((self.notes, self.backups), ([], []))

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
        with patch.object(discovery, "find_device", return_value="192.168.1.20") as find:
            app._maybe_rediscover()
            find.assert_called_once_with(prefer="192.168.1.50")
        self.assertEqual(app.ip, "192.168.1.20")
        self.assertEqual(self.notes[-1][0], "Pantalla encontrada")
        self.assertEqual(self.make(None).ip, "192.168.1.20")  # remembered for the next start

    def test_doesnt_scan_right_away_or_too_often(self):
        app = self.make("192.168.1.50")
        app.offline, app.offline_since = True, time.monotonic() - 5  # just went offline
        with patch.object(discovery, "find_device", return_value=None) as find:
            app._maybe_rediscover()
            app.offline_since = time.monotonic() - tray.REDISCOVER_AFTER - 1
            app._maybe_rediscover()  # due: scans
            app._maybe_rediscover()  # but not again straight away
        self.assertEqual(find.call_count, 1)

    def test_keeps_the_address_when_the_device_is_still_there(self):
        app = self.make("192.168.1.50")
        app.offline, app.offline_since = True, time.monotonic() - 1000
        with patch.object(discovery, "find_device", return_value="192.168.1.50"):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "192.168.1.50")
        self.assertEqual(self.notes, [])

    def test_no_address_at_all_scans_until_it_finds_the_device(self):
        app = self.make(None)
        self.assertEqual(app.ip, "")
        with patch.object(discovery, "find_device", return_value=None):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "")
        app.last_scan = time.monotonic() - tray.DISCOVER_RETRY - 1
        with patch.object(discovery, "find_device", return_value="192.168.1.20"):
            app._maybe_rediscover()
        self.assertEqual(app.ip, "192.168.1.20")

    def test_startup_prefers_given_ip_then_remembered_then_scans(self):
        (self.tmp / "state.json").write_text('{"ip": "192.168.1.99"}')
        with patch.object(discovery, "probe", side_effect=lambda ip, t=0: ip == "192.168.1.50"):
            app = self.make("192.168.1.50")
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.50")
        with patch.object(discovery, "probe", side_effect=lambda ip, t=0: ip == "192.168.1.99"):
            app = self.make("192.168.1.50")  # --ip is dead, but the remembered address answers
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.99")
        with patch.object(discovery, "probe", return_value=False), \
                patch.object(discovery, "find_device", return_value="192.168.1.20"):
            app = self.make("192.168.1.50")
            app._resolve_device()
            self.assertEqual(app.ip, "192.168.1.20")

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
            self.assertEqual([x["title"] for x in uploaded[0]], ["Claude", "Codex"], "the one that failed keeps its panel...")
            self.assertIsNone(uploaded[0][1]["current_pct"], "...with dashes instead of numbers")
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
            with patch.object(discovery, "probe", return_value=True), \
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

    # --- pace: history and projection --------------------------------------------------------------------------

    def reading(self, pct, reset_in_min=150, weekly=5.0, title="Claude"):
        now = datetime.now().astimezone()
        return {"title": title, "current_pct": pct, "current_reset": now + timedelta(minutes=reset_in_min),
                "weekly_pct": weekly, "weekly_reset": now + timedelta(days=3), "now": now}

    def test_readings_are_recorded_when_they_change_and_not_repeated_needlessly(self):
        app = self.make()
        first = self.reading(10)
        app._record_history("claude", first)
        app._record_history("claude", dict(first))  # same percentage moments later
        self.assertEqual(len(app.history["claude/current"]), 1)
        app._record_history("claude", self.reading(12))
        self.assertEqual([p[1] for p in app.history["claude/current"]], [10, 12])
        self.assertEqual(len(app.history["claude/weekly"]), 1)

    def test_a_reset_drops_the_readings_of_the_previous_window(self):
        app = self.make()
        app._record_history("claude", self.reading(90, reset_in_min=10))
        app._record_history("claude", self.reading(3, reset_in_min=300))  # a new window began
        self.assertEqual([p[1] for p in app.history["claude/current"]], [3])

    def test_history_is_capped(self):
        app = self.make()
        for pct in range(tray.HISTORY_MAX + 40):
            app._record_history("claude", self.reading(pct % 99))
        self.assertLessEqual(len(app.history["claude/current"]), tray.HISTORY_MAX)

    def test_a_fetch_attaches_the_pace_and_a_recent_burst_raises_it(self):
        app = self.make()
        now = time.time()
        reset = datetime.now().astimezone() + timedelta(minutes=150)
        # 15 minutes ago it was at 30%, now 40%: a burst, faster than the 40/150 average since the window started
        app.history["claude/current"] = [[now - 900, 30.0, reset.timestamp()]]
        with patch.dict(g.PROVIDERS, {"claude": lambda: self.reading(40)}):
            usage = app._fetch_usage("claude")
        rate = usage["pace"]["current"]["rate"]
        self.assertGreater(rate, 40 / 150)
        self.assertAlmostEqual(rate, 10 / 15, places=1)

    def test_history_survives_a_restart(self):
        app = self.make()
        app._record_history("claude", self.reading(10))
        app._record_history("claude", self.reading(14))
        app._save_state()
        self.assertEqual([p[1] for p in self.make().history["claude/current"]], [10, 14])

    def test_a_garbled_history_is_ignored(self):
        (self.tmp / "state.json").write_text(json.dumps({"history": {"claude/current": [[1, 2], "x", [1, 2, 3]], "bad": 5}}))
        self.assertEqual(self.make().history, {"claude/current": [[1, 2, 3]]})

    # --- pause and the computer being locked -----------------------------------------------------------------

    def run_worker_briefly(self, app, seconds=0.4):
        thread = threading.Thread(target=app.worker, daemon=True)
        with patch.object(tray, "PAUSE_POLL", 0.05), patch.object(discovery, "probe", return_value=True), \
                patch.object(g, "push_usage"), patch.object(g, "push_split"), patch.object(g, "show_image"):
            thread.start()
            time.sleep(seconds)
            app.stop.set()
            app.wake.set()
            thread.join(2)

    def test_nothing_is_read_or_uploaded_while_paused(self):
        app = self.make()
        calls = []
        with patch.dict(g.PROVIDERS, {"claude": lambda: calls.append("claude") or usage(),
                                      "codex": lambda: calls.append("codex") or usage(title="Codex")}):
            app.toggle_pause()
            self.assertTrue(app.paused)
            self.assertIn("en pausa", app._tooltip())
            self.run_worker_briefly(app)
        self.assertEqual(calls, [])

    def test_resuming_catches_up_at_once(self):
        app = self.make()
        app.paused = True
        app.toggle_pause()
        self.assertFalse(app.paused)
        self.assertTrue(app.wake.is_set())
        self.assertNotIn("pausa", app._tooltip())

    def test_the_updates_run_normally_when_not_paused(self):
        app = self.make()
        calls = []
        with patch.dict(g.PROVIDERS, {"claude": lambda: calls.append("claude") or usage(),
                                      "codex": lambda: calls.append("codex") or usage(title="Codex")}):
            self.run_worker_briefly(app)
        self.assertIn("claude", calls)

    def test_locking_the_computer_pauses_unless_that_is_turned_off(self):
        app = self.make()
        self.assertTrue(app.pause_on_lock)
        app._set_locked(True)
        self.assertTrue(app._is_paused())
        self.assertIn("PC bloqueado", app._tooltip())
        calls = []
        with patch.dict(g.PROVIDERS, {"claude": lambda: calls.append("claude") or usage()}):
            self.run_worker_briefly(app)
        self.assertEqual(calls, [], "locked: no queries")
        app.toggle_pause_on_lock()
        self.assertFalse(app._is_paused(), "locked but 'pause when locked' is off: keeps running")
        self.assertFalse(self.make().pause_on_lock, "remembered")

    def test_unlocking_wakes_the_loop_and_puts_the_view_back(self):
        app = self.make()
        app.slot["claude"] = "a"
        app._set_locked(True)
        app.wake.clear()
        shown = []
        with patch.object(g, "show_image", side_effect=lambda ip, name: shown.append(name)):
            app._run_in_background = lambda f, *a: f(*a)
            app._set_locked(False)
        self.assertTrue(app.wake.is_set())
        self.assertEqual(shown, ["claude-usage-a.gif"])

    def test_the_lock_watcher_notices_changes(self):
        app = self.make()
        states = iter([False, True, True, False])
        seen = []
        real_set = app._set_locked

        def is_locked():
            try:
                return next(states)
            except StopIteration:
                app.stop.set()
                return False

        with patch.object(tray.session_lock, "is_locked", side_effect=is_locked), patch.object(tray, "LOCK_POLL", 0.01), \
                patch.object(app, "_set_locked", side_effect=lambda v: (seen.append(v), real_set(v))):
            app._watch_lock()
        self.assertEqual(seen, [True, False])

    # --- brightness, night mode, dimming ---------------------------------------------------------------------

    def sync_background(self, app):
        app._run_in_background = lambda f, *a: f(*a)

    def test_brightness_is_sent_remembered_and_marked_in_the_menu(self):
        app = self.make()
        self.sync_background(app)
        with patch.object(g, "set_brightness") as send:
            app.set_brightness(75)
        send.assert_called_once_with("192.168.1.50", 75)
        self.assertEqual(app.brightness, 75)
        self.assertEqual(self.make().brightness, 75, "remembered across restarts")

    def test_a_refused_brightness_changes_nothing(self):
        app = self.make()
        with patch.object(g, "set_brightness", side_effect=OSError("device refused")):
            app.set_brightness(75)
        self.assertIsNone(app.brightness)
        self.assertIn("No se pudo cambiar el brillo", self.notes[-1][1])

    def test_night_mode_needs_a_known_day_brightness_first(self):
        app = self.make()
        with patch.object(g, "set_night_mode") as send:
            app.toggle_night()
        send.assert_not_called()
        self.assertFalse(app.night["enabled"])
        self.assertIn("Elige primero tu brillo normal", self.notes[-1][1])

    def test_night_mode_sends_the_schedule_with_the_day_level(self):
        app = self.make()
        app.brightness = 60
        with patch.object(g, "set_night_mode") as send:
            app.toggle_night()
            send.assert_called_once_with("192.168.1.50", start_hour=22, end_hour=7, night_level=10, day_level=60, enabled=True)
            self.assertTrue(app.night["enabled"])
            app.toggle_night()
            self.assertFalse(send.call_args.kwargs["enabled"])
        self.assertFalse(app.night["enabled"])

    def test_night_mode_stays_off_if_the_device_refuses(self):
        app = self.make()
        app.brightness = 60
        with patch.object(g, "set_night_mode", side_effect=OSError("FAIL")):
            app.toggle_night()
        self.assertFalse(app.night["enabled"])

    def test_changing_the_day_brightness_keeps_an_enabled_night_schedule_in_step(self):
        app = self.make()
        self.sync_background(app)
        app.brightness, app.night["enabled"] = 60, True
        with patch.object(g, "set_brightness"), patch.object(g, "set_night_mode") as night:
            app.set_brightness(30)
        self.assertEqual(night.call_args.kwargs["day_level"], 30)

    def test_night_label_and_settings_round_trip(self):
        app = self.make()
        self.assertEqual(app._night_label(), "Modo nocturno (10 PM - 7 AM, brillo 10 %)")
        self.assertEqual([tray._hour12(h) for h in (0, 7, 12, 13, 22, 23)], ["12 AM", "7 AM", "12 PM", "1 PM", "10 PM", "11 PM"])
        app.night.update(enabled=True, start=23, end=6, level=5)
        app.brightness = 80
        app._save_state()
        again = self.make()
        self.assertEqual((again.night, again.brightness), ({"enabled": True, "start": 23, "end": 6, "level": 5}, 80))

    def test_dim_when_locked_dims_then_restores_the_known_brightness(self):
        app = self.make()
        self.sync_background(app)
        app.brightness = 70
        with patch.object(g, "set_brightness") as send, patch.object(g, "show_image"):
            app.toggle_dim_on_lock()
            self.assertTrue(app.dim_on_lock)
            app._set_locked(True)
            app._set_locked(False)
        self.assertEqual([c.args[1] for c in send.call_args_list], [tray.LOCK_DIM_LEVEL, 70])

    def test_dim_when_locked_refuses_to_guess_the_brightness_to_restore(self):
        app = self.make()
        app.toggle_dim_on_lock()
        self.assertFalse(app.dim_on_lock)
        self.assertIn("Elige primero tu brillo normal", self.notes[-1][1])

    def test_no_dimming_when_it_is_off_or_there_is_no_device_address(self):
        app = self.make()
        self.sync_background(app)
        app.brightness = 70
        with patch.object(g, "set_brightness") as send, patch.object(g, "show_image"):
            app._set_locked(True)  # dim_on_lock is off
            self.assertEqual(send.call_count, 0)
            app.dim_on_lock = True
            app.ip = ""
            app._set_locked(False)
            self.assertEqual(send.call_count, 0)

    # --- the stats view ----------------------------------------------------------------------------------------

    def test_the_stats_view_is_a_menu_mode_that_the_click_leaves(self):
        app = self.make()
        with patch.object(g, "show_image"):
            app.toggle_stats()
            self.assertEqual((app.mode, app._active_key(), app.split), ("stats", "stats", False))
            self.assertIn("estadísticas", app._tooltip())
            self.assertEqual(self.make()._active_key(), "stats", "remembered")
            app.toggle()
            self.assertEqual((app.mode, app._active_key()), (None, "claude"))
            app.toggle_stats()
            app.toggle_split()  # one view of both at a time
            self.assertEqual(app.mode, "split")
            app.toggle_stats()
            app.toggle_stats()
            self.assertEqual(app.mode, None)

    ACTIVITY = {
        "claude": {"24h": {"requests": 422, "sessions": 4}, "7d": {"requests": 1584, "sessions": 13},
                   "days": [{"date": f"2026-09-{26 + i}", "requests": i} for i in range(7)]},
        "codex": {"24h": {"requests": 26, "sessions": 1}, "7d": {"requests": 1521, "sessions": 17},
                  "days": [{"date": f"2026-09-{26 + i}", "requests": 10 * i} for i in range(7)]},
    }

    def fake_counts(self, **overrides):
        """Replace how each provider's local activity is counted; returns the call log."""
        calls = []
        counters = {p: (lambda now, p=p: calls.append(p) or self.ACTIVITY[p]) for p in tray.TITLES}
        counters.update(overrides)
        return calls, patch.dict(tray.usage_stats.STATS, counters)

    def test_the_stats_update_uploads_one_still_screen_with_both_providers_activity(self):
        app = self.make()
        app.mode = tray.STATS
        uploaded, shown = [], []
        calls, counting = self.fake_counts()
        with patch.dict(g.PROVIDERS, {"claude": lambda: usage(20, 5), "codex": lambda: usage(10, 5, title="Codex")}), \
                counting, \
                patch.object(g, "push_stats", side_effect=lambda ip, panels, name, **k: uploaded.append((panels, name))), \
                patch.object(g, "show_image", side_effect=lambda ip, name: shown.append(name)):
            self.assertEqual(app._update_panels("stats"), "ok")
            app._update_panels("stats")  # the next cycle: the counts are not recomputed straight away
        panels, name = uploaded[0]
        self.assertEqual((name, shown[0]), ("stats-usage-a.gif", "stats-usage-a.gif"))
        self.assertEqual([p["title"] for p in panels], ["Claude", "Codex"])
        self.assertEqual([p["activity"] for p in panels], [self.ACTIVITY["claude"], self.ACTIVITY["codex"]],
                         "the same kind of numbers for both")
        self.assertEqual(sorted(calls), ["claude", "codex"], "each counted once, not once per cycle")

    def test_the_counts_come_back_after_their_interval(self):
        app = self.make()
        calls, counting = self.fake_counts()
        with counting:
            app._refresh_local_stats()
            app._refresh_local_stats()
            self.assertEqual(len(calls), 2)
            app.local_stats_at -= tray.LOCAL_STATS_EVERY + 1
            app._refresh_local_stats()
        self.assertEqual(len(calls), 4)

    def test_a_failing_count_does_not_break_the_stats_view(self):
        app = self.make()
        app.mode = tray.STATS
        uploaded = []

        def broken(now):
            raise OSError("disk")

        calls, counting = self.fake_counts(codex=broken)
        with patch.dict(g.PROVIDERS, {"claude": lambda: usage(20, 5), "codex": lambda: usage(10, 5, title="Codex")}), \
                counting, patch.object(g, "show_image"), \
                patch.object(g, "push_stats", side_effect=lambda ip, panels, name, **k: uploaded.append(panels)):
            self.assertEqual(app._update_panels("stats"), "ok")
        claude, codex = uploaded[0]
        self.assertEqual(claude["activity"], self.ACTIVITY["claude"])
        self.assertIsNone(codex["activity"], "no numbers yet: the screen says it's counting")

    def test_a_provider_without_logs_is_marked_as_having_none(self):
        app = self.make()
        calls, counting = self.fake_counts(claude=lambda now: None)
        with counting:
            app._refresh_local_stats()
        self.assertEqual(app.local_stats["claude"], {})
        self.assertEqual(app.local_stats["codex"], self.ACTIVITY["codex"])

    def test_old_stats_images_are_redrawn_in_the_background_too(self):
        (self.tmp / "state.json").write_text(json.dumps({"slots": {"stats": "a"}, "render": "old", "view": "claude"}))
        app = self.make()
        app.last_good = {p: (usage(title=t), time.monotonic()) for p, t in (("claude", "Claude"), ("codex", "Codex"))}
        uploaded = []
        calls, counting = self.fake_counts()
        with counting, patch.object(g, "push_stats", side_effect=lambda ip, panels, name, **k: uploaded.append(name)):
            app._refresh_outdated_views()
        self.assertEqual(uploaded, ["stats-usage-b.gif"])

    def test_split_uploads_carry_the_animation_choice(self):
        app = self.make()
        app.animation = "coffee"
        seen = []
        with patch.object(g, "push_split", side_effect=lambda ip, panels, name, **k: seen.append(k.get("animation"))), \
                patch.object(g, "show_image"):
            app._upload("split", {"panels": [usage()]})
        self.assertEqual(seen, ["coffee"])

    # --- the menu ----------------------------------------------------------------------------------------------

    def test_the_menu_has_every_new_button_in_a_sensible_order(self):
        app = self.make()
        top = [item.text for item in app.icon.menu.items if not item.text.startswith("-")]  # (no separators)
        for wanted in ("Vista dividida", "Estadísticas", "Más vistas", "Actualizar ahora", "Pausar", "Pantalla", "Opciones", "Ver logs", "Salir"):
            self.assertIn(wanted, top)
        self.assertLess(top.index("Vista dividida"), top.index("Estadísticas"))
        self.assertLess(top.index("Estadísticas"), top.index("Actualizar ahora"))
        self.assertLess(top.index("Opciones"), top.index("Ver logs"))
        sub = {i.text: [s.text for s in i.submenu.items if not s.text.startswith("-")]
               for i in app.icon.menu.items if i.submenu}
        self.assertEqual([x for x in sub["Pantalla"] if x.startswith("Brillo")],
                         [f"Brillo {level} %" for level in tray.DEFAULT_BRIGHTNESS_CHOICES])
        self.assertIn("Atenuar al bloquear el PC", sub["Pantalla"])
        self.assertTrue(any(x.startswith("Modo nocturno") for x in sub["Pantalla"]))
        self.assertEqual(sub["Opciones"], ["Iniciar sesión", "Notificaciones", "Avisar cuando un agente termine o te espere", "Pausar al bloquear el PC"])
        self.assertEqual(sub["Más vistas"], ["Proyectos y modelos", "Horas pico"])

    def test_night_hours_argument(self):
        import argparse
        self.assertEqual(tray.night_hours("22-7"), (22, 7))
        self.assertEqual(tray.night_hours("0-23"), (0, 23))
        for bad in ("22", "24-7", "a-b", "22-7-9", "-3-5", ""):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                tray.night_hours(bad)

    def test_night_settings_from_the_command_line_feed_the_menu_item(self):
        app = tray.App("192.168.1.50", 30, "idle", None, (23, 6), 5)
        self._alive.append(app)
        self.assertEqual((app.night["start"], app.night["end"], app.night["level"]), (23, 6, 5))
        self.assertFalse(app.night["enabled"], "defining the schedule doesn't switch it on")
        self.assertEqual(app._night_label(), "Modo nocturno (11 PM - 6 AM, brillo 5 %)")

    def test_brightness_menu_items_act_on_their_own_level(self):
        app = self.make()
        self.sync_background(app)
        screen = next(i for i in app.icon.menu.items if i.text == "Pantalla")
        item = next(i for i in screen.submenu.items if i.text == "Brillo 25 %")
        with patch.object(g, "set_brightness") as send:
            item(app.icon)
        send.assert_called_once_with("192.168.1.50", 25)
        self.assertTrue(item.checked)

    # --- projects / models and hours views ---------------------------------------------------------------------

    def test_the_two_new_views_are_menu_modes_like_the_others(self):
        app = self.make()
        with patch.object(g, "show_image"):
            for toggle, mode, words in ((app.toggle_breakdown, "breakdown", "proyectos y modelos"), (app.toggle_hours, "hours", "horas pico")):
                toggle()
                self.assertEqual((app.mode, app._active_key()), (mode, mode))
                self.assertIn(words, app._tooltip())
                self.assertEqual(self.make()._active_key(), mode, "remembered across restarts")
                app.toggle()  # the click leaves it
                self.assertEqual((app.mode, app._active_key()), (None, "claude"))
            app.toggle_hours()
            app.toggle_breakdown()  # one view of both at a time
            self.assertEqual(app.mode, "breakdown")
            app.toggle_breakdown()
            self.assertIsNone(app.mode)

    def test_each_new_view_has_its_own_icon_and_its_own_slots_on_the_device(self):
        for key in (tray.STATS, tray.BREAKDOWN, tray.HOURS, tray.SPLIT):
            self.assertEqual(tray._icon_for(key).size, (64, 64))
        self.assertEqual(len({tray._icon_for(k).tobytes() for k in (tray.STATS, tray.BREAKDOWN, tray.HOURS, tray.SPLIT)}), 4)
        app = self.make()
        app.slot.update(breakdown="a", hours="b")
        app._save_state()
        self.assertEqual({k: v for k, v in self.make().slot.items() if k in ("breakdown", "hours")}, {"breakdown": "a", "hours": "b"})

    def test_each_view_uploads_through_its_own_function(self):
        app = self.make()
        uploaded = []
        patches = [patch.object(g, name, side_effect=lambda ip, panels, filename, name=name, **k: uploaded.append((name, filename)))
                   for name in ("push_stats", "push_breakdown", "push_hours")]
        with patches[0], patches[1], patches[2], patch.object(g, "show_image"):
            for key in (tray.STATS, tray.BREAKDOWN, tray.HOURS):
                app._upload(key, {"panels": [usage()]})
        self.assertEqual(uploaded, [("push_stats", "stats-usage-a.gif"), ("push_breakdown", "breakdown-usage-a.gif"),
                                    ("push_hours", "hours-usage-a.gif")])

    def test_the_activity_views_carry_each_providers_activity_and_count_it_once(self):
        app = self.make()
        calls = []
        activity = {p: {"24h": {"requests": 1, "sessions": 1}, "7d": {"requests": 1, "sessions": 1}, "days": [], "total": 1,
                        "projects": [], "models": [], "hours": [0] * 24} for p in tray.TITLES}
        counters = {p: (lambda now, p=p: calls.append(p) or activity[p]) for p in tray.TITLES}
        seen = {}
        with patch.dict(tray.usage_stats.STATS, counters), \
                patch.dict(g.PROVIDERS, {"claude": lambda: usage(20, 5), "codex": lambda: usage(10, 5, title="Codex")}), \
                patch.object(g, "show_image"), \
                patch.object(g, "push_breakdown", side_effect=lambda ip, panels, name, **k: seen.setdefault("breakdown", panels)), \
                patch.object(g, "push_hours", side_effect=lambda ip, panels, name, **k: seen.setdefault("hours", panels)):
            for mode in (tray.BREAKDOWN, tray.HOURS):
                app.mode = mode
                self.assertEqual(app._update_panels(mode), "ok")
        for panels in seen.values():
            self.assertEqual([p["activity"] for p in panels], [activity["claude"], activity["codex"]])
        self.assertEqual(sorted(calls), ["claude", "codex"], "both views share one count")

    def test_the_count_runs_in_a_thread_and_wakes_the_loop_when_done(self):
        app = self.make()
        gate = threading.Event()

        def slow(now):
            gate.wait(2)
            return {"24h": {"requests": 1, "sessions": 1}}

        with patch.object(tray, "BACKGROUND_STATS", True), \
                patch.dict(tray.usage_stats.STATS, {"claude": slow, "codex": slow}):
            app.wake.clear()
            app._refresh_local_stats()
            self.assertTrue(app._counting, "counting in the background")
            self.assertEqual(app.local_stats, {}, "the screens say 'Counting...' meanwhile")
            app._refresh_local_stats()  # a second request while one is running does nothing
            gate.set()
            for _ in range(100):
                if not app._counting:
                    break
                time.sleep(0.02)
        self.assertFalse(app._counting)
        self.assertEqual(sorted(app.local_stats), ["claude", "codex"])
        self.assertTrue(app.wake.is_set(), "the loop is woken to redraw")

    def test_a_failed_count_is_retried_sooner_than_a_good_one_is_refreshed(self):
        app = self.make()
        calls = []

        def broken(now):
            calls.append(1)
            raise OSError("disk")

        with patch.dict(tray.usage_stats.STATS, {"claude": lambda now: {"24h": {}}, "codex": broken}):
            app._refresh_local_stats()
            app._refresh_local_stats()
            self.assertEqual(len(calls), 1, "not again straight away")
            app.local_stats_at -= tray.LOCAL_STATS_RETRY + 1
            app._refresh_local_stats()
            self.assertEqual(len(calls), 2, "incomplete: retried after a minute, not after ten")

    # --- agents working: indicator and "finished" notifications -------------------------------------------------

    def state(self, working, since=None, project="Acme App"):
        return {"working": working, "since": since, "last": time.time(), "project": project}

    def test_a_run_starting_is_shown_at_once_and_flags_the_redraw(self):
        app = self.make()
        app.wake.clear()
        app._agent_update("claude", self.state(True, since=time.time() - 30))
        self.assertTrue(app._working("claude"))
        self.assertFalse(app._working("codex"))
        self.assertTrue(app.agent_dirty)
        self.assertTrue(app.wake.is_set())
        self.assertIn("Claude trabajando", app._tooltip())
        self.assertEqual(self.notes, [], "starting isn't worth a notification")

    def test_a_run_is_only_over_after_two_quiet_polls_in_a_row(self):
        app = self.make()
        app._agent_update("claude", self.state(True, since=time.time() - 300))
        app.agent_dirty = False
        app._agent_update("claude", self.state(False))
        self.assertTrue(app._working("claude"), "one quiet poll: probably just a gap between steps")
        self.assertFalse(app.agent_dirty)
        app._agent_update("claude", self.state(True, since=time.time() - 300))  # it picked up again
        app._agent_update("claude", self.state(False))
        self.assertTrue(app._working("claude"), "the quiet streak starts over")
        app._agent_update("claude", self.state(False))
        self.assertFalse(app._working("claude"))
        self.assertTrue(app.agent_dirty)

    def test_a_long_run_finishing_notifies_with_how_long_and_where(self):
        app = self.make()
        app._agent_update("codex", self.state(True, since=time.time() - 250, project="Storefront"))
        app._agent_update("codex", self.state(False, project="Storefront"))
        app._agent_update("codex", self.state(False, project="Storefront"))
        title, message = self.notes[-1]
        self.assertEqual(title, "Codex · Storefront", "the project is in the title, the first thing you read")
        self.assertEqual(message, "Terminó (trabajó 4 min)")

    def test_short_runs_finish_without_a_notification(self):
        app = self.make()
        app._agent_update("claude", self.state(True, since=time.time() - 20))
        for _ in range(2):
            app._agent_update("claude", self.state(False))
        self.assertEqual(self.notes, [])
        self.assertFalse(app._working("claude"))

    def test_the_finished_notification_can_be_turned_off_and_is_remembered(self):
        app = self.make()
        app.toggle_notify_done()
        self.assertFalse(app.notify_done)
        app._agent_update("claude", self.state(True, since=time.time() - 400))
        for _ in range(2):
            app._agent_update("claude", self.state(False))
        self.assertEqual(self.notes, [])
        self.assertTrue(app.agent_dirty, "the screen still updates")
        self.assertFalse(self.make().notify_done)

    def test_a_run_already_under_way_when_the_app_starts_is_timed_from_the_log(self):
        app = self.make()
        app._agent_update("claude", self.state(True, since=time.time() - 900))  # started 15 minutes before we looked
        for _ in range(2):
            app._agent_update("claude", self.state(False))
        self.assertIn("15 min", self.notes[-1][1])

    # --- an agent waiting for you ----------------------------------------------------------------------------

    def waiting(self, reason="question", project="Acme App", since=None):
        return {**self.state(True, since=since or time.time() - 100, project=project), "waiting": True, "waiting_for": reason}

    def test_waiting_is_only_believed_after_two_polls_in_a_row(self):
        app = self.make()
        app._agent_update("claude", self.waiting())
        self.assertFalse(app._waiting("claude"), "one poll could be a flicker")
        self.assertEqual(self.notes, [])
        self.assertTrue(app._working("claude"))
        app._agent_update("claude", self.state(True, since=time.time() - 100))  # it moved on: the streak starts over
        app._agent_update("claude", self.waiting())
        self.assertFalse(app._waiting("claude"))
        app._agent_update("claude", self.waiting())
        self.assertTrue(app._waiting("claude"))
        self.assertEqual(len(self.notes), 1)

    def test_the_notification_says_what_it_is_waiting_for_and_where(self):
        cases = (("question", "Te hizo una pregunta"), ("plan", "Espera que apruebes su plan"),
                 ("approval (Edit)", "Espera tu aprobación (Edit)"), ("approval", "Espera tu aprobación"),
                 ("something new", "Espera tu respuesta"))
        for reason, expected in cases:
            app = self.make()
            self.notes.clear()
            for _ in range(2):
                app._agent_update("claude", self.waiting(reason))
            self.assertEqual(self.notes[-1], ("Claude · Acme App", expected), "the project is in the title")
        app = self.make()
        self.notes.clear()
        for _ in range(2):
            app._agent_update("codex", self.waiting("question", project=None))
        self.assertEqual(self.notes[-1], ("Codex", "Te hizo una pregunta"), "without a project, just the provider")

    def test_waiting_flags_a_redraw_and_shows_in_the_tooltip(self):
        app = self.make()
        app.agent_dirty = False
        for _ in range(2):
            app._agent_update("codex", self.waiting("approval"))
        self.assertTrue(app.agent_dirty)
        self.assertIn("Codex te espera", app._tooltip())
        self.assertNotIn("trabajando", app._tooltip())

    def test_answering_clears_it_and_redraws_but_it_keeps_working(self):
        app = self.make()
        for _ in range(2):
            app._agent_update("claude", self.waiting())
        app.agent_dirty = False
        self.notes.clear()
        app._agent_update("claude", self.state(True, since=time.time() - 100))
        self.assertFalse(app._waiting("claude"))
        self.assertTrue(app._working("claude"))
        self.assertTrue(app.agent_dirty)
        self.assertEqual(self.notes, [], "no notification for being answered")
        self.assertIn("Claude trabajando", app._tooltip())

    def test_one_notification_per_wait_not_one_per_poll(self):
        app = self.make()
        for _ in range(10):
            app._agent_update("claude", self.waiting())
        self.assertEqual(len(self.notes), 1)

    def test_it_can_be_silenced_with_the_same_option_as_the_finished_notification(self):
        app = self.make()
        app.notify_done = False
        for _ in range(3):
            app._agent_update("claude", self.waiting())
        self.assertEqual(self.notes, [])
        self.assertTrue(app._waiting("claude"), "the screen still shows it")

    def test_waiting_then_finishing_still_reports_the_whole_run(self):
        app = self.make()
        app._agent_update("claude", self.waiting(since=time.time() - 600))
        app._agent_update("claude", self.waiting(since=time.time() - 600))
        self.notes.clear()
        for _ in range(2):
            app._agent_update("claude", self.state(False))
        self.assertIn("10 min", self.notes[-1][1])
        self.assertFalse(app._waiting("claude"))

    def test_the_flag_reaches_what_gets_drawn(self):
        app = self.make()
        for _ in range(2):
            app._agent_update("claude", self.waiting())
        single = app._flag_working("claude", usage())
        self.assertEqual((single["working"], single["waiting"]), (True, True))
        other = app._flag_working("codex", usage(title="Codex"))
        self.assertEqual((other["working"], other["waiting"]), (False, False))
        panels = app._flag_working("split", {"panels": [usage(), usage(title="Codex")]})["panels"]
        self.assertEqual([(p["working"], p["waiting"]) for p in panels], [(True, True), (False, False)])

    # --- several projects at once ---------------------------------------------------------------------------

    def session(self, sid, project, working=True, waiting=False, reason=None, since=None):
        return {"id": sid, "project": project, "working": working, "waiting": waiting, "waiting_for": reason,
                "since": since or time.time() - 300, "last": time.time()}

    def overall(self, *sessions):
        """What agent_activity reports: the overall state plus every session."""
        lead = next((s for s in sessions if s["waiting"]), next((s for s in sessions if s["working"]), sessions[0]))
        return {**{k: v for k, v in lead.items() if k != "id"}, "sessions": list(sessions)}

    def test_each_project_is_followed_and_notified_on_its_own(self):
        app = self.make()
        alpha, beta = self.session("A", "Alpha"), self.session("B", "Beta")
        app._agent_update("claude", self.overall(alpha, beta))
        self.assertEqual(app.agent["claude"]["sessions"], 2)
        beta_asks = {**beta, "waiting": True, "reason": "question", "waiting_for": "question"}
        for _ in range(2):
            app._agent_update("claude", self.overall(alpha, beta_asks))
        self.assertEqual(self.notes, [("Claude · Beta", "Te hizo una pregunta")], "it says which project is asking")
        self.assertTrue(app._waiting("claude"))

    def test_two_projects_asking_at_once_each_get_their_own_notification(self):
        app = self.make()
        asking = lambda sid, project: {**self.session(sid, project), "waiting": True, "waiting_for": "question"}
        for _ in range(3):
            app._agent_update("claude", self.overall(asking("A", "Alpha"), asking("B", "Beta")))
        self.assertEqual(sorted(self.notes), [("Claude · Alpha", "Te hizo una pregunta"), ("Claude · Beta", "Te hizo una pregunta")])

    def test_one_project_finishing_is_reported_while_another_keeps_working(self):
        app = self.make()
        alpha, beta = self.session("A", "Alpha", since=time.time() - 400), self.session("B", "Beta", since=time.time() - 50)
        app._agent_update("claude", self.overall(alpha, beta))
        alpha_done = {**alpha, "working": False, "waiting": False, "since": None}
        for _ in range(2):
            app._agent_update("claude", self.overall(alpha_done, beta))
        self.assertEqual(self.notes, [("Claude · Alpha", "Terminó (trabajó 6 min)")])
        self.assertTrue(app._working("claude"), "Beta is still going: the screen keeps saying so")
        self.assertEqual(app.agent["claude"]["sessions"], 1)
        self.assertEqual(app.agent["claude"]["project"], "Beta")

    def test_the_last_one_finishing_clears_the_indicator(self):
        app = self.make()
        alpha = self.session("A", "Alpha", since=time.time() - 400)
        app._agent_update("claude", self.overall(alpha))
        done = {**alpha, "working": False, "since": None}
        for _ in range(2):
            app._agent_update("claude", self.overall(done))
        self.assertFalse(app._working("claude"))
        self.assertEqual(len(self.notes), 1)

    def test_a_session_that_disappears_counts_as_finished(self):
        app = self.make()
        app._agent_update("claude", self.overall(self.session("A", "Alpha", since=time.time() - 200)))
        idle = {"working": False, "waiting": False, "waiting_for": None, "since": None, "last": None, "project": None, "sessions": []}
        for _ in range(2):
            app._agent_update("claude", idle)
        self.assertFalse(app._working("claude"))
        self.assertEqual(self.notes, [("Claude · Alpha", "Terminó (trabajó 3 min)")])
        self.assertEqual(app.tracks["claude"], {}, "forgotten once idle and gone")

    def test_the_needier_project_leads_the_summary_the_screens_use(self):
        app = self.make()
        working = self.session("A", "Alpha")
        asking = {**self.session("B", "Beta"), "waiting": True, "waiting_for": "plan"}
        for _ in range(2):
            app._agent_update("claude", self.overall(working, asking))
        summary = app.agent["claude"]
        self.assertEqual((summary["waiting"], summary["waiting_for"], summary["project"]), (True, "plan", "Beta"))

    def test_sessions_of_the_two_providers_do_not_mix(self):
        app = self.make()
        app._agent_update("claude", self.overall(self.session("S", "Same")))
        app._agent_update("codex", self.overall(self.session("S", "Same")))
        self.assertEqual((app.agent["claude"]["sessions"], app.agent["codex"]["sessions"]), (1, 1))
        done = {**self.session("S", "Same"), "working": False}
        for _ in range(2):
            app._agent_update("codex", self.overall(done))
        self.assertTrue(app._working("claude"))
        self.assertFalse(app._working("codex"))

    def test_polling_reads_both_agents_and_survives_a_failure(self):
        app = self.make()
        states = {"claude": lambda now: self.state(True, since=now - 100), "codex": lambda now: 1 / 0}
        with patch.dict(tray.agent_activity.STATES, states):
            app._poll_agents()
        self.assertTrue(app._working("claude"))
        self.assertFalse(app._working("codex"))

    def test_the_flag_reaches_what_gets_drawn(self):
        app = self.make()
        app.agent = {"claude": {"working": True}}
        single = app._flag_working("claude", usage())
        self.assertTrue(single["working"])
        self.assertFalse(app._flag_working("codex", usage(title="Codex"))["working"])
        panels = app._flag_working("split", {"panels": [usage(), usage(title="Codex")]})["panels"]
        self.assertEqual([p["working"] for p in panels], [True, False])
        self.assertNotIn("working", usage(), "the original reading isn't modified")

    def test_uploads_carry_the_flag(self):
        app = self.make()
        app.agent = {"codex": {"working": True}}
        seen = []
        with patch.object(g, "push_usage", side_effect=lambda ip, u, anim, name, **k: seen.append(u["working"])), \
                patch.object(g, "show_image"):
            app._upload("codex", usage(title="Codex"))
            app._upload("claude", usage())
        self.assertEqual(seen, [True, False])

    def test_an_agent_change_redraws_from_what_is_already_read_without_querying_again(self):
        app = self.make()
        queries = []
        with patch.dict(g.PROVIDERS, {"claude": lambda: queries.append("claude") or usage(),
                                      "codex": lambda: queries.append("codex") or usage(title="Codex")}), \
                patch.object(g, "push_usage"), patch.object(g, "push_split"), patch.object(g, "show_image"):
            app._fetch_usage("claude")
            app._fetch_usage("codex")
            queries.clear()
            self.assertEqual(app._update_panels("split", refetch=False), "ok")
            self.assertEqual(queries, [], "redrawn from the last readings")
            app._update_panels("split")
            self.assertEqual(sorted(queries), ["claude", "codex"])

    def test_the_agent_watcher_keeps_working_while_paused(self):
        app = self.make()
        app.paused = True
        app._agent_update("claude", self.state(True, since=time.time() - 300))
        for _ in range(2):
            app._agent_update("claude", self.state(False))
        self.assertEqual(len(self.notes), 1, "paused or locked is exactly when you want to hear it")

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
