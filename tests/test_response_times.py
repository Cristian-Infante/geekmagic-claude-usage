"""The things that make the screen respond quickly: small GIFs, one request to show an image, parallel reads."""
import io
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from PIL import Image

import geekmagic_claude as g


def reading(title="Claude", pct=36.0):
    now = datetime.now().astimezone()
    return {"title": title, "current_pct": pct, "current_reset": now + timedelta(minutes=103),
            "weekly_pct": 17.0, "weekly_reset": now + timedelta(days=5), "now": now}


class GifSizeTests(unittest.TestCase):
    def frames(self, animation="idle"):
        u = reading()
        return [g._render_frame(u, dx, dy, extra) for dx, dy, extra in g.ANIMATIONS[animation]]

    def full_frame_size(self, frames):
        """What the same frames cost when every one is stored whole (how the GIFs used to be written)."""
        sheet = Image.new("RGB", (g.WIDTH, g.HEIGHT * len(frames)))
        for i, f in enumerate(frames):
            sheet.paste(f, (0, i * g.HEIGHT))
        base = sheet.quantize(colors=96)
        quantized = [f.quantize(palette=base) for f in frames]
        buf = io.BytesIO()
        quantized[0].save(buf, format="GIF", save_all=True, append_images=quantized[1:], duration=130, loop=0, disposal=2, optimize=False)
        return len(buf.getvalue())

    def test_animations_are_several_times_smaller_than_storing_every_frame_whole(self):
        for animation in ("idle", "typing", "coffee"):
            frames = self.frames(animation)
            small, whole = len(g._encode_gif(frames)), self.full_frame_size(frames)
            self.assertLess(small * 3, whole, f"{animation}: {small} B vs {whole} B")

    def test_a_split_view_upload_is_small_enough_to_store_in_about_a_second(self):
        panels = [reading(), reading("Codex")]
        gif = g.render_split(panels, ["laptop", "bolt"])
        # the device stores ~36 ms per KB: under ~30 KB is under ~1.1 s (it used to be ~120 KB, 4.3 s)
        self.assertLess(len(gif), 40 * 1024)

    def test_the_stills_are_tiny(self):
        panels = [{**reading(), "activity": None}, {**reading("Codex"), "activity": None}]
        self.assertLess(len(g.render_stats(panels)), 20 * 1024)


class GifFidelityTests(unittest.TestCase):
    """Smaller must not mean different: every frame still decodes to exactly what was drawn."""

    def test_every_frame_decodes_to_the_frame_that_was_drawn(self):
        u = reading()
        frames = [g._render_frame(u, dx, dy, extra) for dx, dy, extra in g.ANIMATIONS["typing"]]
        decoded = Image.open(io.BytesIO(g._encode_gif(frames)))
        # what each frame looks like after the shared palette (the only loss, and the same as before)
        sheet = Image.new("RGB", (g.WIDTH, g.HEIGHT * len(frames)))
        for i, f in enumerate(frames):
            sheet.paste(f, (0, i * g.HEIGHT))
        base = sheet.quantize(colors=96)
        expected = [f.quantize(palette=base).convert("RGB") for f in frames]
        shown = []
        for i in range(decoded.n_frames):
            decoded.seek(i)
            shown.append(decoded.convert("RGB"))
        # identical frames in a row are merged into one longer frame, so compare as a timeline
        timeline = []
        for i in range(decoded.n_frames):
            decoded.seek(i)
            timeline += [shown[i]] * max(1, round(decoded.info.get("duration", 130) / 130))
        self.assertEqual(len(timeline), len(expected))
        for i, (got, want) in enumerate(zip(timeline, expected)):
            self.assertEqual(got.tobytes(), want.tobytes(), f"frame {i}")

    def test_it_loops_forever_and_keeps_the_frame_timing(self):
        data = g._encode_gif([g._render_frame(reading(), dx, dy, extra) for dx, dy, extra in g.ANIMATIONS["dance"]])
        self.assertTrue(data.startswith(b"GIF89a"))
        self.assertIn(b"NETSCAPE2.0", data, "the loop extension")
        im = Image.open(io.BytesIO(data))
        self.assertEqual(im.info.get("loop"), 0)
        self.assertEqual(im.size, (g.WIDTH, g.HEIGHT))
        total = 0
        for i in range(im.n_frames):
            im.seek(i)
            total += im.info.get("duration", 0)
        self.assertEqual(total, 130 * len(g.ANIMATIONS["dance"]))

    def test_the_first_frame_is_the_whole_screen_and_later_ones_stay_inside_it(self):
        im = Image.open(io.BytesIO(g._encode_gif([g._render_frame(reading(), dx, dy, extra) for dx, dy, extra in g.ANIMATIONS["laptop"]])))
        for i in range(im.n_frames):
            im.seek(i)
            x0, y0, x1, y1 = im.tile[0][1]
            self.assertTrue(0 <= x0 < x1 <= g.WIDTH and 0 <= y0 < y1 <= g.HEIGHT, f"frame {i}: {im.tile[0][1]}")
            if i == 0:
                self.assertEqual((x0, y0, x1, y1), (0, 0, g.WIDTH, g.HEIGHT), "the loop restarts on a complete picture")
            self.assertEqual(im.disposal_method, 1)

    def test_a_single_frame_still_encodes(self):
        data = g._encode_gif([g._render_frame(reading())])
        self.assertEqual(Image.open(io.BytesIO(data)).n_frames, 1)


