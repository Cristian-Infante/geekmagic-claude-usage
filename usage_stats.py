"""Activity statistics, computed the same way for Claude and Codex so the two can be compared.

Both providers keep a local log of what you did with them. Each model call is one *request*, a *session* is a
conversation, and the numbers are: requests and sessions in the last 24 hours and 7 days, and requests per day for the
last seven days. (For Claude these agree with what `claude /usage` reports as "Last 24h / Last 7d".)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

DAYS = 7  # days in the chart: today and the six before it
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"

_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
Event = tuple  # (epoch seconds, session key, unique id of the request or None)
_cache: dict[str, tuple[float, int, list[Event]]] = {}  # path -> (mtime, size, events)


def _epoch(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _claude_events(path: Path) -> list[Event]:
    """Every model call in one Claude Code log: an `assistant` entry with token usage. A reply written in several
    blocks repeats its message id, so ids are what get counted. The session is the entry's own sessionId (sub-agents
    log under their parent's)."""
    found = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(message, dict) or "usage" not in message:
                continue
            when = _epoch(entry.get("timestamp") or "")
            if when is not None:
                found.append((when, entry.get("sessionId") or path.stem, message.get("id") or entry.get("uuid")))
    return found


def _codex_events(path: Path) -> list[Event]:
    """Every model call in one Codex session log: a `token_count` event that carries token info."""
    found = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"token_count"' not in line or '"info":null' in line.replace(" ", ""):
                continue
            match = _TIMESTAMP_RE.search(line)
            when = _epoch(match.group(1)) if match else None
            if when is not None:
                found.append((when, path.stem, None))
    return found


def _events(path: Path, mtime: float, size: int, reader) -> list[Event]:
    """A finished log never changes, so each is read once; only one that grew since last time is read again."""
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    try:
        found = reader(path)
    except OSError:
        return []
    _cache[str(path)] = (mtime, size, found)
    return found


def collect(root: Path, reader, now_epoch: float) -> list[Event]:
    """The events of every log under `root` recent enough to matter for the chart."""
    oldest = _start_of_day(now_epoch, DAYS - 1)
    events: list[Event] = []
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= oldest:  # a log untouched since before the chart began has nothing in it
            events.extend(_events(path, stat.st_mtime, stat.st_size, reader))
    return events


def _start_of_day(epoch: float, days_ago: int) -> float:
    day = datetime.fromtimestamp(epoch).date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day).timestamp()


def summarize(events: list[Event], now_epoch: float) -> dict:
    """{"24h": {"requests", "sessions"}, "7d": {...}, "days": [{"date", "requests"}] oldest first, 7 entries}."""
    seen: set = set()
    unique: list[Event] = []
    for event in sorted(events):
        key = (event[1], event[2]) if event[2] is not None else (event[0], event[1], len(unique))
        if key not in seen:
            seen.add(key)
            unique.append(event)
    windows = {"24h": 24 * 3600, "7d": 7 * 24 * 3600}
    summary: dict = {}
    for label, span in windows.items():
        inside = [e for e in unique if now_epoch - e[0] <= span]
        summary[label] = {"requests": len(inside), "sessions": len({e[1] for e in inside})}
    today = datetime.fromtimestamp(now_epoch).date()
    counts = {today - timedelta(days=n): 0 for n in range(DAYS - 1, -1, -1)}
    for when, _, _ in unique:
        day = datetime.fromtimestamp(when).date()
        if day in counts:
            counts[day] += 1
    summary["days"] = [{"date": day.isoformat(), "requests": n} for day, n in counts.items()]
    return summary


def claude_stats(now_epoch: float, root: Path | None = None) -> dict | None:
    root = root or CLAUDE_DIR
    return summarize(collect(root, _claude_events, now_epoch), now_epoch) if root.is_dir() else None


def codex_stats(now_epoch: float, root: Path | None = None) -> dict | None:
    root = root or CODEX_DIR
    return summarize(collect(root, _codex_events, now_epoch), now_epoch) if root.is_dir() else None


STATS = {"claude": claude_stats, "codex": codex_stats}


# --- text for the screen ------------------------------------------------------------------------------

def counts_text(label: str, counts: dict) -> str:
    """"24h · 375 requests · 4 sessions" (singular for 1, thousands separators)."""
    r, s = counts["requests"], counts["sessions"]
    return f"{label} · {r:,} request{'' if r == 1 else 's'} · {s} session{'' if s == 1 else 's'}"


def weekday(date_text: str) -> str:
    """"2026-10-02" -> "Fr" (English, whatever the system language)."""
    return ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")[datetime.fromisoformat(date_text).weekday()]
