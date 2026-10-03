"""Rendering of the alert colours and of the dimmed "no fresh data" image."""
import unittest
from datetime import datetime, timedelta

from PIL import ImageStat

import geekmagic_claude as g

BAR_SAMPLE = (22, 93)  # inside the left end of the "Session" bar
WEEK_BAR_SAMPLE = (22, 187)  # same for the "Weekly" bar


def hex_to_rgb(color: str):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def usage(current, weekly=10.0, title="Claude", **extra):
    now = datetime.now().astimezone().replace(hour=14, minute=57, second=0, microsecond=0)
    return {
        "title": title, "current_pct": current, "current_reset": now + timedelta(hours=2),
        "weekly_pct": weekly, "weekly_reset": now + timedelta(days=3), "now": now, **extra,
    }


def has_colour(image, color, box):
    target = hex_to_rgb(color)
    crop = image.crop(box)
    return any(pixel == target for pixel in crop.getdata())


def has_similar_colour(image, color, box, tolerance=40, at_least=8):
    """Small anti-aliased text rarely has a pixel of the exact colour: look for enough close-by ones."""
    target = hex_to_rgb(color)
    close = sum(1 for p in image.crop(box).getdata() if all(abs(a - b) <= tolerance for a, b in zip(p, target)))
    return close >= at_least


class ClockTests(unittest.TestCase):
    def test_twelve_hour_format_regardless_of_locale(self):
        at = lambda h, m: datetime(2026, 10, 2, h, m)
        self.assertEqual(
            [g._clock(at(h, m)) for h, m in ((0, 5), (9, 3), (11, 59), (12, 0), (12, 30), (14, 57), (23, 59))],
            ["12:05 AM", "9:03 AM", "11:59 AM", "12:00 PM", "12:30 PM", "2:57 PM", "11:59 PM"],
        )

    def test_the_screens_use_it(self):
        import inspect
        source = inspect.getsource(g)
        self.assertNotIn("usage['now']:%H:%M}", source, "a 24-hour time slipped back onto a screen")


class AlertColourTests(unittest.TestCase):
    def test_bar_and_number_change_colour_past_the_thresholds(self):
        for pct, expected in ((20, g.THEMES["Claude"]["current"]), (75, g.STATE_COLORS["warn"]), (95, g.STATE_COLORS["crit"])):
            image = g._render_frame(usage(pct))
            self.assertEqual(image.getpixel(BAR_SAMPLE), hex_to_rgb(expected), f"bar at {pct}%")
            number_box = (14, 46, 70, 80)
            if pct >= 70:
                self.assertTrue(has_colour(image, expected, number_box), f"number at {pct}%")
            else:
                self.assertFalse(has_colour(image, g.STATE_COLORS["warn"], number_box))

    def test_each_bar_is_judged_on_its_own(self):
        image = g._render_frame(usage(20, weekly=95))
        self.assertEqual(image.getpixel(BAR_SAMPLE), hex_to_rgb(g.THEMES["Claude"]["current"]))
        self.assertEqual(image.getpixel(WEEK_BAR_SAMPLE), hex_to_rgb(g.STATE_COLORS["crit"]))

    def test_codex_theme_uses_the_same_alert_colours(self):
        image = g._render_frame(usage(95, title="Codex"))
        self.assertEqual(image.getpixel(BAR_SAMPLE), hex_to_rgb(g.STATE_COLORS["crit"]))


class StaleImageTests(unittest.TestCase):
    def test_stale_image_is_dimmed_and_dated(self):
        fresh = g._render_frame(usage(40))
        stale = g._render_frame(usage(40, stale=True))
        self.assertLess(ImageStat.Stat(stale).mean[0], ImageStat.Stat(fresh).mean[0] * 0.8)  # clearly darker
        footer = (0, 218, 240, 240)
        self.assertTrue(has_colour(stale, g.STATE_COLORS["warn"], footer), "warning-coloured footer text")
        self.assertFalse(has_colour(fresh, g.STATE_COLORS["warn"], footer))

    def test_stale_animation_still_renders(self):
        self.assertTrue(g.render_animation(usage(40, stale=True), "idle").startswith(b"GIF"))


