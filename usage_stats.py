"""Activity statistics, computed the same way for Claude and Codex so the two can be compared.

Both providers keep a local log of what you did with them. Each model call is one *request*, a *session* is a
conversation, and from the logs come: requests and sessions in the last 24 hours and 7 days, requests per day for
the last week, which projects and models those requests went to, and at what hours of the day you work. (For Claude
the totals agree with what `claude /usage` reports as "Last 24h / Last 7d".)
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

DAYS = 7  # days in the chart: today and the six before it
HOURS_DAYS = 28  # days the hours-of-the-day pattern looks back over (four full weeks)
TOP = 6  # projects / models kept per provider (the screens show the top few)
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"

_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
_CLAUDE_MODEL_RE = re.compile(r"^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?!\d)")
_CLAUDE_OLD_MODEL_RE = re.compile(r"^claude-(\d+)(?:-(\d+))?-(opus|sonnet|haiku)")
Event = tuple  # (epoch seconds, session key, unique id of the request or None, project, model)
_cache: dict[str, tuple[float, int, list[Event]]] = {}  # path -> (mtime, size, events)


def _epoch(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


GENERIC_FOLDERS = {"src", "app", "apps", "frontend", "backend", "server", "client", "web", "api", "lib", "packages",
                   "docs", "test", "tests", "scripts", "public", "dist", "build"}  # say nothing without their parent


def project_label(cwd: str | None) -> str:
    """The folder a session ran in, by name: "C:\\Work\\Acme App" -> "Acme App" (either kind of slash, and
    Windows drive letters in any case don't matter). A folder like `frontend` or `src` is shown with its parent
    ("Storefront/frontend"), as on its own it could be any project's."""
    parts = [p for p in re.split(r"[\\/]", cwd or "") if p]
    if not parts:
        return "(unknown)"
    if parts[-1].lower() in GENERIC_FOLDERS and len(parts) > 1 and not parts[-2].endswith(":"):
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]


def model_label(model: str | None) -> str:
    """"claude-opus-5-5" -> "Opus 5.5", "claude-sonnet-5" -> "Sonnet 5", older "claude-3-5-sonnet-2024..." ->
    "Sonnet 3.5"; any other name (Codex's) is shown as is."""
    if not model:
        return "(unknown)"
    new = _CLAUDE_MODEL_RE.match(model)
    if new:
        family, major, minor = new.groups()
        return f"{family.title()} {major}" + (f".{minor}" if minor else "")
    old = _CLAUDE_OLD_MODEL_RE.match(model)
    if old:
        major, minor, family = old.groups()
        return f"{family.title()} {major}" + (f".{minor}" if minor else "")
    return model


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
            if message.get("model") == "<synthetic>":  # Claude Code's own notices, not a model call
                continue
            when = _epoch(entry.get("timestamp") or "")
            if when is not None:
                found.append((when, entry.get("sessionId") or path.stem, message.get("id") or entry.get("uuid"),
                              project_label(entry.get("cwd")), model_label(message.get("model"))))
    return found


def _codex_events(path: Path) -> list[Event]:
    """Every model call in one Codex session log: a `token_count` event that carries token info. Which project and
    model it belongs to is whatever the latest `turn_context` / `session_meta` before it said."""
    found = []
    project, model = "(unknown)", "(unknown)"
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"turn_context"' in line or '"session_meta"' in line:
                try:
                    payload = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    project = project_label(payload.get("cwd")) if payload.get("cwd") else project
                    model = payload.get("model") or model
            elif '"token_count"' in line and '"info":null' not in line.replace(" ", ""):
                match = _TIMESTAMP_RE.search(line)
                when = _epoch(match.group(1)) if match else None
                if when is not None:
                    found.append((when, path.stem, None, project, model))
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
    """The events of every log under `root` recent enough to matter (the longest look-back is the hours pattern)."""
    oldest = _start_of_day(now_epoch, max(DAYS, HOURS_DAYS) - 1)
    events: list[Event] = []
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= oldest:  # a log untouched since before then has nothing in it that matters
            events.extend(_events(path, stat.st_mtime, stat.st_size, reader))
    return events


def _start_of_day(epoch: float, days_ago: int) -> float:
    day = datetime.fromtimestamp(epoch).date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day).timestamp()


def _top(counter: Counter) -> list[dict]:
    return [{"name": name, "requests": n} for name, n in counter.most_common(TOP)]


def summarize(events: list[Event], now_epoch: float) -> dict:
    """What the screens need, from a provider's events:
    {"24h": {"requests", "sessions"}, "7d": {...}, "days": [{"date", "requests"}] oldest first (7 entries),
     "projects" / "models": [{"name", "requests"}] busiest first over the last 7 days, "total": requests those
     shares are of, "hours": requests per hour of the day (24 numbers) over the last HOURS_DAYS days}."""
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
    hours = [0] * 24
    hours_since = _start_of_day(now_epoch, HOURS_DAYS - 1)
    for when, *_ in unique:
        moment = datetime.fromtimestamp(when)
        if moment.date() in counts:
            counts[moment.date()] += 1
        if when >= hours_since:
            hours[moment.hour] += 1
    summary["days"] = [{"date": day.isoformat(), "requests": n} for day, n in counts.items()]
    week = [e for e in unique if now_epoch - e[0] <= windows["7d"]]
    summary["projects"] = _top(Counter(e[3] for e in week))
    summary["models"] = _top(Counter(e[4] for e in week))
    summary["total"] = len(week)
    summary["hours"] = hours
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


def hour12(hour: int) -> str:
    """14 -> "2 PM", 0 -> "12 AM"."""
    return f"{hour % 12 or 12} {'AM' if hour % 24 < 12 else 'PM'}"


def busiest_hours(hours: list[int], span: int = 2) -> tuple[int, int] | None:
    """The `span` consecutive hours (wrapping past midnight) with the most requests, as (first hour, hour after the
    last); None if there's no activity at all."""
    if not any(hours):
        return None
    # ties: the stretch that starts on its busier hour (not one that starts with an empty hour), then the earlier one
    best = max(range(24), key=lambda start: (sum(hours[(start + i) % 24] for i in range(span)), hours[start], -start))
    return best, (best + span) % 24


def hours_text(window: tuple[int, int]) -> str:
    """(14, 16) -> "2-4 PM"; across noon or midnight both ends say so: (11, 13) -> "11 AM-1 PM"."""
    start, end = window
    a, b = hour12(start), hour12(end)
    return f"{a.split()[0]}-{b}" if a.split()[1] == b.split()[1] else f"{a}-{b}"


def shares(items: list[dict], total: int, limit: int = 3) -> list[tuple[str, float]]:
    """The first `limit` of `items` as (name, share of `total` in 0..1), for the breakdown bars."""
    return [(item["name"], item["requests"] / total) for item in items[:limit]] if total else []