class FakeDevice:
    """Answers /set?theme= and /set?img= like the SmallTV: OK for what it knows, FAIL otherwise."""

    def __init__(self, files=("a.gif",), refuse_img=0):
        self.requests, self.files, self.refuse_img = [], set(files), refuse_img
        device = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                device.requests.append(next(iter(query), ""))
                ok = "theme" in query or ("img" in query and query["img"].rsplit("/", 1)[-1] in device.files)
                if "img" in query and device.refuse_img > 0:
                    device.refuse_img, ok = device.refuse_img - 1, False
                body = b"OK" if ok else b"FAIL"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ShowImageTests(unittest.TestCase):
    def setUp(self):
        g._theme_ready.clear()
        self.device = FakeDevice()
        self.addCleanup(self.device.close)

    def test_the_first_show_selects_the_theme_and_every_later_one_is_a_single_request(self):
        g.show_image(self.device.address, "a.gif")
        self.assertEqual(self.device.requests, ["theme", "img"])
        g.show_image(self.device.address, "a.gif")
        g.show_image(self.device.address, "a.gif")
        self.assertEqual(self.device.requests, ["theme", "img", "img", "img"], "one request each from then on")

    def test_a_refused_image_reselects_the_theme_and_tries_once_more(self):
        g.show_image(self.device.address, "a.gif")
        self.device.requests.clear()
        self.device.refuse_img = 1  # e.g. someone changed the theme on the device
        g.show_image(self.device.address, "a.gif")
        self.assertEqual(self.device.requests, ["img", "theme", "img"])

    def test_an_image_the_device_does_not_have_is_an_error_and_forgets_the_theme(self):
        with self.assertRaisesRegex(OSError, "wouldn't show missing.gif"):
            g.show_image(self.device.address, "missing.gif")
        self.assertNotIn(self.device.address, g._theme_ready)
        self.device.requests.clear()
        g.show_image(self.device.address, "a.gif")
        self.assertEqual(self.device.requests, ["theme", "img"], "selected again, as it may have restarted")

    def test_an_unreachable_device_forgets_the_theme_too(self):
        g.show_image(self.device.address, "a.gif")
        self.assertIn(self.device.address, g._theme_ready)
        self.device.close()
        with self.assertRaises(OSError):
            g.show_image(self.device.address, "a.gif")
        self.assertNotIn(self.device.address, g._theme_ready)
        self.device = FakeDevice()  # (the cleanup closes it)

    def test_each_device_is_tracked_on_its_own(self):
        other = FakeDevice()
        self.addCleanup(other.close)
        g.show_image(self.device.address, "a.gif")
        g.show_image(other.address, "a.gif")
        self.assertEqual(other.requests, ["theme", "img"], "a second screen still gets its theme")