class SplitViewTests(unittest.TestCase):
    TOP, BOTTOM = (0, 0, 240, 118), (0, 122, 240, 240)

    def panels(self, claude=None, codex=None):
        return [claude or usage(9, 14), codex or usage(7, 1, title="Codex")]

    def test_is_a_full_screen_gif_with_both_providers_looks(self):
        data = g.render_split(self.panels())
        self.assertTrue(data.startswith(b"GIF"))
        frame = g._render_split_frame(self.panels())
        self.assertEqual(frame.size, (g.WIDTH, g.HEIGHT))
        self.assertTrue(has_colour(frame, g.THEMES["Claude"]["body"], self.TOP), "orange creature on top")
        self.assertTrue(has_colour(frame, g.THEMES["Codex"]["body"], self.BOTTOM), "blue cloud below")
        self.assertFalse(has_colour(frame, g.THEMES["Codex"]["body"], self.TOP))

    def test_alert_colours_apply_per_provider(self):
        frame = g._render_split_frame(self.panels(claude=usage(20, 20), codex=usage(96, 20, title="Codex")))
        self.assertTrue(has_colour(frame, g.STATE_COLORS["crit"], self.BOTTOM))
        self.assertFalse(has_colour(frame, g.STATE_COLORS["crit"], self.TOP))

    def test_only_the_stale_panel_is_dimmed(self):
        fresh = g._render_split_frame(self.panels())
        stale = g._render_split_frame(self.panels(claude=usage(9, 14, stale=True)))
        top_mean = lambda im: ImageStat.Stat(im.crop(self.TOP)).mean[0]
        bottom_mean = lambda im: ImageStat.Stat(im.crop(self.BOTTOM)).mean[0]
        self.assertLess(top_mean(stale), top_mean(fresh) * 0.8)
        self.assertEqual(bottom_mean(stale), bottom_mean(fresh))
        note_box = (110, 2, 240, 28)  # right of the title, where "! stale since HH:MM" goes
        self.assertTrue(has_similar_colour(stale, g.STATE_COLORS["warn"], note_box), "the 'stale since' note")
        self.assertFalse(has_similar_colour(fresh, g.STATE_COLORS["warn"], note_box))

    def test_a_single_available_provider_still_renders(self):
        self.assertTrue(g.render_split(self.panels()[:1]).startswith(b"GIF"))
        self.assertTrue(g.render_split([]).startswith(b"GIF"))

    def test_missing_percentages_render_as_dashes_without_crashing(self):
        g._render_split_frame([usage(None, None), usage(None, None, title="Codex")])


