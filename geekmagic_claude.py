#!/usr/bin/env python3
"""Push Claude Code's /usage limits to a GeekMagic SmallTV Ultra (stock firmware).

Reads usage via Claude Code's own CLI (`claude /usage`), zero cost, and
renders a 240x240 animated GIF — a pixel-art mascot plus Session/Weekly
usage bars — uploaded straight into the device's stock Photo Album.
See ANIMATIONS for the available mascot animations.

Usage:
    python3 geekmagic_claude.py --ip 192.168.1.18
    python3 geekmagic_claude.py --ip 192.168.1.18 --loop 60
    python3 geekmagic_claude.py --ip 192.168.1.18 --animation coffee
"""

from __future__ import annotations

import argparse
import functools
import http.client
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import alerts
import pace
import usage_stats

WIDTH = HEIGHT = 240
IMAGE_NAME = "claude-usage.gif"
IMAGE_NAMES = {"claude": IMAGE_NAME, "codex": "codex-usage.gif"}  # one file per provider, so switching is just /set?img
_OLD_IMAGE_NAMES = ("claude-usage.jpg",)  # cleaned up on first run after the GIF switch

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

_SESSION_RE = re.compile(r"^Current session:\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s+(.+))?$", re.MULTILINE)
_WEEK_RE = re.compile(r"^Current week(?:\s*\([^)]*\))?:\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s+(.+))?$", re.MULTILINE)
_RESET_RE = re.compile(
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})(?:,|\s+at)\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<meridiem>am|pm)\s+\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)


class UsageError(RuntimeError):
    pass


def fetch_usage() -> dict:
    """Call Claude Code's built-in /usage command. Guaranteed $0 cost."""
    try:
        proc = subprocess.run(
            [
                _which("claude") or "claude", "-p", "--safe-mode",
                "--output-format", "json",
                "--max-budget-usd", "0.000001",
                "--tools", "",
                "--no-session-persistence",
                "--no-chrome",
                "/usage",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise UsageError("`claude` CLI not found in PATH. Install/open Claude Code first.") from e
    except subprocess.TimeoutExpired as e:
        raise UsageError("Timed out waiting for `claude /usage`.") from e

    if proc.returncode != 0:
        raise UsageError(f"claude exited {proc.returncode}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)

    cost = payload.get("total_cost_usd", 0)
    if float(cost) != 0:
        raise UsageError("Refusing result: /usage unexpectedly incurred cost.")

    result = payload.get("result")
    if payload.get("is_error") or not isinstance(result, str):
        raise UsageError("Claude Code did not return usage text.")
    return pace.annotate(_parse(result))


CODEX_TIMEOUT = 30


def _which(name: str) -> str | None:
    """Like shutil.which, plus the usual install folders a background job (launchd, Task Scheduler) may not have on PATH."""
    extra = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(extra))


def _find_codex() -> str | None:
    """`codex` from PATH, else the newest copy bundled with a VS Code-family ChatGPT/Codex extension."""
    found = _which("codex")
    if found:
        return found
    binary = "codex.exe" if sys.platform == "win32" else "codex"
    bundled = [
        p
        for editor in (".vscode", ".vscode-insiders", ".cursor")
        for p in (Path.home() / editor / "extensions").glob(f"openai.chatgpt-*/bin/*/{binary}")
    ]
    bundled.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(bundled[0]) if bundled else None


def _fetch_codex_rate_limits() -> dict:
    """Query the authenticated Codex app-server without starting an AI turn."""
    executable = _find_codex()
    if not executable:
        raise UsageError("`codex` CLI not found in PATH. Install Codex CLI and run `codex login`.")
    try:
        proc = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        raise UsageError("Could not start `codex app-server`.") from e

    responses = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in proc.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + CODEX_TIMEOUT

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("Timed out querying Codex limits. Check your connection and `codex login`.")
            try:
                line = responses.get(timeout=remaining)
            except queue.Empty as e:
                raise UsageError("Timed out querying Codex limits. Check your connection and `codex login`.") from e
            if line is None:
                raise UsageError("Codex app-server exited before returning limits. Check `codex login`.")
            try:
                message = json.loads(line)
            except ValueError as e:
                raise UsageError("Codex app-server returned invalid JSON.") from e
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # Notifications can arrive between request responses.
            if "error" in message:
                raise UsageError("Codex could not read account limits. Check your connection and `codex login` with ChatGPT.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise UsageError("Codex app-server returned an invalid response.")
            return result

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "geekmagic_usage", "title": "GeekMagic Usage", "version": "1.0.0",
        }}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        result = receive(2)
        buckets = result.get("rateLimitsByLimitId")
        # Prefer the Codex bucket when multiple model quotas are returned.
        limits = buckets.get("codex") if isinstance(buckets, dict) else None
        if limits is None:
            limits = result.get("rateLimits")
        if not isinstance(limits, dict) or not any(limits.get(w) for w in ("primary", "secondary")):
            raise UsageError("Codex returned no account limits. Sign in with ChatGPT using `codex login`.")
        return {**limits, "_reset_credits": result.get("rateLimitResetCredits")}  # the free resets sit beside the limits
    except OSError as e:
        raise UsageError("Lost connection to Codex app-server.") from e
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=2)
        proc.stdout.close()


def fetch_codex_usage() -> dict:
    """Fetch fresh account limits, even when the user is not actively using Codex."""
    limits = _fetch_codex_rate_limits()
    now = datetime.now().astimezone()

    def window(block: dict | None) -> tuple[float | None, datetime | None]:
        if block is None:
            return None, None
        if not isinstance(block, dict):
            raise UsageError("Codex returned an invalid quota window.")
        try:
            reset = datetime.fromtimestamp(block["resetsAt"]).astimezone() if block.get("resetsAt") else None
            return float(block["usedPercent"]), reset
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as e:
            raise UsageError("Codex returned an invalid quota window.") from e

    current_pct, current_reset = window(limits.get("primary"))
    weekly_pct, weekly_reset = window(limits.get("secondary"))
    usage = {
        "title": "Codex",
        "current_pct": current_pct, "current_reset": current_reset,
        "weekly_pct": weekly_pct, "weekly_reset": weekly_reset,
        "now": now,
    }
    # Codex says how long each window really is (300 and 10080 minutes at the time of writing).
    lengths = {w: limits[key]["windowDurationMins"] for w, key in (("current", "primary"), ("weekly", "secondary"))
               if isinstance(limits.get(key), dict) and isinstance(limits[key].get("windowDurationMins"), (int, float))}
    if lengths:
        usage["window_min"] = lengths
    usage["codex_extra"] = _codex_extra(limits)
    return pace.annotate(usage)