class ParallelReadTests(unittest.TestCase):
    """The tray reads Claude and Codex at the same time."""

    @classmethod
    def setUpClass(cls):
        try:
            import tray
        except Exception as e:
            raise unittest.SkipTest(f"tray can't be imported here: {e}")
        cls.tray = tray

    _alive = []

    def setUp(self):
        import logging
        logging.disable(logging.CRITICAL)  # a failing read is logged on purpose in one of these
        self.addCleanup(logging.disable, logging.NOTSET)

    def make(self):
        import tempfile
        from pathlib import Path
        patches = [patch.object(self.tray, "STATE_PATH", Path(tempfile.mkdtemp()) / "state.json"),
                   patch.object(self.tray, "BACKGROUND_STATS", False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app = self.tray.App("192.168.1.20", 30, "idle")
        self._alive.append(app)
        app.icon.notify = lambda *a, **k: None
        return app

    def test_both_providers_are_read_at_the_same_time(self):
        app = self.make()
        started = {"claude": threading.Event(), "codex": threading.Event()}

        def read(provider):
            def run():
                started[provider].set()
                other = "codex" if provider == "claude" else "claude"
                # each waits for the other to have started: sequential reads would time out here
                self.assertTrue(started[other].wait(3), f"{other} was not read while {provider} was")
                return reading("Claude" if provider == "claude" else "Codex")
            return run

        with patch.dict(g.PROVIDERS, {"claude": read("claude"), "codex": read("codex")}):
            t = time.monotonic()
            app._fetch_all()
            took = time.monotonic() - t
        self.assertEqual(sorted(app.last_good), ["claude", "codex"])
        self.assertLess(took, 2)

    def test_the_view_update_takes_as_long_as_the_slower_read_not_both(self):
        app = self.make()

        def slow(title):
            def read():
                time.sleep(0.4)
                return reading(title)
            return read

        with patch.dict(g.PROVIDERS, {"claude": slow("Claude"), "codex": slow("Codex")}), \
                patch.object(g, "push_split"), patch.object(g, "show_image"):
            app.mode = self.tray.SPLIT
            t = time.monotonic()
            self.assertEqual(app._update_panels("split"), "ok")
            took = time.monotonic() - t
        self.assertLess(took, 0.75, "two 0.4 s reads, in parallel")

    def test_a_failing_read_does_not_hold_up_or_break_the_other(self):
        app = self.make()

        def broken():
            raise g.UsageError("not logged in")

        with patch.dict(g.PROVIDERS, {"claude": broken, "codex": lambda: reading("Codex")}):
            app._fetch_all()
        self.assertEqual(sorted(app.last_good), ["codex"])

    def test_saving_state_while_readings_arrive_in_other_threads_never_breaks(self):
        app = self.make()
        errors, stop = [], threading.Event()

        def hammer_saves():
            while not stop.is_set():
                try:
                    app._save_state()
                except Exception as e:  # e.g. "dictionary changed size during iteration"
                    errors.append(e)

        counter = iter(range(10**6))

        def hammer_readings(provider, title):
            real = g.PROVIDERS[provider]
            g.PROVIDERS[provider] = lambda: reading(title, pct=next(counter) % 99)  # (one patch, not one per reading)
            try:
                for _ in range(60):
                    app._fetch_usage(provider)
            finally:
                g.PROVIDERS[provider] = real

        saver = threading.Thread(target=hammer_saves)
        saver.start()
        readers = [threading.Thread(target=hammer_readings, args=(p, t)) for p, t in (("claude", "Claude"), ("codex", "Codex"))]
        for r in readers:
            r.start()
        for r in readers:
            r.join()
        stop.set()
        saver.join()
        self.assertEqual(errors, [])
        import json
        saved = json.loads(self.tray.STATE_PATH.read_text())
        self.assertIn("claude/current", saved["history"])

    def test_agents_are_polled_often_enough_to_feel_live(self):
        self.assertLessEqual(self.tray.AGENT_POLL, 2)


class StartupOrderTests(unittest.TestCase):
    def test_the_remembered_view_goes_up_before_the_search_and_the_file_list(self):
        try:
            import tray
        except Exception as e:
            self.skipTest(f"tray can't be imported here: {e}")
        import json
        import tempfile
        from pathlib import Path
        state = Path(tempfile.mkdtemp()) / "state.json"
        state.write_text(json.dumps({"slots": {"claude": "a"}, "view": "claude", "render": tray.RENDER_ID, "ip": "192.168.1.20"}))
        order = []
        with patch.object(tray, "STATE_PATH", state), patch.object(tray, "BACKGROUND_STATS", False):
            app = tray.App(None, 30, "idle")
            self.addCleanup(lambda: None)
            ParallelReadTests._alive.append(app)
            app.stop.set()  # only the start-up part
            with patch.object(g, "show_image", side_effect=lambda ip, name: order.append("show")), \
                    patch.object(tray.discover, "probe", side_effect=lambda *a, **k: order.append("search") or True), \
                    patch.object(g, "list_images", side_effect=lambda ip: order.append("files") or {"claude-usage-a.gif"}):
                app.worker()
        self.assertEqual(order[:3], ["show", "search", "files"])
        self.assertEqual(order.count("show"), 1, "and not shown a second time when the first got through")

    def test_it_is_shown_again_if_the_first_try_did_not_get_through(self):
        try:
            import tray
        except Exception as e:
            self.skipTest(f"tray can't be imported here: {e}")
        import json
        import tempfile
        from pathlib import Path
        state = Path(tempfile.mkdtemp()) / "state.json"
        state.write_text(json.dumps({"slots": {"claude": "a"}, "view": "claude", "render": tray.RENDER_ID, "ip": "192.168.1.20"}))
        attempts = []

        def flaky(ip, name):
            attempts.append(name)
            if len(attempts) == 1:
                raise OSError("timed out")

        with patch.object(tray, "STATE_PATH", state), patch.object(tray, "BACKGROUND_STATS", False):
            app = tray.App(None, 30, "idle")
            ParallelReadTests._alive.append(app)
            app.stop.set()
            with patch.object(g, "show_image", side_effect=flaky), patch.object(tray.discover, "probe", return_value=True), \
                    patch.object(g, "list_images", return_value={"claude-usage-a.gif"}):
                app.worker()
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
