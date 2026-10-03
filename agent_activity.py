"""Is Claude Code / Codex working right now? Read from the tail of their local logs, which both keep writing as they go.

Claude Code appends an `assistant` entry for every reply (its `stop_reason` is "tool_use" while it wants to run a tool
and "end_turn" when it is done) and a `user` entry for the prompt and for each tool result. So a log whose last
conversation entry is anything but a finished reply is a turn in progress. Codex marks every turn with
`task_started` and `task_complete` (or `turn_aborted`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import usage_stats

SCAN_AGE = 900  # only logs written within the last 15 minutes can belong to a turn in progress
TURN_MAX_AGE = 120  # seconds of silence after which a turn that isn't running a tool counts as abandoned
TOOL_MAX_AGE = 600  # ...but while a tool runs (a long build, or waiting for you to approve) it may be quiet this long
TAIL_BYTES = 262_144  # how much of the end of a log is read
FINISHED = {"end_turn", "stop_sequence", "max_tokens", "refusal", "pause_turn"}  # a reply that ends the turn


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace").splitlines()
    return data[1:] if size > TAIL_BYTES else data  # the first line of a tail is usually cut in half


def _epoch(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _claude_file_state(path: Path, now_epoch: float) -> dict | None:
    """State of one Claude Code log, or None if it has no conversation entries in its tail."""
    turn = []  # (time, kind, stop_reason) of the user/assistant entries, in order
    project = None
    for line in _tail_lines(path):
        if '"user"' not in line and '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        when = _epoch(entry.get("timestamp"))
        if kind not in ("user", "assistant") or when is None:
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        turn.append((when, kind, message.get("stop_reason")))
        project = usage_stats.project_label(entry["cwd"]) if entry.get("cwd") else project
    if not turn:
        return None
    last_time, last_kind, last_stop = turn[-1]
    age = now_epoch - last_time
    finished = last_kind == "assistant" and last_stop in FINISHED
    running_tool = last_kind == "assistant" and last_stop in ("tool_use", None)  # None: still being written
    working = not finished and age <= (TOOL_MAX_AGE if running_tool else TURN_MAX_AGE)
    since = None
    if working:  # the turn began right after the last reply that finished one
        begin = 0
        for i, (_, kind, stop) in enumerate(turn):
            if kind == "assistant" and stop in FINISHED:
                begin = i + 1
        since = turn[min(begin, len(turn) - 1)][0]
    return {"working": working, "since": since, "last": last_time, "project": project}


def _codex_file_state(path: Path, now_epoch: float) -> dict | None:
    """State of one Codex session log, from its task markers."""
    marker, marker_time, last_time, project = None, None, None, None
    for line in _tail_lines(path):
        found = next((k for k in ("task_started", "task_complete", "turn_aborted") if f'"{k}"' in line), None)
        if '"turn_context"' in line:
            try:
                cwd = (json.loads(line).get("payload") or {}).get("cwd")
                project = usage_stats.project_label(cwd) if cwd else project
            except ValueError:
                pass
        match = usage_stats._TIMESTAMP_RE.search(line)
        when = usage_stats._epoch(match.group(1)) if match else None
        if when is not None:
            last_time = when
        if found:
            marker, marker_time = found, when
    if last_time is None:
        return None
    if marker is None:  # a long run whose markers have scrolled out of the tail: judge by whether it's still writing
        working = now_epoch - last_time <= TURN_MAX_AGE
        return {"working": working, "since": None, "last": last_time, "project": project}
    working = marker == "task_started" and now_epoch - last_time <= TOOL_MAX_AGE
    return {"working": working, "since": marker_time if working else None, "last": last_time, "project": project}


def _state(root: Path, reader, now_epoch: float) -> dict:
    """The most recent working log's state, or the freshest log's if none is working. Always has the same keys."""
    idle = {"working": False, "since": None, "last": None, "project": None}
    if not root.is_dir():
        return idle
    states = []
    for path in root.rglob("*.jsonl"):
        try:
            if now_epoch - path.stat().st_mtime > SCAN_AGE:
                continue
            state = reader(path, now_epoch)
        except OSError:
            continue
        if state:
            states.append(state)
    if not states:
        return idle
    working = [s for s in states if s["working"]]
    if working:
        return min(working, key=lambda s: s["since"] or now_epoch)  # the run that has been going the longest
    return max(states, key=lambda s: s["last"])


def claude_state(now_epoch: float, root: Path | None = None) -> dict:
    return _state(root or usage_stats.CLAUDE_DIR, _claude_file_state, now_epoch)


def codex_state(now_epoch: float, root: Path | None = None) -> dict:
    return _state(root or usage_stats.CODEX_DIR, _codex_file_state, now_epoch)


STATES = {"claude": claude_state, "codex": codex_state}


def duration_text(seconds: float) -> str:
    """"45 s", "4 min", "1 h 05 min"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"