def _codex_extra(limits: dict) -> dict:
    """The free rate-limit resets Codex has granted you, for its own screen: how many are left and in how many days
    the next one expires. Every field is optional in Codex's reply."""
    resets = limits.get("_reset_credits") if isinstance(limits.get("_reset_credits"), dict) else {}
    available = [c for c in resets.get("credits", []) if isinstance(c, dict) and c.get("status") == "available"]
    expiries = [c["expiresAt"] for c in available if isinstance(c.get("expiresAt"), (int, float))]
    return {
        "free_resets": resets.get("availableCount", len(available)) or 0,
        "next_reset_expires_days": max(0, round((min(expiries) - time.time()) / 86400)) if expiries else None,
    }


def _resets_chip(usage: dict) -> tuple[str, str] | None:
    """(text, colour) for the free-resets note in the corner of Codex's screen: "3 resets · 2d" (days until the
    next one expires; yellow from 3 days, red from 1 so it gets used in time)."""
    extra = usage.get("codex_extra") or {}
    count = extra.get("free_resets")
    if usage.get("title") != "Codex" or not count:
        return None
    days = extra.get("next_reset_expires_days")
    text = f"{count} reset{'' if count == 1 else 's'}" + (f" · {days}d" if days is not None else "")
    color = STATE_COLORS["crit"] if days is not None and days <= 1 else STATE_COLORS["warn"] if days is not None and days <= 3 else THEMES["Codex"]["weekly"]
    return text, color


PROVIDERS = {"claude": fetch_usage, "codex": fetch_codex_usage}


def _parse(text: str) -> dict:
    now = datetime.now().astimezone()
    session = _SESSION_RE.search(text)
    week = _WEEK_RE.search(text)
    if not session and not week:
        raise UsageError("Could not find usage lines in /usage output:\n" + text)
    return {
        "title": "Claude",
        "current_pct": float(session.group(1)) if session else None,
        "current_reset": _parse_reset(session.group(2), now) if session and session.group(2) else None,
        "weekly_pct": float(week.group(1)) if week else None,
        "weekly_reset": _parse_reset(week.group(2), now) if week and week.group(2) else None,
        "now": now,
    }


def _parse_reset(text: str, now: datetime) -> datetime | None:
    m = _RESET_RE.search(text)
    if not m:
        return None
    tz = ZoneInfo(m.group("tz"))
    month = datetime.strptime(m.group("month"), "%b").month
    hour = int(m.group("hour")) % 12
    if m.group("meridiem").lower() == "pm":
        hour += 12
    minute = int(m.group("minute") or 0)
    local_now = now.astimezone(tz)
    candidate = None
    for year in (local_now.year, local_now.year + 1):
        candidate = datetime(year, month, int(m.group("day")), hour, minute, tzinfo=tz)
        if candidate >= local_now:
            return candidate
    return candidate


def _clock(moment: datetime) -> str:
    """12-hour clock time, "2:57 PM". Built by hand: strftime's %p follows the system locale ("p. m." in Spanish)."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _format_delta(reset_at: datetime | None, now: datetime) -> str:
    if reset_at is None:
        return "Resets in --"
    delta = reset_at - now
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    if days >= 1:
        return f"Resets in {days}d"
    return f"Resets in {hours}h {minutes}m"


# Pixel-art mascot bitmap: '1' = orange body, '2' = black eye, '0' = background.
# 14 columns x 8 rows — squat and chubby: square torso, two stubby side
# arms, and four thin legs, without the extra height of a taller torso.
_MASCOT_BITMAP = (
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
_CODEX_BITMAP = (
    "00000000000000",
    "00000011110000",
    "00011011111100",
    "00111111111110",
    "01111111111111",
    "01111111111111",
    "01111111111111",
    "00111111111110",
)
_INK = "#0F0D0B"

# Per-provider look, picked by usage["title"]: mascot bitmap + body colour, and the two bar accents.
THEMES = {
    "Claude": {"bitmap": _MASCOT_BITMAP, "body": ACCENT, "current": CURRENT_ACCENT, "weekly": WEEKLY_ACCENT},
    "Codex": {"bitmap": _CODEX_BITMAP, "body": "#4F9DFF", "current": "#4F9DFF", "weekly": "#B79CFF"},
}


def _theme(usage: dict) -> dict:
    return THEMES.get(usage.get("title"), THEMES["Claude"])


def _draw_mascot(image: Image.Image, top_left: tuple[int, int], cell_size: int, theme: dict | None = None) -> None:
    """Blocky pixel-art mascot, drawn cell by cell so it stays crisp at small sizes."""
    theme = theme or THEMES["Claude"]
    x0, y0 = top_left
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(theme["bitmap"]):
        for col, cell in enumerate(line):
            if cell == "0":
                continue
            fill = theme["body"] if cell == "1" else _INK
            x = x0 + col * cell_size
            y = y0 + row * cell_size
            draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=fill)


# A tiny pixel-art laptop the mascot pulls out mid-loop: 'B' = bezel/base,
# 'G' = the glowing screen, '.' = background.
_LAPTOP_BITMAP = (
    "BBBBBBBB",
    "BGGGGGGB",
    "BGGGGGGB",
    "BBBBBBBB",
    ".BBBBBB.",
    "BBBBBBBB",
)
_LAPTOP_BEZEL = "#4A3F4D"
_LAPTOP_GLOW = "#FFE9C7"


def _draw_laptop(image: Image.Image, top_left: tuple[int, int], cell_size: int) -> None:
    x0, y0 = top_left
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(_LAPTOP_BITMAP):
        for col, cell in enumerate(line):
            if cell == ".":
                continue
            fill = _LAPTOP_BEZEL if cell == "B" else _LAPTOP_GLOW
            x = x0 + col * cell_size
            y = y0 + row * cell_size
            draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=fill)


def _laptop_geometry(mascot_w: int, cell_size: int = 3) -> tuple[int, int, int]:
    """(x, hidden_y, held_y) shared by the laptop and the typing-cursor overlay."""
    laptop_w = cell_size * len(_LAPTOP_BITMAP[0])
    x = 10 + (mascot_w - laptop_w) // 2
    return x, 34, 21


# A tiny pixel-art mug the mascot pulls out mid-loop: 'B' = ceramic, 'C' = coffee.
_MUG_BITMAP = (
    "BBBBBB",
    "BCCCCB",
    "BCCCCB",
    "BCCCCB",
    "BBBBBB",
)
_MUG_CERAMIC = "#F5F4EF"
_MUG_COFFEE = "#5A3A22"
_STEAM_COLOR = "#C7C2BC"


def _draw_mug(image: Image.Image, top_left: tuple[int, int], cell_size: int, steam_phase: int) -> None:
    x0, y0 = top_left
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(_MUG_BITMAP):
        for col, cell in enumerate(line):
            fill = _MUG_CERAMIC if cell == "B" else _MUG_COFFEE
            x = x0 + col * cell_size
            y = y0 + row * cell_size
            draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=fill)

    # Two wisps of steam, each drifting up and fading over a 3-step cycle.
    for wisp, col in enumerate((1, 4)):
        phase = (steam_phase + wisp * 2) % 6
        if phase >= 4:
            continue
        x = x0 + col * cell_size
        y = y0 - (phase + 1) * cell_size
        draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=_STEAM_COLOR)


# A tiny "idea" spark for the "eureka" moment — a diamond burst, simplest
# shape that still reads clearly at a few pixels across.
_BULB_BITMAP = (
    "..G..",
    ".GGG.",
    "GGGGG",
    ".GGG.",
    "..G..",
)
_BULB_GLOW_ON = "#FFD966"
_BULB_GLOW_OFF = "#6B5E3E"


def _draw_bulb(image: Image.Image, top_left: tuple[int, int], cell_size: int, lit: bool) -> None:
    x0, y0 = top_left
    draw = ImageDraw.Draw(image)
    glow = _BULB_GLOW_ON if lit else _BULB_GLOW_OFF
    for row, line in enumerate(_BULB_BITMAP):
        for col, cell in enumerate(line):
            if cell == ".":
                continue
            fill = glow
            x = x0 + col * cell_size
            y = y0 + row * cell_size
            draw.rectangle((x, y, x + cell_size - 1, y + cell_size - 1), fill=fill)


_MASCOT_TOP = 10
_IDLE_BOB = (0, -1, -2, -1, 0, 1, 2, 1)  # a gentle idle bob, one loop


def _combine(*layers):
    """Chain several per-frame drawing hooks into one."""
    active = [layer for layer in layers if layer is not None]

    def draw(image: Image.Image, mascot_w: int) -> None:
        for layer in active:
            layer(image, mascot_w)
    return draw


def _laptop_layer(progress: float):
    """The laptop rising to `progress` (0..1) in front of the mascot."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        if progress <= 0:
            return
        x, hidden_y, held_y = _laptop_geometry(mascot_w)
        y = round(hidden_y - (hidden_y - held_y) * progress)
        _draw_laptop(image, (x, y), 3)
    return draw


