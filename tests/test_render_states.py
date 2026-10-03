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


if __name__ == "__main__":
    unittest.main()
