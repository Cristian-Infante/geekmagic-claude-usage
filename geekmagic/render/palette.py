"""Colours and sizes shared by every screen."""

from __future__ import annotations


WIDTH = HEIGHT = 240
SPLIT_PANEL_H = 118  # two panels of this height, a 4 px divider between them


# Claude's own palette (warm ink + a vivid, saturated orange), laid out
# like a watch face: bold numbers, pill badges, no card boxes.
BG = "#18160F"


PILL_BG = "#4A3F4D"


# Past the thresholds in alerts.py the percentage and its bar swap the provider's accent for these.
STATE_COLORS = {"warn": "#FFD23F", "crit": "#FF2D55"}


CURRENT_ACCENT = "#FA5407"


WEEKLY_ACCENT = "#E8B24D"


TEXT = "#F5F4EF"


MUTED = "#C7C2BC"


ACCENT = CURRENT_ACCENT  # used by the mascot's body


WORKING_COLOR = "#4ADE80"  # the dot (and word) that says an agent is running right now


WAITING_COLOR = "#FFB020"  # ...and amber when it has stopped and is waiting for you


def hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def blend(color: str, over: str, amount: float) -> tuple[int, int, int]:
    """`color` mixed with `over` (0 = all `over`, 1 = all `color`)."""
    a, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (color, over))
    return tuple(round(b[k] + (a[k] - b[k]) * amount) for k in range(3))