def _typing_layer(cursor_col: int):
    """A single blinking 'cursor' block sweeping across the held laptop's screen."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        x, _hidden_y, held_y = _laptop_geometry(mascot_w)
        col = 1 + (cursor_col % 6)
        cx = x + col * 3
        cy = held_y + 1 * 3
        ImageDraw.Draw(image).rectangle((cx, cy, cx + 2, cy + 5), fill=_LAPTOP_BEZEL)
    return draw


def _mug_layer(progress: float, steam_phase: int = 0):
    """The mug rising to `progress` (0..1) beside the mascot, with animated steam once held."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        if progress <= 0:
            return
        mug_cell = 2
        mug_w = mug_cell * len(_MUG_BITMAP[0])
        x = 10 + mascot_w - mug_w + 2
        hidden_y, held_y = 34, 15
        y = round(hidden_y - (hidden_y - held_y) * progress)
        _draw_mug(image, (x, y), mug_cell, steam_phase if progress >= 1 else 6)
    return draw


def _bulb_layer(progress: float, lit: bool):
    """The idea spark popping in beside the mascot's head, in the gap before the title."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        if progress <= 0:
            return
        bulb_cell = 2
        bulb_w = bulb_cell * len(_BULB_BITMAP[0])
        x = 10 + mascot_w - 8
        held_y, hidden_y = 2, 14
        y = round(hidden_y - (hidden_y - held_y) * progress)
        _draw_bulb(image, (x, y), bulb_cell, lit)
    return draw


# Named animations, kept side by side so picking one never deletes another.
# Each entry is a tuple of (mascot dx, mascot dy, extra-drawing hook or None).
_IDLE_FRAMES = tuple((0, dy, None) for dy in _IDLE_BOB)

ANIMATIONS: dict[str, tuple[tuple[int, int, object], ...]] = {
    "idle": _IDLE_FRAMES,
    "laptop": (
        *_IDLE_FRAMES,
        (0, 0, _laptop_layer(0.35)), (0, 0, _laptop_layer(0.7)), (0, 0, _laptop_layer(1.0)),
        (0, 1, _laptop_layer(1.0)), (0, 2, _laptop_layer(1.0)), (0, 1, _laptop_layer(1.0)), (0, 0, _laptop_layer(1.0)),
        (0, 0, _laptop_layer(0.5)),
    ),
    "coffee": (
        *_IDLE_FRAMES,
        (0, 0, _mug_layer(0.4)), (0, 0, _mug_layer(0.7)), (0, 0, _mug_layer(1.0, 0)),
        (0, 0, _mug_layer(1.0, 1)), (0, 0, _mug_layer(1.0, 2)), (0, 0, _mug_layer(1.0, 3)),
        (0, 0, _mug_layer(1.0, 4)), (0, 0, _mug_layer(1.0, 5)), (0, 0, _mug_layer(1.0, 0)),
        (0, 0, _mug_layer(0.5)),
    ),
    "eureka": (
        *_IDLE_FRAMES,
        (0, 0, _bulb_layer(0.5, True)), (0, 0, _bulb_layer(1.0, True)),
        (0, 0, _bulb_layer(1.0, False)), (0, 0, _bulb_layer(1.0, True)),
        (0, 0, _bulb_layer(1.0, False)), (0, 0, _bulb_layer(1.0, True)),
        (0, 0, _bulb_layer(0.5, True)),
    ),
    "dance": (
        (0, 0, None), (2, -2, None), (3, 0, None), (2, 2, None),
        (0, 0, None), (-2, -2, None), (-3, 0, None), (-2, 2, None),
    ),
    "typing": (
        *_IDLE_FRAMES,
        (0, 0, _laptop_layer(0.35)), (0, 0, _laptop_layer(0.7)),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(0))),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(1))),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(2))),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(3))),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(4))),
        (0, 0, _combine(_laptop_layer(1.0), _typing_layer(5))),
        (0, 0, _laptop_layer(1.0)),
        (0, 0, _laptop_layer(0.5)),
    ),
}
DEFAULT_ANIMATION = "laptop"
# Each provider rotates through its own set; "auto"/"random" (the default) pick one, never the same twice in a row.
ANIMATION_GROUPS = {
    "Claude": ("idle", "laptop", "coffee", "eureka", "dance", "typing"),
    "Codex": ("prompt", "think", "sparkle", "bolt", "code", "hop"),
}


# --- Codex animations: all drawn on the 14x8 cloud grid (3 px cells), see _CODEX_BITMAP -----------------

def _cell(image: Image.Image, col: int, row: int, color: str = _INK, dy: int = 0) -> None:
    """One 3x3 px cell of the mascot grid; `dy` follows a bobbing/hopping cloud."""
    x, y = 10 + col * 3, _MASCOT_TOP + row * 3 + dy
    ImageDraw.Draw(image).rectangle((x, y, x + 2, y + 2), fill=color)


_CHEVRON = ((3, 4), (4, 5), (3, 6))  # the `>` of `>_`
_BOLT = ((8, 3), (7, 4), (8, 4), (6, 5), (7, 5), (7, 6), (8, 6), (7, 7), (6, 8))
_BRACKET_L = ((2, 4), (3, 4), (2, 5), (2, 6), (3, 6))
_BRACKET_R = ((11, 4), (10, 4), (11, 5), (11, 6), (10, 6))
_SPARK_COLOR = "#CFE3FF"
_SPARK_SPOTS = ((14, 3), (32, 2), (46, 5), (55, 15), (56, 27))  # top-left px of each sparkle, around the cloud
_FLASH_BODY = "#A9CFFF"
_BOLT_COLOR = "#FFD966"
_SHADOW_COLOR = "#3A352C"


def _prompt_layer(chevron: bool = True, dots: int = 0, cursor: int | None = None, dy: int = 0):
    """Codex's `>_` prompt on the cloud: a chevron, then `dots` typed dots, then a cursor block at column `cursor`."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        if chevron:
            for col, row in _CHEVRON:
                _cell(image, col, row, dy=dy)
        for i in range(dots):
            _cell(image, 7 + i * 2, 6, dy=dy)  # dots sit on the baseline, like periods
        if cursor is not None:
            for col in (cursor, cursor + 1):
                _cell(image, col, 6, dy=dy)
    return draw