class SplitAnimationTests(unittest.TestCase):
    TOP_ART, BOTTOM_ART = (0, 0, 64, 40), (0, 122, 64, 162)  # the animated corner of each panel

    def panels(self):
        return [usage(9, 14), usage(7, 1, title="Codex")]

    def test_each_panel_plays_its_own_animation_and_the_gif_is_as_long_as_the_longer_one(self):
        gif = g.render_split(self.panels(), ["laptop", "bolt"])
        from io import BytesIO
        from PIL import Image
        frames = Image.open(BytesIO(gif)).n_frames
        self.assertLessEqual(frames, max(len(g.ANIMATIONS["laptop"]), len(g.ANIMATIONS["bolt"])))
        self.assertGreater(frames, len(g._IDLE_BOB), "more than the old idle-only bob")

    def test_the_props_of_the_single_screens_show_up(self):
        laptop = g.ANIMATIONS["laptop"]
        held = next(i for i, (_, _, hook) in enumerate(laptop) if hook is not None and i == len(laptop) - 5)  # laptop up
        frame = g._render_split_frame(self.panels(), ["laptop", "idle"], held)
        self.assertTrue(has_colour(frame, g._LAPTOP_BEZEL, self.TOP_ART), "Claude's laptop")
        bolt = g.ANIMATIONS["bolt"]
        flash = next(i for i, (_, _, hook) in enumerate(bolt) if i >= 5)  # the lightning flash frames
        frame = g._render_split_frame(self.panels(), ["idle", "bolt"], flash)
        self.assertTrue(has_colour(frame, g._BOLT_COLOR, self.BOTTOM_ART), "Codex's lightning bolt")

    def test_frames_differ_over_time(self):
        a = g._render_split_frame(self.panels(), ["coffee", "sparkle"], 0)
        b = g._render_split_frame(self.panels(), ["coffee", "sparkle"], 12)
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_a_stale_panel_holds_still(self):
        panels = [usage(9, 14, stale=True), usage(7, 1, title="Codex")]
        a = g._render_split_frame(panels, ["laptop", "idle"], 0).crop(self.TOP_ART)
        b = g._render_split_frame(panels, ["laptop", "idle"], 9).crop(self.TOP_ART)
        self.assertEqual(a.tobytes(), b.tobytes())

    def test_animation_choice_respects_each_providers_own_set(self):
        self.assertEqual(g._animation_for(usage(5, 5), "coffee"), "coffee")
        self.assertIn(g._animation_for(usage(5, 5, title="Codex"), "coffee"), g.ANIMATION_GROUPS["Codex"])
        self.assertIn(g._animation_for(usage(5, 5), "auto"), g.ANIMATION_GROUPS["Claude"])

    def test_pushing_rotates_both_providers_and_remembers_them_once_uploaded(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        with patch.object(g, "upload"):
            for _ in range(30):
                g.push_split("x", self.panels(), "split.gif")
        for title in ("Claude", "Codex"):
            history = g._recent_animations[title]
            self.assertEqual(len(history), g.RECENT_ANIMATIONS)
            self.assertEqual(len(set(history)), g.RECENT_ANIMATIONS, "never one of the last three again")
            self.assertTrue(set(history) <= set(g.ANIMATION_GROUPS[title]))

    def test_a_cancelled_upload_leaves_the_animation_history_alone(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        with patch.object(g, "upload", side_effect=g.UploadCancelled()):
            with self.assertRaises(g.UploadCancelled):
                g.push_split("x", self.panels(), "split.gif")
        self.assertEqual(g._recent_animations, {})

    def test_a_pinned_animation_is_used_and_not_counted_as_rotation(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        with patch.object(g, "upload"), patch.object(g, "render_split", wraps=g.render_split) as render:
            g.push_split("x", self.panels(), "split.gif", animation="coffee")
        self.assertEqual(render.call_args.args[1][0], "coffee")
        self.assertEqual(g._recent_animations, {})


class PaceRenderTests(unittest.TestCase):
    PACE_BOX = (120, 104, 230, 126)  # right of the "Session" reset countdown

    def paced(self, current, left_min, **extra):
        import pace
        now = datetime.now().astimezone().replace(hour=14, minute=57, second=0, microsecond=0)
        u = {"title": "Claude", "current_pct": current, "current_reset": now + timedelta(minutes=left_min),
             "weekly_pct": 10.0, "weekly_reset": now + timedelta(days=5), "now": now, **extra}
        return pace.annotate(u)

    def test_a_projection_that_runs_out_is_drawn_in_the_alert_colour(self):
        frame = g._render_frame(self.paced(80, 180))  # "Full in 30m", under an hour: red
        self.assertTrue(has_similar_colour(frame, g.STATE_COLORS["crit"], self.PACE_BOX, at_least=6))

    def test_a_calm_projection_is_drawn_quietly(self):
        calm = g._render_frame(self.paced(62, 90))
        self.assertFalse(has_similar_colour(calm, g.STATE_COLORS["crit"], self.PACE_BOX))
        self.assertTrue(has_similar_colour(calm, g.MUTED, self.PACE_BOX, at_least=6), "'~89% at reset'")

    def test_no_projection_means_nothing_extra_on_screen(self):
        early_usage = self.paced(30, 290)  # 10 minutes into the session
        self.assertIsNone(early_usage["pace"]["current"])
        early = g._render_frame(early_usage)
        plain = g._render_frame({**early_usage, "pace": {"current": None, "weekly": None}})
        self.assertEqual(early.crop(self.PACE_BOX).tobytes(), plain.crop(self.PACE_BOX).tobytes(), "session: nothing added")

    def test_stale_numbers_show_no_projection(self):
        fresh = g._render_frame(self.paced(80, 180))
        stale = g._render_frame(self.paced(80, 180, stale=True))
        self.assertFalse(has_similar_colour(stale, g.STATE_COLORS["crit"], self.PACE_BOX))
        self.assertTrue(has_similar_colour(fresh, g.STATE_COLORS["crit"], self.PACE_BOX, at_least=6))

    def test_the_projection_never_overlaps_the_reset_countdown(self):
        # a long countdown leaves no room: the countdown wins and the projection is skipped
        from PIL import ImageDraw, ImageFont
        canvas = g.Image.new("RGB", (240, 240))
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=14)
        crowded = draw.textlength("x" * 40, font=font) + 14  # the countdown reaches almost to the right edge
        g._draw_pace(draw, ("Full in 2h 20m", "warn"), right=226, y=100, left_end=crowded, font=font)
        self.assertIsNone(canvas.getbbox(), "no room next to the countdown: nothing is drawn")
        g._draw_pace(draw, ("Full in 2h 20m", "warn"), right=226, y=100, left_end=20, font=font)
        self.assertIsNotNone(canvas.getbbox(), "with room it is drawn")

    def test_split_rows_show_the_projection_too(self):
        panel = [self.paced(80, 180), {**self.paced(30, 290)}]
        frame = g._render_split_frame(panel)
        self.assertTrue(has_similar_colour(frame, g.STATE_COLORS["crit"], (150, 60, 232, 76), at_least=4))


class StatsViewTests(unittest.TestCase):
    TOP, BOTTOM = (0, 0, 240, 118), (0, 122, 240, 240)

    @staticmethod
    def activity(requests=(5, 0, 12, 30, 8, 22, 40), sessions=4):
        days = [{"date": f"2026-10-{2 + i:02d}", "requests": n} for i, n in enumerate(requests)]
        return {"24h": {"requests": requests[-1], "sessions": 1}, "7d": {"requests": sum(requests), "sessions": sessions}, "days": days}

    def claude(self, **extra):
        return {**usage(9, 14), "activity": self.activity(), **extra}

    def codex(self, **extra):
        return {**usage(7, 1, title="Codex"), "activity": self.activity((100, 90, 5, 0, 60, 300, 26), 17), **extra}

    def test_both_panels_have_exactly_the_same_layout(self):
        frame = g._render_stats_frame([self.claude(), self.codex(activity=self.claude()["activity"])])
        top, bottom = frame.crop((0, 40, 240, 118)), frame.crop((0, 162, 240, 240))  # everything below each header
        # same numbers, so the only differences are the providers' colours: the shape of what's drawn is identical
        shape = lambda im: [(x, y) for y in range(im.height) for x in range(im.width) if im.getpixel((x, y)) != g._hex_rgb(g.BG)]
        self.assertEqual(shape(top), shape(bottom))

    def test_the_chart_has_a_bar_per_day_and_marks_today(self):
        panel = self.claude()
        frame = g._render_stats_frame([panel])
        crop = (10, 60, 230, 108)
        dim, bright = g._blend(g.THEMES["Claude"]["body"], g.BG, 0.45), g.THEMES["Claude"]["body"]
        pixels = list(frame.crop(crop).getdata())
        self.assertIn(g._hex_rgb(bright), pixels, "today's bar in the full colour")
        self.assertIn(tuple(dim), pixels, "earlier days dimmer")
        weekday_names = [s for s in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")]
        self.assertEqual(len(panel["activity"]["days"]), len(weekday_names))

    def test_the_tallest_day_is_the_tallest_bar_and_empty_days_stay_flat(self):
        from PIL import ImageDraw
        canvas = g.Image.new("RGB", (240, 240), g.BG)
        draw = ImageDraw.Draw(canvas)
        days = [{"date": f"2026-10-{2 + i:02d}", "requests": n} for i, n in enumerate((0, 10, 100, 50, 0, 25, 100))]
        fonts = {"tiny": g.ImageFont.load_default(size=10)}
        g._draw_week_chart(draw, days, left=10, right=230, baseline=100, height=34, color="#FFFFFF", fonts=fonts)
        bar_colours = {g._hex_rgb("#FFFFFF"), tuple(g._blend("#FFFFFF", g.BG, 0.45)), g._hex_rgb(g.PILL_BG)}

        def bar_height(i):  # walk up from the baseline while the pixels are the bar's (not the number above it)
            x, y, height = round(10 + i * (220 / 7) + (220 / 7) / 2), 99, 0
            while canvas.getpixel((x, y)) in bar_colours:
                height, y = height + 1, y - 1
            return height
        heights = [bar_height(i) for i in range(7)]
        self.assertEqual(heights[2], heights[6], "equal days, equal bars")
        self.assertGreater(heights[2], heights[3])
        self.assertGreater(heights[3], heights[5])
        self.assertGreater(heights[5], heights[1])
        self.assertLessEqual(heights[0], 3, "a day with no activity is a flat stub")

    def test_the_week_is_scaled_to_the_providers_own_busiest_day(self):
        quiet = g._render_stats_frame([self.claude(activity=self.activity((1, 1, 2, 1, 1, 1, 2)))])
        busy = g._render_stats_frame([self.claude(activity=self.activity((100, 100, 200, 100, 100, 100, 200)))])
        bar = (10, 60, 230, 108)
        self.assertEqual(quiet.crop(bar).tobytes()[:0], b"")  # (renders)
        bright = g._hex_rgb(g.THEMES["Claude"]["body"])
        count = lambda im: sum(1 for p in im.crop(bar).getdata() if p == bright)
        self.assertAlmostEqual(count(quiet), count(busy), delta=count(busy) * 0.25, msg="same shape whatever the volume")

    def test_renders_a_still_gif_with_both_panels(self):
        gif = g.render_stats([self.claude(), self.codex()])
        from io import BytesIO
        from PIL import Image
        self.assertTrue(gif.startswith(b"GIF"))
        self.assertEqual(Image.open(BytesIO(gif)).n_frames, 1, "a still: small and quick to upload")
        frame = g._render_stats_frame([self.claude(), self.codex()])
        self.assertTrue(has_colour(frame, g.THEMES["Claude"]["body"], self.TOP))
        self.assertTrue(has_colour(frame, g.THEMES["Codex"]["body"], self.BOTTOM))

    def test_the_headline_numbers_are_drawn_in_full_text_colour(self):
        frame = g._render_stats_frame([self.claude(), self.codex()])
        self.assertTrue(has_similar_colour(frame, g.TEXT, (10, 28, 230, 58), at_least=20), "24h and 7d lines")
        self.assertTrue(has_similar_colour(frame, g.TEXT, (10, 150, 230, 180), at_least=20), "...in the second panel too")

    def test_it_says_when_it_is_counting_or_when_there_are_no_logs(self):
        counting = g._render_stats_frame([self.claude(activity=None)])
        none_found = g._render_stats_frame([self.claude(activity={})])
        normal = g._render_stats_frame([self.claude()])
        self.assertNotEqual(counting.tobytes(), none_found.tobytes(), "different messages")
        self.assertTrue(has_similar_colour(counting, g.MUTED, (10, 36, 230, 62), at_least=10))
        self.assertFalse(has_colour(counting, g.THEMES["Claude"]["body"], (10, 60, 230, 108)), "no chart without data")
        self.assertTrue(has_colour(normal, g.THEMES["Claude"]["body"], (10, 60, 230, 108)))

    def test_it_copes_with_missing_data(self):
        g._render_stats_frame([{**usage(1, 1)}, {**usage(1, 1, title="Codex")}])
        g._render_stats_frame([self.claude()])
        g._render_stats_frame([])
        g._render_stats_frame([self.claude(activity=self.activity((0, 0, 0, 0, 0, 0, 0)))])  # nothing in a week

    def test_a_stale_panel_is_dimmed_and_dated(self):
        fresh = g._render_stats_frame([self.claude(), self.codex()])
        stale = g._render_stats_frame([self.claude(stale=True), self.codex()])
        body = (10, 34, 230, 116)  # the lines of text, below the header (where the "stale since" note goes)
        brightest = lambda im: max(ch[1] for ch in im.crop(body).getextrema())
        self.assertLess(brightest(stale), brightest(fresh) * 0.7, "the text is dimmed")
        self.assertTrue(has_similar_colour(stale, g.STATE_COLORS["warn"], (110, 2, 240, 28)))
        self.assertEqual(stale.crop(self.BOTTOM).tobytes(), fresh.crop(self.BOTTOM).tobytes(), "the other panel is untouched")

    def test_long_lines_are_shortened_to_fit(self):
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(g.Image.new("RGB", (240, 240)))
        font = ImageFont.load_default(size=12)
        fitted = g._fit(draw, "A very long line of text " * 6, font, 220)
        self.assertTrue(fitted.endswith("..."))
        self.assertLessEqual(draw.textlength(fitted, font=font), 220)
        self.assertEqual(g._fit(draw, "short", font, 220), "short")


def sample_activity(**override):
    base = {
        "24h": {"requests": 40, "sessions": 2}, "7d": {"requests": 400, "sessions": 9},
        "days": [{"date": f"2026-10-{2 + i:02d}", "requests": 10 * i} for i in range(7)],
        "projects": [{"name": "Acme App", "requests": 200}, {"name": "Storefront/frontend", "requests": 120}, {"name": "Docs", "requests": 60}],
        "models": [{"name": "Opus 5.5", "requests": 280}, {"name": "Sonnet 5", "requests": 100}, {"name": "Haiku 4.5", "requests": 20}],
        "total": 400, "hours": [0, 0, 0, 0, 0, 0, 0, 5, 20, 40, 60, 80, 100, 90, 70, 50, 30, 20, 10, 5, 0, 0, 0, 0],
    }
    return {**base, **override}


class BreakdownViewTests(unittest.TestCase):
    TOP, BOTTOM = (0, 0, 240, 118), (0, 122, 240, 240)
    ROWS = (10, 42, 230, 118)

    def panels(self, **extra):
        return [{**usage(9, 14), "activity": sample_activity(), **extra},
                {**usage(7, 1, title="Codex"), "activity": sample_activity(), **extra}]

    def test_renders_a_still_gif_with_both_providers_in_their_colours(self):
        from io import BytesIO
        from PIL import Image
        gif = g.render_breakdown(self.panels())
        self.assertEqual(Image.open(BytesIO(gif)).n_frames, 1)
        frame = g._render_breakdown_frame(self.panels())
        self.assertTrue(has_colour(frame, g.THEMES["Claude"]["body"], self.TOP))
        self.assertTrue(has_colour(frame, g.THEMES["Codex"]["body"], self.BOTTOM))

    def test_projects_use_the_providers_colour_and_models_the_secondary_one(self):
        frame = g._render_breakdown_frame(self.panels())
        theme = g.THEMES["Claude"]
        left, right = (10, 42, 118, 118), (126, 42, 230, 118)
        self.assertTrue(has_colour(frame, theme["body"], left))
        self.assertTrue(has_colour(frame, theme["weekly"], right))
        self.assertFalse(has_colour(frame, theme["weekly"], left))

    def test_bar_lengths_follow_the_shares(self):
        frame = g._render_breakdown_frame(self.panels())
        body = g._hex_rgb(g.THEMES["Claude"]["body"])

        def bar_width(row):  # the coloured stretch of one row's bar (the row's name is above it)
            y = 42 + row * 25 + 16
            return sum(1 for x in range(10, 118) if frame.getpixel((x, y)) == body)

        first, second, third = (bar_width(i) for i in range(3))  # 50%, 30%, 15% of 104 px
        self.assertGreater(first, second)
        self.assertGreater(second, third)
        self.assertAlmostEqual(first, 52, delta=4)
        self.assertAlmostEqual(second, 31, delta=4)

    def test_percentages_and_names_are_drawn(self):
        frame = g._render_breakdown_frame(self.panels())
        self.assertTrue(has_similar_colour(frame, g.TEXT, (10, 40, 118, 60), at_least=15), "a project name")
        self.assertTrue(has_similar_colour(frame, g.MUTED, (85, 40, 118, 60), at_least=6), "its percentage")

    def test_a_long_name_is_shortened_and_never_runs_into_the_next_column(self):
        long = sample_activity(projects=[{"name": "A-very-very-long-project-name-indeed", "requests": 400}])
        frame = g._render_breakdown_frame([{**usage(9, 14), "activity": long}])
        gap = frame.crop((119, 42, 125, 60))  # the gutter between the two columns
        self.assertIsNone(gap.convert("L").point(lambda v: 255 if v > 60 else 0).getbbox())

    def test_tiny_shares_say_less_than_one_percent(self):
        activity = sample_activity(total=10000, projects=[{"name": "Big", "requests": 9990}, {"name": "Tiny", "requests": 3}])
        frame = g._render_breakdown_frame([{**usage(1, 1), "activity": activity}])  # must render
        self.assertLess(usage_stats_shares(activity)[1][1], 0.01)
        self.assertTrue(has_similar_colour(frame, g.MUTED, (85, 66, 118, 84), at_least=6), "'<1%' beside the tiny one")

    def test_messages_for_counting_no_logs_and_an_empty_week(self):
        counting = g._render_breakdown_frame([{**usage(1, 1), "activity": None}])
        nothing = g._render_breakdown_frame([{**usage(1, 1), "activity": {}}])
        quiet = g._render_breakdown_frame([{**usage(1, 1), "activity": sample_activity(total=0, projects=[], models=[])}])
        self.assertEqual(len({counting.tobytes(), nothing.tobytes(), quiet.tobytes()}), 3, "three different messages")
        for frame in (counting, nothing, quiet):
            self.assertTrue(has_similar_colour(frame, g.MUTED, (10, 36, 230, 62), at_least=10))
            self.assertFalse(has_colour(frame, g.THEMES["Claude"]["body"], (10, 60, 230, 118)))

    def test_a_stale_panel_is_dimmed_and_dated_and_the_other_untouched(self):
        fresh = g._render_breakdown_frame(self.panels())
        stale = g._render_breakdown_frame([{**self.panels()[0], "stale": True}, self.panels()[1]])
        brightest = lambda im: max(ch[1] for ch in im.crop(self.ROWS).getextrema())
        self.assertLess(brightest(stale), brightest(fresh) * 0.7)
        self.assertTrue(has_similar_colour(stale, g.STATE_COLORS["warn"], (110, 2, 240, 28)))
        self.assertEqual(stale.crop(self.BOTTOM).tobytes(), fresh.crop(self.BOTTOM).tobytes())


def usage_stats_shares(activity):
    import usage_stats
    return usage_stats.shares(activity["projects"], activity["total"], 3)


class HoursViewTests(unittest.TestCase):
    TOP, BOTTOM = (0, 0, 240, 118), (0, 122, 240, 240)
    CHART = (12, 46, 228, 104)

    def panels(self, **extra):
        return [{**usage(9, 14), "activity": sample_activity(), **extra},
                {**usage(7, 1, title="Codex"), "activity": sample_activity(), **extra}]

    def test_renders_a_still_gif_with_both_providers(self):
        from io import BytesIO
        from PIL import Image
        self.assertEqual(Image.open(BytesIO(g.render_hours(self.panels()))).n_frames, 1)
        frame = g._render_hours_frame(self.panels())
        self.assertTrue(has_colour(frame, g.THEMES["Claude"]["body"], self.CHART))
        self.assertTrue(has_colour(frame, g.THEMES["Codex"]["body"], (12, 168, 228, 226)))

    def test_the_caption_names_the_busiest_stretch(self):
        import usage_stats
        window = usage_stats.busiest_hours(sample_activity()["hours"])
        self.assertEqual(usage_stats.hours_text(window), "12-2 PM")
        frame = g._render_hours_frame(self.panels())
        self.assertTrue(has_similar_colour(frame, g.TEXT, (10, 26, 230, 42), at_least=25))

    def test_the_busiest_two_hours_are_highlighted_and_the_rest_dimmer(self):
        frame = g._render_hours_frame([self.panels()[0]])
        bright = g._hex_rgb(g.THEMES["Claude"]["body"])
        dim = tuple(g._blend(g.THEMES["Claude"]["body"], g.BG, 0.45))

        def colour_at(hour):  # the bar's colour just above its baseline
            return frame.getpixel((12 + hour * 9 + 3, 100))

        self.assertEqual(colour_at(12), bright)
        self.assertEqual(colour_at(13), bright)
        self.assertEqual(colour_at(11), dim, "busy, but not in the busiest stretch")
        self.assertEqual(colour_at(15), dim)
        self.assertEqual(colour_at(2), g._hex_rgb(g.PILL_BG), "no activity: a flat stub")

    def test_bar_heights_follow_the_requests(self):
        frame = g._render_hours_frame([self.panels()[0]])

        def height(hour):
            x, y, h = 12 + hour * 9 + 3, 101, 0
            while frame.getpixel((x, y)) != g._hex_rgb(g.BG) and h < 60:  # up to the background above the bar
                y, h = y - 1, h + 1
            return h

        self.assertGreater(height(12), height(9))
        self.assertGreater(height(9), height(18))
        self.assertLessEqual(height(2), 3)

    def test_a_window_crossing_midnight_highlights_both_sides(self):
        hours = [0] * 24
        hours[23], hours[0], hours[12] = 50, 60, 10
        frame = g._render_hours_frame([{**usage(1, 1), "activity": sample_activity(hours=hours)}])
        bright = g._hex_rgb(g.THEMES["Claude"]["body"])
        self.assertEqual(frame.getpixel((12 + 23 * 9 + 3, 100)), bright)
        self.assertEqual(frame.getpixel((12 + 0 * 9 + 3, 100)), bright)
        self.assertNotEqual(frame.getpixel((12 + 12 * 9 + 3, 100)), bright)

    def test_the_hour_axis_is_labelled(self):
        frame = g._render_hours_frame(self.panels())
        self.assertTrue(has_similar_colour(frame, g.MUTED, (12, 102, 228, 114), at_least=20))

    def test_messages_when_there_is_nothing_to_show(self):
        counting = g._render_hours_frame([{**usage(1, 1), "activity": None}])
        nothing = g._render_hours_frame([{**usage(1, 1), "activity": {}}])
        quiet = g._render_hours_frame([{**usage(1, 1), "activity": sample_activity(hours=[0] * 24)}])
        self.assertEqual(len({counting.tobytes(), nothing.tobytes(), quiet.tobytes()}), 3)
        for frame in (counting, nothing, quiet):
            self.assertFalse(has_colour(frame, g.THEMES["Claude"]["body"], self.CHART), "no chart without data")

    def test_a_stale_panel_is_dimmed(self):
        fresh = g._render_hours_frame(self.panels())
        stale = g._render_hours_frame([{**self.panels()[0], "stale": True}, self.panels()[1]])
        brightest = lambda im: max(ch[1] for ch in im.crop((10, 40, 230, 116)).getextrema())
        self.assertLess(brightest(stale), brightest(fresh) * 0.7)
        self.assertEqual(stale.crop(self.BOTTOM).tobytes(), fresh.crop(self.BOTTOM).tobytes())


class WorkingIndicatorTests(unittest.TestCase):
    DOT = g._hex_rgb(g.WORKING_COLOR)

    def screen(self, **extra):
        return g._render_frame({**usage(36, 17), **extra})

    def test_the_single_screen_shows_a_green_working_badge_only_while_working(self):
        box = (140, 4, 234, 28)
        on, off = self.screen(working=True), self.screen()
        self.assertTrue(has_similar_colour(on, g.WORKING_COLOR, box, at_least=15))
        self.assertFalse(has_similar_colour(off, g.WORKING_COLOR, box))

    def test_codex_keeps_its_resets_chip_beside_the_badge(self):
        codex = {**usage(36, 17, title="Codex"), "codex_extra": {"free_resets": 3, "next_reset_expires_days": 12}}
        screen = g._render_frame({**codex, "working": True})
        self.assertTrue(has_similar_colour(screen, g.WORKING_COLOR, (140, 4, 234, 20), at_least=10), "the badge, up top")
        self.assertTrue(has_similar_colour(screen, g.THEMES["Codex"]["weekly"], (140, 21, 234, 38), at_least=8), "the chip below it")

    def test_every_view_of_both_providers_can_show_the_dot(self):
        dot_pixels = lambda im, box: sum(1 for p in im.crop(box).getdata() if p == self.DOT)
        busy = {**usage(9, 14), "working": True, "activity": sample_activity()}
        idle = {**busy, "working": False}
        for name, render, box in (
            ("stats", g._render_stats_frame, (150, 4, 232, 30)),
            ("breakdown", g._render_breakdown_frame, (150, 4, 232, 30)),
            ("hours", g._render_hours_frame, (150, 4, 232, 30)),
            ("split", g._render_split_frame, (150, 4, 232, 34)),
        ):
            self.assertGreater(dot_pixels(render([busy, idle]), box), 10, f"{name}: the dot")
            self.assertEqual(dot_pixels(render([idle, idle]), box), 0, f"{name}: no dot when idle")

    def test_the_dot_is_not_drawn_on_a_stale_panel(self):
        stale = g._render_stats_frame([{**usage(9, 14), "working": True, "stale": True, "activity": sample_activity()}])
        self.assertEqual(sum(1 for p in stale.crop((150, 4, 232, 30)).getdata() if p == self.DOT), 0)

    def test_a_busy_agents_mascot_works_instead_of_rotating(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        with patch.object(g, "upload"), patch.object(g, "render_animation", wraps=g.render_animation) as render:
            g.push_usage("x", {**usage(9, 14), "working": True}, "auto")
            g.push_usage("x", {**usage(7, 1, title="Codex"), "working": True}, "random")
            g.push_usage("x", usage(9, 14), "auto")  # idle again: back to a random one
        self.assertEqual([c.args[1] for c in render.call_args_list[:2]], ["typing", "code"])
        self.assertEqual(len(g._recent_animations["Claude"]), 1, "only the idle push counts for the rotation")
        self.assertNotIn("Codex", g._recent_animations)

    def test_a_pinned_animation_wins_over_the_working_one(self):
        from unittest.mock import patch
        with patch.object(g, "upload"), patch.object(g, "render_animation", wraps=g.render_animation) as render:
            g.push_usage("x", {**usage(9, 14), "working": True}, "coffee")
        self.assertEqual(render.call_args.args[1], "coffee")

    def test_split_panels_use_the_working_animations_without_touching_the_history(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        panels = [{**usage(9, 14), "working": True}, {**usage(7, 1, title="Codex"), "working": True}]
        with patch.object(g, "upload"), patch.object(g, "render_split", wraps=g.render_split) as render:
            g.push_split("x", panels, "split.gif")
        self.assertEqual(render.call_args.args[1], ["typing", "code"])
        self.assertEqual(g._recent_animations, {})

    def test_the_working_animations_exist(self):
        for title, name in g.WORKING_ANIMATION.items():
            self.assertIn(name, g.ANIMATION_GROUPS[title])
        for title, name in g.WAITING_ANIMATION.items():
            self.assertIn(name, g.ANIMATION_GROUPS[title])


class WaitingIndicatorTests(unittest.TestCase):
    AMBER = g._hex_rgb(g.WAITING_COLOR)

    def screen(self, **extra):
        return g._render_frame({**usage(36, 17), **extra})

    def test_waiting_shows_an_amber_badge_instead_of_the_green_one(self):
        box = (140, 4, 234, 28)
        waiting = self.screen(working=True, waiting=True)
        self.assertTrue(has_similar_colour(waiting, g.WAITING_COLOR, box, at_least=15))
        self.assertFalse(has_similar_colour(waiting, g.WORKING_COLOR, box), "not green as well")
        self.assertTrue(has_similar_colour(self.screen(working=True), g.WORKING_COLOR, box, at_least=15))

    def test_the_two_badges_say_different_words(self):
        self.assertEqual(g._busy_style({"working": True}), ("Working", g.WORKING_COLOR))
        self.assertEqual(g._busy_style({"working": True, "waiting": True}), ("Waiting", g.WAITING_COLOR))
        self.assertIsNone(g._busy_style({}))
        self.assertIsNone(g._busy_style({"working": False, "waiting": False}))

    def test_waiting_wins_the_animation_too(self):
        self.assertEqual(g._busy_animation({"title": "Claude", "working": True}), "typing")
        self.assertEqual(g._busy_animation({"title": "Claude", "working": True, "waiting": True}), "eureka")
        self.assertEqual(g._busy_animation({"title": "Codex", "working": True, "waiting": True}), "sparkle")
        self.assertIsNone(g._busy_animation({"title": "Codex"}))

    def test_every_view_shows_the_amber_dot(self):
        dots = lambda im, box: sum(1 for p in im.crop(box).getdata() if p == self.AMBER)
        waiting = {**usage(9, 14), "working": True, "waiting": True, "activity": sample_activity()}
        idle = {**waiting, "working": False, "waiting": False}
        for name, render, box in (("stats", g._render_stats_frame, (150, 4, 232, 30)),
                                  ("breakdown", g._render_breakdown_frame, (150, 4, 232, 30)),
                                  ("hours", g._render_hours_frame, (150, 4, 232, 30)),
                                  ("split", g._render_split_frame, (150, 4, 232, 34))):
            self.assertGreater(dots(render([waiting, idle]), box), 10, name)
            self.assertEqual(dots(render([idle, idle]), box), 0, name)

    def test_pushing_uses_the_waiting_animation_and_leaves_the_rotation_alone(self):
        from unittest.mock import patch
        g._recent_animations.clear()
        with patch.object(g, "upload"), patch.object(g, "render_animation", wraps=g.render_animation) as render:
            g.push_usage("x", {**usage(9, 14), "working": True, "waiting": True}, "auto")
        self.assertEqual(render.call_args.args[1], "eureka")
        self.assertEqual(g._recent_animations, {})
        with patch.object(g, "upload"), patch.object(g, "render_split", wraps=g.render_split) as split:
            g.push_split("x", [{**usage(9, 14), "working": True, "waiting": True}, {**usage(7, 1, title="Codex")}], "s.gif")
        self.assertEqual(split.call_args.args[1][0], "eureka")
        self.assertEqual(list(g._recent_animations), ["Codex"], "only the idle panel counts for the rotation")

    def test_the_codex_resets_chip_still_fits_under_the_waiting_badge(self):
        codex = {**usage(36, 17, title="Codex"), "codex_extra": {"free_resets": 3, "next_reset_expires_days": 12},
                 "working": True, "waiting": True}
        screen = g._render_frame(codex)
        self.assertTrue(has_similar_colour(screen, g.THEMES["Codex"]["weekly"], (140, 21, 234, 38), at_least=8))


class CodexResetsRenderTests(unittest.TestCase):
    CHIP_BOX = (130, 10, 232, 32)  # top right of Codex's screen

    def screen(self, title="Codex", **extra):
        return g._render_frame({**usage(36, 17, title=title), **extra})

    def test_the_chip_shows_on_codex_screen_in_urgency_colours(self):
        calm = self.screen(codex_extra={"free_resets": 3, "next_reset_expires_days": 12})
        soon = self.screen(codex_extra={"free_resets": 3, "next_reset_expires_days": 3})
        today = self.screen(codex_extra={"free_resets": 1, "next_reset_expires_days": 1})
        self.assertTrue(has_similar_colour(calm, g.THEMES["Codex"]["weekly"], self.CHIP_BOX, at_least=8))
        self.assertTrue(has_similar_colour(soon, g.STATE_COLORS["warn"], self.CHIP_BOX, at_least=8))
        self.assertTrue(has_similar_colour(today, g.STATE_COLORS["crit"], self.CHIP_BOX, at_least=8))
        self.assertFalse(has_similar_colour(calm, g.STATE_COLORS["crit"], self.CHIP_BOX))

    def test_no_chip_without_resets_and_never_on_claude_or_in_the_split_and_stats_views(self):
        none = self.screen(codex_extra={"free_resets": 0, "next_reset_expires_days": None})
        plain = self.screen()
        self.assertEqual(none.crop(self.CHIP_BOX).tobytes(), plain.crop(self.CHIP_BOX).tobytes())
        claude = self.screen(title="Claude", codex_extra={"free_resets": 3, "next_reset_expires_days": 1})
        self.assertFalse(has_similar_colour(claude, g.STATE_COLORS["crit"], self.CHIP_BOX))
        codex = {**usage(36, 17, title="Codex"), "codex_extra": {"free_resets": 3, "next_reset_expires_days": 1}}
        split = g._render_split_frame([usage(10, 10), codex])
        stats = g._render_stats_frame([usage(10, 10), codex])
        for view in (split, stats):
            self.assertFalse(has_similar_colour(view, g.STATE_COLORS["crit"], (120, 122, 240, 150)), "resets belong to Codex's own screen")

    def test_the_chip_does_not_collide_with_the_title(self):
        frame = self.screen(codex_extra={"free_resets": 3, "next_reset_expires_days": 12})
        title_box = (62, 6, 128, 38)  # where "Codex" is
        chip_in_title = has_similar_colour(frame, g.THEMES["Codex"]["weekly"], title_box, at_least=1)
        self.assertFalse(chip_in_title)


if __name__ == "__main__":
    unittest.main()
