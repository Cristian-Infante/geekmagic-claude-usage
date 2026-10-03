"""Each provider's pixel-art mascot and colours."""

from __future__ import annotations

from PIL import Image, ImageDraw

from geekmagic.render.palette import ACCENT, CURRENT_ACCENT, WEEKLY_ACCENT


# Pixel-art mascot bitmap: '1' = orange body, '2' = black eye, '0' = background.
# 14 columns x 8 rows — squat and chubby: square torso, two stubby side
# arms, and four thin legs, without the extra height of a taller torso.
CLAUDE_BITMAP = (
    "00011111111000",
    "00011211211000",
    "01111111111110",
    "01111111111110",
    "00011111111000",
    "00011111111000",
    "00010100101000",
    "00010100101000",
)


# Codex's mascot: a little cloud (an original pixel-art take on a terminal-in-the-cloud),
# same 14x8 footprint as the Claude one. Its `>_` prompt is drawn per frame by _prompt_layer.
CODEX_BITMAP = (
    "00000000000000",
    "00000011110000",
    "00011011111100",
    "00111111111110",
    "01111111111111",
    "01111111111111",
    "01111111111111",
    "00111111111110",
)


INK = "#0F0D0B"


# Per-provider look, picked by usage["title"]: mascot bitmap + body colour, and the two bar accents.
THEMES = {
    "Claude": {"bitmap": CLAUDE_BITMAP, "body": ACCENT, "current": CURRENT_ACCENT, "weekly": WEEKLY_ACCENT},
    "Codex": {"bitmap": CODEX_BITMAP, "body": "#4F9DFF", "current": "#4F9DFF", "weekly": "#B79CFF"},
}


def theme_for(usage: dict) -> dict:
    return THEMES.get(usage.get("title"), THEMES["Claude"])


def draw_mascot(image: Image.Image, top_left: tuple[int, int], cell_size: int, theme: dict | None = None) -> None:
    """Blocky pixel-art mascot, drawn cell by cell so it stays crisp at small sizes."""
    theme = theme or THEMES["Claude"]
    x0, y0 = top_left
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(theme["bitmap"]):
        for col, cell in enumerate(line):
            if cell == "0":
                continue
            fill = theme["body"] if cell == "1" else INK
            x = x0 + col * cell_size
            y = y0 + row * cell_size
            draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=fill)


MASCOT_TOP = 10