def _think_layer(dots: int, dy: int):
    """`dots` dots in the middle of a bobbing cloud: it's thinking."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        for i in range(dots):
            _cell(image, 5 + 2 * i, 5, dy=dy)
    return draw


def _sparkle_layer(phase: int):
    """A resting `>_` with sparkles twinkling around the cloud (each one cycles off, dot, plus, dot)."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        for col, row in (*_CHEVRON, (7, 6), (8, 6)):
            _cell(image, col, row)
        d = ImageDraw.Draw(image)
        for i, (x, y) in enumerate(_SPARK_SPOTS):
            stage = (phase + i) % 4
            if stage == 0:
                continue
            d.rectangle((x + 2, y + 2, x + 3, y + 3), fill=_SPARK_COLOR)  # centre dot
            if stage == 2:  # plus-shaped arms
                for ax, ay in ((x + 2, y), (x + 2, y + 4), (x, y + 2), (x + 4, y + 2)):
                    d.rectangle((ax, ay, ax + 1, ay + 1), fill=_SPARK_COLOR)
    return draw


def _bolt_layer(flash: bool):
    """Calm `>_`, or a lightning flash: the cloud lights up and a bolt strikes out of it."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        if not flash:
            for col, row in (*_CHEVRON, (7, 6), (8, 6)):
                _cell(image, col, row)
            return
        for row, line in enumerate(_CODEX_BITMAP):
            for col, ch in enumerate(line):
                if ch == "1":
                    _cell(image, col, row, _FLASH_BODY)
        for col, row in _BOLT:
            _cell(image, col, row, _BOLT_COLOR)
    return draw


def _code_layer(left: bool = False, right: bool = False, dots: int = 0):
    """`[ ... ]` brackets appearing on the cloud, with `dots` typed inside."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        for col, row in (_BRACKET_L if left else ()):
            _cell(image, col, row)
        for col, row in (_BRACKET_R if right else ()):
            _cell(image, col, row)
        for i in range(dots):
            _cell(image, 4 + 2 * i, 5)
    return draw


def _hop_layer(dy: int, lift: int):
    """The cloud hopping: `>_` rides along, and the shadow on the ground shrinks as it rises (`lift` px)."""
    def draw(image: Image.Image, mascot_w: int) -> None:
        half = max(8, 17 - lift)
        ImageDraw.Draw(image).rectangle((31 - half, 36, 31 + half, 37), fill=_SHADOW_COLOR)
        for col, row in (*_CHEVRON, (7, 6), (8, 6)):
            _cell(image, col, row, dy=dy)
    return draw


_HOP_DY = (0, 0, -3, -6, -8, -8, -6, -3, 0, 2, 0)

