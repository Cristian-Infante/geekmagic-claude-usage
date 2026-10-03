"""Usage statistics beyond the limits: what `claude /usage` says about your recent activity, and the same kind of
numbers for Codex (from its account data and its local session logs)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
_HEADER_RE = re.compile(r"^Last\s+(\w+)\s*·\s*([\d,]+)\s+requests?(?:\s*·\s*([\d,]+)\s+sessions?)?", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def _int(text: str | None) -> int | None:
    return int(text.replace(",", "")) if text else None


def _counts(requests: int | None, sessions: int | None) -> str:
    """"375 requests · 4 sessions" (singular for 1, thousands separators)."""
    parts = []
    if requests is not None:
        parts.append(f"{requests:,} request{'' if requests == 1 else 's'}")
    if sessions is not None:
        parts.append(f"{sessions} session{'' if sessions == 1 else 's'}")
    return " · ".join(parts)


def parse_claude_stats(text: str) -> dict | None:
    """The "Last 24h / Last 7d" blocks of `claude /usage`:
    {"windows": [{"label": "24h", "requests": 375, "sessions": 4, "notes": ["89% of your usage was at ..."]}]}.
    None if the text has none (the format is Claude Code's to change, so this is deliberately forgiving)."""
    windows: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        header = _HEADER_RE.match(line.strip()) if not line.startswith((" ", "\t")) else None
        if header:
            current = {"label": header.group(1), "requests": _int(header.group(2)), "sessions": _int(header.group(3)), "notes": []}
            windows.append(current)
        elif current is not None and line.startswith((" ", "\t")) and line.strip():
            current["notes"].append(line.strip())
        elif not line.strip() or not line.startswith((" ", "\t")):
            current = None
    return {"windows": windows} if windows else None


def _short(note: str) -> str:
    """"89% of your usage was at >150k context" -> "89% of usage at >150k context"."""
    return (note.replace("of your usage was at", "of usage at").replace("of your usage came from", "of usage from")
            .replace("your ", ""))


def claude_lines(stats: dict | None, limit: int = 5) -> list[str]:
    """The lines for Claude's stats panel: one per window, then the most telling notes."""
    if not stats:
        return ["No activity stats in /usage"]
    lines = []
    for w in stats["windows"]:
        lines.append(f"{w['label']} · {_counts(w['requests'], w['sessions'])}")
    notes = [_short(n) for w in stats["windows"] for n in w["notes"]]
    notes.sort(key=lambda n: (not n.startswith("Top"), ))  # "Top skills/subagents" first: they say the most
    return (lines + notes)[:limit]


def codex_lines(usage: dict, local: dict | None, limit: int = 5) -> list[str]:
    """The lines for Codex's panel: plan and free resets (from the API) plus local request/session counts."""
    extra = usage.get("codex_extra") or {}
    lines = []
    plan = extra.get("plan")
    if plan:
        credits = extra.get("credits")
        lines.append(f"Plan: {plan.title()}" + (" · unlimited credits" if credits == "unlimited" else
                                                  f" · credits {credits}" if credits else ""))
    free = extra.get("free_resets")
    if free:
        days = extra.get("next_reset_expires_days")
        lines.append(f"Free resets: {free}" + (f" · next expires in {days}d" if days is not None else ""))
    for label in ("24h", "7d"):
        counts = (local or {}).get(label)
        if counts is None:
            lines.append(f"{label} · counting...")
        else:
            lines.append(f"{label} · {_counts(counts['requests'], counts['sessions'])}")
    return lines[:limit]


# --- Codex local activity ------------------------------------------------------------------------------

_cache: dict[str, tuple[float, int, list[float]]] = {}  # path -> (mtime, size, request epochs)


def _epochs(path: Path, mtime: float, size: int) -> list[float]:
    """Epoch seconds of every model call (a `token_count` event) in one session log. Finished logs never change,
    so each is read once; only a log that grew since last time is read again."""
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    found: list[float] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"token_count"' not in line or '"info":null' in line.replace(" ", ""):
                    continue
                match = _TIMESTAMP_RE.search(line)
                if match:
                    try:
                        found.append(datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        pass
    except OSError:
        return []
    _cache[str(path)] = (mtime, size, found)
    return found


def codex_local_stats(now_epoch: float, sessions_dir: Path | None = None) -> dict | None:
    """{"24h": {"requests": n, "sessions": n}, "7d": {...}} from Codex's local session logs, or None if there are none.
    Requests are model calls; a session is a log that saw activity in the window."""
    root = sessions_dir or CODEX_SESSIONS
    if not root.is_dir():
        return None
    windows = {"24h": 24 * 3600, "7d": 7 * 24 * 3600}
    totals = {label: {"requests": 0, "sessions": 0} for label in windows}
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if now_epoch - stat.st_mtime > windows["7d"]:
            continue
        epochs = _epochs(path, stat.st_mtime, stat.st_size)
        for label, span in windows.items():
            count = sum(1 for t in epochs if now_epoch - t <= span)
            if count:
                totals[label]["requests"] += count
                totals[label]["sessions"] += 1
    return totals