ANIMATIONS["prompt"] = (  # the prompt appears, the cursor blinks, then it types three dots
    (0, 0, None), (0, 0, None),
    (0, 0, _prompt_layer()),
    (0, 0, _prompt_layer(cursor=7)), (0, 0, _prompt_layer(cursor=7)),
    (0, 0, _prompt_layer()), (0, 0, _prompt_layer()),
    (0, 0, _prompt_layer(cursor=7)), (0, 0, _prompt_layer(cursor=7)),
    (0, 0, _prompt_layer(dots=1, cursor=9)), (0, 0, _prompt_layer(dots=1, cursor=9)),
    (0, 0, _prompt_layer(dots=2, cursor=11)), (0, 0, _prompt_layer(dots=2, cursor=11)),
    (0, 0, _prompt_layer(dots=3)), (0, 0, _prompt_layer(dots=3)), (0, 0, _prompt_layer(dots=3)),
    (0, 0, _prompt_layer()), (0, 0, _prompt_layer()),
)
ANIMATIONS["think"] = tuple(  # bobbing cloud (one bob cycle = smaller GIF), dots 0..3 cycling
    (0, _IDLE_BOB[i], _think_layer((i // 2) % 4, _IDLE_BOB[i])) for i in range(8)
)
ANIMATIONS["sparkle"] = tuple((0, 0, _sparkle_layer(i)) for i in range(8))
ANIMATIONS["bolt"] = (
    *((0, 0, _bolt_layer(False)),) * 5,
    (0, 0, _bolt_layer(True)), (0, 0, _bolt_layer(True)),
    (0, 0, _bolt_layer(False)),
    (0, 0, _bolt_layer(True)),
    *((0, 0, _bolt_layer(False)),) * 4,
)
ANIMATIONS["code"] = (  # [ ] brackets appear, three dots get typed inside
    (0, 0, None), (0, 0, None),
    (0, 0, _code_layer(left=True)), (0, 0, _code_layer(left=True)),
    (0, 0, _code_layer(left=True, right=True)), (0, 0, _code_layer(left=True, right=True)),
    (0, 0, _code_layer(True, True, 1)), (0, 0, _code_layer(True, True, 1)),
    (0, 0, _code_layer(True, True, 2)), (0, 0, _code_layer(True, True, 2)),
    (0, 0, _code_layer(True, True, 3)), (0, 0, _code_layer(True, True, 3)), (0, 0, _code_layer(True, True, 3)),
    (0, 0, _code_layer(True, True)), (0, 0, _code_layer(True, True)),
)
ANIMATIONS["hop"] = tuple((0, dy, _hop_layer(dy, -dy)) for dy in _HOP_DY)

RECENT_ANIMATIONS = 3  # an animation can't come back until this many different ones have played
_recent_animations: dict[str, list[str]] = {}  # per provider title, oldest first


def _pick_animation(usage: dict) -> str:
    """A random animation from the provider's own set, none of the last RECENT_ANIMATIONS shown.

    The history only grows once an upload succeeds (see push_usage), so a cancelled or failed upload
    can't make the next pick repeat something that is actually on screen.
    """
    import random
    title = usage.get("title", "Claude")
    group = ANIMATION_GROUPS.get(title, ANIMATION_GROUPS["Claude"])
    keep_out = min(RECENT_ANIMATIONS, len(group) - 1)  # always leave at least one candidate
    recent = _recent_animations.get(title, [])[-keep_out:] if keep_out else []
    return random.choice([a for a in group if a not in recent])


def _render_frame(usage: dict, mascot_dx: int = 0, mascot_dy: int = 0, extra=None) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    title_font = ImageFont.load_default(size=26)
    pill_font = ImageFont.load_default(size=14)
    pct_font = ImageFont.load_default(size=30)
    reset_font = ImageFont.load_default(size=14)
    footer_font = ImageFont.load_default(size=13)

    theme = _theme(usage)
    mascot_cell = 3
    mascot_w = mascot_cell * len(theme["bitmap"][0])
    _draw_mascot(image, (10 + mascot_dx, _MASCOT_TOP + mascot_dy), mascot_cell, theme)

    if extra is not None:
        extra(image, mascot_w)

    draw.text((10 + mascot_w + 10, 8), usage.get("title", "Usage"), font=title_font, fill=TEXT)
    chip = _resets_chip(usage)  # Codex only: the free rate-limit resets you still have, top right
    if chip:
        text, color = chip
        draw.text((230 - draw.textlength(text, font=footer_font), 15), text, font=footer_font, fill=color)

    _draw_section(
        draw, top=40, label="Session", percent=usage["current_pct"],
        reset_text=_format_delta(usage["current_reset"], usage["now"]),
        accent=theme["current"],
        pill_font=pill_font, pct_font=pct_font, reset_font=reset_font,
        pace_info=_pace_info(usage, "current"), pace_font=footer_font,
    )
    _draw_section(
        draw, top=134, label="Weekly", percent=usage["weekly_pct"],
        reset_text=_format_delta(usage["weekly_reset"], usage["now"]),
        accent=theme["weekly"],
        pill_font=pill_font, pct_font=pct_font, reset_font=reset_font,
        pace_info=_pace_info(usage, "weekly"), pace_font=footer_font,
    )

    if usage.get("stale"):
        # Last known numbers that couldn't be refreshed: dim everything and say since when.
        image = Image.blend(image, Image.new("RGB", image.size, BG), 0.55)
        draw = ImageDraw.Draw(image)
        footer_text, footer_color = f"! No fresh data since {_clock(usage['now'])}", STATE_COLORS["warn"]
    else:
        footer_text, footer_color = f"* Updated {_clock(usage['now'])}", theme["current"]
    bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
    draw.text((((240 - (bbox[2] - bbox[0])) // 2), 223), footer_text, font=footer_font, fill=footer_color)

    return image


def render(usage: dict) -> bytes:
    """Static JPEG (single frame, mascot at rest)."""
    buf = BytesIO()
    _render_frame(usage).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def render_animation(usage: dict, animation: str = DEFAULT_ANIMATION) -> bytes:
    """Looping GIF for one of the named ANIMATIONS presets."""
    frames_spec = ANIMATIONS[animation]
    return _encode_gif([_render_frame(usage, dx, dy, extra) for dx, dy, extra in frames_spec])


def _encode_gif(frames: list[Image.Image]) -> bytes:
    # One palette built from *all* frames, so colours that only appear later (a bolt, sparkles, steam) survive.
    sheet = Image.new("RGB", (WIDTH, HEIGHT * len(frames)))
    for i, frame in enumerate(frames):
        sheet.paste(frame, (0, i * HEIGHT))
    base = sheet.quantize(colors=96)
    quantized = [f.quantize(palette=base) for f in frames]
    buf = BytesIO()
    quantized[0].save(
        buf, format="GIF", save_all=True, append_images=quantized[1:],
        duration=130, loop=0, disposal=2, optimize=False,
    )
    return buf.getvalue()


# --- Split view: both providers on one screen ---------------------------------------------------------

SPLIT_PANEL_H = 118  # two panels of this height, a 4 px divider between them


def _draw_split_row(draw, *, top, label, percent, reset_text, accent, fonts, pace_info=None) -> None:
    """One compact usage row: big number + bar on the first line, label + reset countdown under it."""
    state = alerts.bar_state(percent)
    color = STATE_COLORS.get(state, accent)
    pct_text = f"{percent:.0f}%" if percent is not None else "--%"
    draw.text((10, top - 1), pct_text, font=fonts["pct"], fill=STATE_COLORS.get(state, TEXT))
    bar_left, bar_right, bar_top, bar_h = 68, 230, top + 4, 12
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_top + bar_h), radius=6, fill=PILL_BG)
    if percent:
        fill_w = round((bar_right - bar_left) * min(percent, 100) / 100)
        if fill_w > 0:
            draw.rounded_rectangle(
                (bar_left, bar_top, bar_left + fill_w, bar_top + bar_h), radius=min(6, max(2, fill_w // 2)), fill=color,
            )
    draw.text((10, top + 26), label, font=fonts["small"], fill=MUTED)
    draw.text((bar_left, top + 26), reset_text, font=fonts["small"], fill=MUTED)
    _draw_pace(draw, pace_info, right=230, y=top + 26, left_end=bar_left + draw.textlength(reset_text, font=fonts["small"]), font=fonts["small"])


SPLIT_ART = (64, 40)  # the mascot-and-props corner of a split panel: the same art, at the same scale, as the single screens


def _header_art(usage: dict, frame: tuple) -> Image.Image:
    """The top-left corner of a single-provider screen for one animation frame (mascot at full size plus whatever it
    holds: laptop, mug, bolt, sparkles...). The animations draw at absolute coordinates, so draw the real thing on a
    scratch screen and cut the corner out."""
    scratch = Image.new("RGB", (WIDTH, HEIGHT), BG)
    dx, dy, extra = frame
    theme = _theme(usage)
    _draw_mascot(scratch, (10 + dx, _MASCOT_TOP + dy), 3, theme)
    if extra is not None:
        extra(scratch, 3 * len(theme["bitmap"][0]))
    return scratch.crop((0, 0, *SPLIT_ART))


def _draw_split_panel(image: Image.Image, top: int, usage: dict, frame: tuple, fonts: dict) -> None:
    """One provider's panel (animated header + two rows) at vertical offset `top`; dimmed and dated if stale."""
    theme = _theme(usage)
    draw = ImageDraw.Draw(image)
    image.paste(_header_art(usage, (0, 0, None) if usage.get("stale") else frame), (0, top))
    draw.text((SPLIT_ART[0] + 2, top + 8), usage.get("title", "Usage"), font=fonts["title"], fill=TEXT)
    updated = _clock(usage["now"])
    if not usage.get("stale"):
        width = draw.textlength(updated, font=fonts["small"])
        draw.text((230 - width, top + 12), updated, font=fonts["small"], fill=theme["current"])
    for row_top, label, window, accent in (
        (top + 40, "Session", "current", theme["current"]),
        (top + 77, "Weekly", "weekly", theme["weekly"]),
    ):
        _draw_split_row(
            draw, top=row_top, label=label, percent=usage[f"{window}_pct"],
            reset_text=_format_delta(usage[f"{window}_reset"], usage["now"]), accent=accent, fonts=fonts,
            pace_info=_pace_info(usage, window),
        )
    if usage.get("stale"):
        box = (0, top, WIDTH, top + SPLIT_PANEL_H)
        image.paste(Image.blend(image.crop(box), Image.new("RGB", (WIDTH, SPLIT_PANEL_H), BG), 0.55), box)
        text = f"! stale since {updated}"
        width = ImageDraw.Draw(image).textlength(text, font=fonts["small"])
        ImageDraw.Draw(image).text((230 - width, top + 12), text, font=fonts["small"], fill=STATE_COLORS["warn"])


def _render_split_frame(panels: list[dict], animations: list[str] | None = None, index: int = 0) -> Image.Image:
    """Frame `index` of the split view. Each panel plays its own animation (default: just the idle bob); a shorter
    one simply loops again while a longer one is still going."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    fonts = {
        "title": ImageFont.load_default(size=18), "pct": ImageFont.load_default(size=22),
        "small": ImageFont.load_default(size=11),
    }
    animations = animations or []
    for i, usage in enumerate(panels[:2]):
        spec = ANIMATIONS[animations[i] if i < len(animations) else "idle"]
        _draw_split_panel(image, i * (SPLIT_PANEL_H + 4), usage, spec[index % len(spec)], fonts)
    ImageDraw.Draw(image).line((10, SPLIT_PANEL_H + 1, 230, SPLIT_PANEL_H + 1), fill=PILL_BG, width=2)  # divider
    return image


def render_split(panels: list[dict], animations: list[str] | None = None) -> bytes:
    """Looping GIF with two providers' usage stacked on one screen, each mascot doing its own animation."""
    animations = animations or []
    length = max((len(ANIMATIONS[animations[i] if i < len(animations) else "idle"]) for i in range(len(panels[:2]))),
                 default=len(_IDLE_BOB))
    return _encode_gif([_render_split_frame(panels, animations, index) for index in range(length)])


def _animation_for(usage: dict, requested: str) -> str:
    """The animation a provider's panel should play: the one asked for if it's one of that provider's, else a
    random one from its own set (never one of its last three)."""
    group = ANIMATION_GROUPS.get(usage.get("title", "Claude"), ANIMATION_GROUPS["Claude"])
    return requested if requested in group else _pick_animation(usage)


def push_split(
    ip: str, panels: list[dict], filename: str, show: bool = True, cancel: threading.Event | None = None,
    animation: str = "auto",
) -> None:
    """Render and upload the split view as `filename`. Like the single screens, "auto"/"random" gives each provider
    a fresh animation of its own on every push, remembered once the upload has gone through."""
    names = [_animation_for(usage, animation) for usage in panels[:2]]
    upload(ip, render_split(panels, names), filename, "image/gif", show, cancel)
    if animation in ("auto", "random"):
        for usage, name in zip(panels[:2], names):
            history = _recent_animations.setdefault(usage.get("title", "Claude"), [])
            history.append(name)
            del history[:-RECENT_ANIMATIONS]


# --- Stats view: activity numbers for both providers ------------------------------------------------------

def _fit(draw, text: str, font, width: float) -> str:
    """`text` shortened with "..." to fit `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _draw_week_chart(draw, days: list[dict], *, left: int, right: int, baseline: int, height: int, color: str, fonts: dict) -> None:
    """Requests per day for the last week: one bar per day (today's in full colour, earlier days dimmer), the number
    above each bar and the weekday under it. Scaled to the provider's own busiest day, so the shapes compare."""
    slot = (right - left) / len(days)
    top_value = max((d["requests"] for d in days), default=0)
    dim = _blend(color, BG, 0.45)
    for i, day in enumerate(days):
        n = day["requests"]
        x = left + i * slot
        bar_w = min(20, slot - 6)
        x0 = round(x + (slot - bar_w) / 2)
        h = max(2, round(height * n / top_value)) if top_value and n else 2
        today = i == len(days) - 1
        draw.rectangle((x0, baseline - h, x0 + round(bar_w) - 1, baseline - 1), fill=color if today and n else (dim if n else PILL_BG))
        label = f"{n:,}" if n < 10000 else f"{n // 1000}k"
        w = draw.textlength(label, font=fonts["tiny"])
        draw.text((x0 + bar_w / 2 - w / 2, baseline - h - 11), label, font=fonts["tiny"], fill=TEXT if today else MUTED)
        name = usage_stats.weekday(day["date"])
        w = draw.textlength(name, font=fonts["tiny"])
        draw.text((x0 + bar_w / 2 - w / 2, baseline + 1), name, font=fonts["tiny"], fill=TEXT if today else MUTED)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _blend(color: str, over: str, amount: float) -> tuple[int, int, int]:
    """`color` mixed with `over` (0 = all `over`, 1 = all `color`)."""
    a, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (color, over))
    return tuple(round(b[k] + (a[k] - b[k]) * amount) for k in range(3))


def _draw_stats_panel(image: Image.Image, top: int, usage: dict, fonts: dict) -> None:
    """One provider's activity: requests and sessions for 24 h and 7 days, and requests per day over the last week.
    The very same layout and numbers for every provider, so they can be compared at a glance."""
    theme = _theme(usage)
    draw = ImageDraw.Draw(image)
    _draw_mascot(image, (10, top + 8), 2, theme)
    draw.text((46, top + 5), usage.get("title", "Usage"), font=fonts["title"], fill=TEXT)
    updated = _clock(usage["now"])
    if not usage.get("stale"):
        width = draw.textlength(updated, font=fonts["small"])
        draw.text((230 - width, top + 10), updated, font=fonts["small"], fill=theme["current"])
    activity = usage.get("activity")
    if not activity:
        draw.text((10, top + 40), "No local activity logs found" if activity == {} else "Counting...",
                  font=fonts["body"], fill=MUTED)
    else:
        for i, label in enumerate(("24h", "7d")):
            draw.text((10, top + 28 + i * 15), usage_stats.counts_text(label, activity[label]), font=fonts["body"], fill=TEXT)
        _draw_week_chart(draw, activity["days"], left=10, right=230, baseline=top + 106, height=34,
                         color=theme["body"], fonts=fonts)
    if usage.get("stale"):
        box = (0, top, WIDTH, top + SPLIT_PANEL_H)
        image.paste(Image.blend(image.crop(box), Image.new("RGB", (WIDTH, SPLIT_PANEL_H), BG), 0.55), box)
        text = f"! stale since {updated}"
        width = ImageDraw.Draw(image).textlength(text, font=fonts["small"])
        ImageDraw.Draw(image).text((230 - width, top + 10), text, font=fonts["small"], fill=STATE_COLORS["warn"])


def _render_stats_frame(panels: list[dict]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    fonts = {"title": ImageFont.load_default(size=17), "body": ImageFont.load_default(size=12),
             "small": ImageFont.load_default(size=11), "tiny": ImageFont.load_default(size=10)}
    for i, usage in enumerate(panels[:2]):
        _draw_stats_panel(image, i * (SPLIT_PANEL_H + 4), usage, fonts)
    ImageDraw.Draw(image).line((10, SPLIT_PANEL_H + 1, 230, SPLIT_PANEL_H + 1), fill=PILL_BG, width=2)
    return image


def render_stats(panels: list[dict]) -> bytes:
    """A still GIF (so a small upload) with both providers' activity stats."""
    return _encode_gif([_render_stats_frame(panels)])


def push_stats(
    ip: str, panels: list[dict], filename: str, show: bool = True, cancel: threading.Event | None = None,
) -> None:
    upload(ip, render_stats(panels), filename, "image/gif", show, cancel)




def _draw_section(
    draw, *, top, label, percent, reset_text, accent, pill_font, pct_font, reset_font, pace_info=None, pace_font=None,
) -> None:
    panel_left, panel_right = 10, 230
    pad = 4
    pct_text = f"{percent:.0f}%" if percent is not None else "--%"
    state = alerts.bar_state(percent)  # past the warn/crit thresholds the number and bar change colour
    accent = STATE_COLORS.get(state, accent)
    draw.text((panel_left + pad, top + 6), pct_text, font=pct_font, fill=STATE_COLORS.get(state, TEXT))

    pill_bbox = draw.textbbox((0, 0), label, font=pill_font)
    pill_text_w = pill_bbox[2] - pill_bbox[0]
    pill_h = 26
    pill_w = pill_text_w + 28
    pill_left = panel_right - pad - pill_w
    pill_top = top + 10
    draw.rounded_rectangle((pill_left, pill_top, pill_left + pill_w, pill_top + pill_h), radius=pill_h // 2, fill=PILL_BG)
    draw.text((pill_left + (pill_w - pill_text_w) // 2, pill_top + (pill_h - pill_bbox[3]) // 2), label, font=pill_font, fill=TEXT)

    bar_left, bar_top, bar_right, bar_h = panel_left + pad, top + 44, panel_right - pad, 18
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_top + bar_h), radius=9, fill=PILL_BG)
    if percent:
        fill_w = round((bar_right - bar_left) * min(percent, 100) / 100)
        if fill_w > 0:
            draw.rounded_rectangle(
                (bar_left, bar_top, bar_left + fill_w, bar_top + bar_h),
                radius=min(9, max(3, fill_w // 2)), fill=accent,
            )

    draw.text((panel_left + pad, top + 68), reset_text, font=reset_font, fill=MUTED)
    _draw_pace(draw, pace_info, right=panel_right - pad, y=top + 70, left_end=panel_left + pad + draw.textlength(reset_text, font=reset_font), font=pace_font)


def _draw_pace(draw, pace_info, *, right: int, y: int, left_end: float, font) -> None:
    """The projection ("Full in 1h 30m" / "~62% at reset"), right-aligned; skipped if it wouldn't fit next to the
    reset countdown, which matters more."""
    if not pace_info:
        return
    text, state = pace_info
    width = draw.textlength(text, font=font)
    if left_end + 10 < right - width:
        draw.text((right - width, y), text, font=font, fill=STATE_COLORS.get(state, MUTED))


def _pace_info(usage: dict, window: str):
    """(text, state) projection for a usage window, or None (also None while the numbers are stale)."""
    if usage.get("stale"):
        return None
    return pace.describe((usage.get("pace") or {}).get(window))


CONNECT_TIMEOUT = 3  # seconds to reach the device / get a reply to a small request; a switched-off one fails fast
UPLOAD_TIMEOUT = 15  # seconds to let the device finish storing an uploaded GIF (~3 s in practice)


class UploadCancelled(Exception):
    pass


def _device_errors(func):
    """The device sometimes cuts a response short or answers garbage (http.client raises HTTPException,
    which isn't an OSError). Report those like any other "device didn't answer properly" failure."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except http.client.HTTPException as e:
            raise OSError(f"incomplete or invalid response from the device ({type(e).__name__})") from e
    return wrapper


def _abort_on_cancel(cancel: threading.Event, done: threading.Event, sock) -> None:
    """Watcher: when `cancel` fires mid-upload, reset the connection so the device stops receiving at once."""
    while not done.is_set():
        if cancel.wait(0.05):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST, drop buffered data
                sock.shutdown(socket.SHUT_RDWR)  # close() alone is deferred while the response reader holds the socket
                sock.close()
            except OSError:
                pass
            return


@_device_errors
def upload(
    ip: str, image_bytes: bytes, filename: str = IMAGE_NAME, content_type: str = "image/gif", show: bool = True,
    cancel: threading.Event | None = None,
) -> None:
    """POST the image to the device. Setting `cancel` aborts the transfer and raises UploadCancelled."""
    boundary = "----geekmagicclaude"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()

    if cancel is not None and cancel.is_set():
        raise UploadCancelled()
    conn = http.client.HTTPConnection(ip, timeout=CONNECT_TIMEOUT)
    done = threading.Event()
    try:
        conn.connect()
        conn.sock.settimeout(UPLOAD_TIMEOUT)  # connecting fails fast; the device then needs seconds to store the file
        # A tiny send buffer makes us send at the device's pace instead of dumping the whole file into
        # the OS buffer at once, so a cancel can still stop the transfer while the device is busy writing.
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        if cancel is not None:
            threading.Thread(target=_abort_on_cancel, args=(cancel, done, conn.sock), daemon=True).start()
        conn.request(
            "POST", "/doUpload?dir=/image/",
            body=(body[i:i + 2048] for i in range(0, len(body), 2048)),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        resp.read()
        if resp.status >= 300:
            raise UsageError(f"Upload failed: HTTP {resp.status}")
    except Exception:
        if cancel is not None and cancel.is_set():
            raise UploadCancelled() from None
        raise
    finally:
        done.set()
        conn.close()

    if show:
        show_image(ip, filename)


@_device_errors
def list_images(ip: str) -> set[str]:
    """Names of the image files stored on the device (parsed from its file-list page)."""
    html = urllib.request.urlopen(f"http://{ip}/filelist?dir=/image/", timeout=CONNECT_TIMEOUT).read().decode("utf-8", "replace")
    return set(re.findall(r"[\w.\-]+\.(?:gif|jpe?g|png)", html, re.IGNORECASE))


@_device_errors
def delete_image(ip: str, filename: str) -> None:
    urllib.request.urlopen(f"http://{ip}/delete?file=/image/{filename}", timeout=CONNECT_TIMEOUT).read()


@_device_errors
def show_image(ip: str, filename: str) -> None:
    """Pin an already-uploaded image on screen (fast: no file transfer)."""
    urllib.request.urlopen(f"http://{ip}/set?theme=3", timeout=CONNECT_TIMEOUT).read()
    urllib.request.urlopen(f"http://{ip}/set?img=/image/{filename}", timeout=CONNECT_TIMEOUT).read()


# --- Brightness and night mode: the device's own settings --------------------------------------------------

BRIGHTNESS_RANGE = (-10, 100)  # the slider range of the device's own settings page


def _clamp_brightness(level: float) -> int:
    return max(BRIGHTNESS_RANGE[0], min(BRIGHTNESS_RANGE[1], int(level)))


def _device_set(ip: str, query: str) -> None:
    """GET /set?<query>: the device answers "OK", or "FAIL" for what it doesn't understand."""
    body = urllib.request.urlopen(f"http://{ip}/set?{query}", timeout=CONNECT_TIMEOUT).read().decode("utf-8", "replace").strip()
    if body != "OK":
        raise OSError(f"the device refused the setting ({body or 'no answer'})")


@_device_errors
def set_brightness(ip: str, level: float) -> None:
    """Backlight level, -10..100 (the device doesn't let you read the current one back)."""
    _device_set(ip, f"brt={_clamp_brightness(level)}")


@_device_errors
def set_night_mode(ip: str, *, start_hour: int, end_hour: int, night_level: float, day_level: float, enabled: bool) -> None:
    """The device's own night schedule: between the two hours it runs at `night_level`, else at `day_level`. It keeps
    working with the computer off. (Same call as the "Night Mode" section of the device's settings page.)"""
    _device_set(ip, f"t1={start_hour % 24}&t2={end_hour % 24}&b1={_clamp_brightness(day_level)}"
                    f"&b2={_clamp_brightness(night_level)}&en={1 if enabled else 0}")


_cleaned_up = False


def _cleanup_old_images(ip: str) -> None:
    for name in _OLD_IMAGE_NAMES:
        try:
            urllib.request.urlopen(f"http://{ip}/delete?file=/image/{name}", timeout=10).read()
        except OSError:
            pass


def run_once(ip: str, animation: str, provider: str = "claude") -> None:
    push_usage(ip, PROVIDERS[provider](), animation, IMAGE_NAMES[provider])


def push_usage(
    ip: str, usage: dict, animation: str, filename: str = IMAGE_NAME, show: bool = True,
    cancel: threading.Event | None = None,
) -> None:
    """Render and upload `usage` as `filename`; `show=False` uploads without switching the screen to it."""
    rotating = animation in ("auto", "random")
    if rotating:
        animation = _pick_animation(usage)
    gif_bytes = render_animation(usage, animation)
    upload(ip, gif_bytes, filename, "image/gif", show, cancel)
    if rotating:
        history = _recent_animations.setdefault(usage.get("title", "Claude"), [])
        history.append(animation)  # now it really is on the device
        del history[:-RECENT_ANIMATIONS]
    global _cleaned_up
    if not _cleaned_up:  # one-time migration cleanup, not worth an extra request every push
        _cleanup_old_images(ip)
        _cleaned_up = True
    print(
        f"[{usage['now']:%H:%M:%S}] pushed [{animation}] ({len(gif_bytes) / 1024:.1f} KB) — "
        f"current {usage['current_pct']}% / weekly {usage['weekly_pct']}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", help="GeekMagic device IP, e.g. 192.168.1.18 (omit it to find the device on your network)")
    parser.add_argument("--discover", action="store_true", help="list the GeekMagic devices found on your network, then exit")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="repeat forever every N seconds")
    parser.add_argument(
        "--animation", default="auto", choices=[*ANIMATIONS, "auto", "random"],
        help="which animation: 'auto'/'random' (default) picks a random one from the provider's own set on every push, or name one",
    )
    parser.add_argument("--provider", default="claude", choices=list(PROVIDERS), help="which usage to show")
    args = parser.parse_args()

    if args.discover or not args.ip:
        import discover
        found = discover.scan()
        if args.discover:
            print("\n".join(found) if found else "No GeekMagic device found on this network.")
            return 0 if found else 1
        if len(found) != 1:
            print("error: " + (f"several devices found ({', '.join(found)}); pick one with --ip"
                               if found else "no GeekMagic device found; pass --ip"), file=sys.stderr)
            return 1
        args.ip = found[0]
        print(f"found the device at {args.ip}")

    try:
        if args.loop:
            while True:
                try:
                    run_once(args.ip, args.animation, args.provider)
                except (UsageError, OSError, ValueError) as e:
                    print(f"warning: {e}", file=sys.stderr)
                time.sleep(args.loop)
        else:
            run_once(args.ip, args.animation, args.provider)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
