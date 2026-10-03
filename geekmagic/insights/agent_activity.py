"""What is Claude Code / Codex doing right now: working, waiting for you, or idle? Read from the tail of their local
logs, which both keep writing as they go.

Claude Code appends an `assistant` entry for every reply (its `stop_reason` is "tool_use" while it wants to run a tool
and "end_turn" when it is done) and a `user` entry for the prompt and for each tool result. So a log whose last
conversation entry is anything but a finished reply is a turn in progress. It is *waiting for you* when a tool call has
no result and that tool is one that only waits on a person: `AskUserQuestion` (it asked you something) and
`ExitPlanMode` (it wants your approval of a plan) at once, and a tool that normally returns in milliseconds (`Read`,
`Edit`, `Write`...) that has been pending for a while, which means it is waiting for you to allow it. A long `Bash`
command looks the same from the log as a permission prompt, so it is never taken for waiting.

Codex marks every turn with `task_started` and `task_complete` (or `turn_aborted`); an approval or question request
that is the last thing in the log means it is waiting (best effort: not every setup records those).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from geekmagic.insights import usage_stats

SCAN_AGE = 3 * 3600  # logs older than this can't belong to a turn that is still being worked on or waited for
TURN_MAX_AGE = 120  # seconds of silence after which a turn that isn't running a tool counts as abandoned
TOOL_MAX_AGE = 600  # ...but while a tool runs (a long build) it may be quiet this long
WAIT_MAX_AGE = 3 * 3600  # a question or approval left unanswered this long is taken for a forgotten session
PERMISSION_WAIT = 20  # an instant tool still pending after this many seconds is waiting for you to allow it
TAIL_BYTES = 262_144  # how much of the end of a log is read
FINISHED = {"end_turn", "stop_sequence", "max_tokens", "refusal", "pause_turn"}  # a reply that ends the turn
ASKS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}  # tools that exist to wait for you
INSTANT_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "TodoWrite"}
CODEX_REQUESTS = ("approval_request", "request_user_input", "elicitation_request")  # Codex events that wait on you

_PAYLOAD_TYPE_RE = re.compile(r'"payload"\s*:\s*\{\s*"type"\s*:\s*"([A-Za-z_]+)"')
_cache: dict[str, tuple[float, int, dict | None]] = {}  # path -> (mtime, size, summary of its tail)


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


def _summary(path: Path, mtime: float, size: int, reader) -> dict | None:
    """What a log's tail says, independent of the time of day: a log that hasn't changed is only read once."""
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    summary = reader(path)
    _cache[str(path)] = (mtime, size, summary)
    return summary


def _claude_summary(path: Path) -> dict | None:
    turn = []  # (time, kind, stop_reason) of the user/assistant entries, in order
    pending: dict[str, tuple[str, float]] = {}  # tool call id -> (tool name, when it was called), until its result
    project = session = None
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
        session = entry.get("sessionId") or session  # a sub-agent's log carries its parent session's id
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                pending[block["id"]] = (block.get("name") or "", when)
            elif block.get("type") == "tool_result":
                pending.pop(block.get("tool_use_id"), None)
    if not turn:
        return None
    begin = 0
    for i, (_, kind, stop) in enumerate(turn):  # the turn began right after the last reply that finished one
        if kind == "assistant" and stop in FINISHED:
            begin = i + 1
    return {"turn": turn, "begin": turn[min(begin, len(turn) - 1)][0], "pending": pending, "project": project,
            "session": session or path.stem}


def _claude_state(summary: dict | None, now_epoch: float) -> dict | None:
    if not summary:
        return None
    last_time, last_kind, last_stop = summary["turn"][-1]
    age = now_epoch - last_time
    finished = last_kind == "assistant" and last_stop in FINISHED
    waiting_for = None
    if not finished and age <= WAIT_MAX_AGE:
        for name, when in summary["pending"].values():
            if name in ASKS:
                waiting_for = ASKS[name]
                break
            if name in INSTANT_TOOLS and now_epoch - when >= PERMISSION_WAIT:
                waiting_for = f"approval ({name})"
    running_tool = last_kind == "assistant" and last_stop in ("tool_use", None)  # None: still being written
    working = waiting_for is not None or (not finished and age <= (TOOL_MAX_AGE if running_tool else TURN_MAX_AGE))
    return {"id": summary["session"], "working": working, "waiting": waiting_for is not None, "waiting_for": waiting_for,
            "since": summary["begin"] if working else None, "last": last_time, "project": summary["project"]}


def _codex_summary(path: Path) -> dict | None:
    marker, marker_time, last_time, last_type, project = None, None, None, None, None
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
        typed = _PAYLOAD_TYPE_RE.search(line)
        last_type = typed.group(1) if typed else last_type
        if found:
            marker, marker_time = found, when
    if last_time is None:
        return None
    return {"marker": marker, "marker_time": marker_time, "last": last_time, "last_type": last_type, "project": project,
            "session": path.stem}


def _codex_state(summary: dict | None, now_epoch: float) -> dict | None:
    if not summary:
        return None
    age = now_epoch - summary["last"]
    if summary["marker"] is None:  # a long run whose markers have scrolled out of the tail: judge by recent writes
        working = age <= TURN_MAX_AGE
        return {"id": summary["session"], "working": working, "waiting": False, "waiting_for": None, "since": None,
                "last": summary["last"], "project": summary["project"]}
    in_turn = summary["marker"] == "task_started"
    asking = in_turn and age <= WAIT_MAX_AGE and any(k in (summary["last_type"] or "") for k in CODEX_REQUESTS)
    working = in_turn and (asking or age <= TOOL_MAX_AGE)
    return {"id": summary["session"], "working": working, "waiting": asking, "waiting_for": "approval" if asking else None,
            "since": summary["marker_time"] if working else None, "last": summary["last"], "project": summary["project"]}


def _sessions(states: list[dict]) -> list[dict]:
    """One entry per session (a session's sub-agent logs are merged into it): working if any of its logs is, waiting
    if any is, the project of its freshest log. This is what lets several projects be followed at the same time."""
    merged: dict[str, dict] = {}
    for state in sorted(states, key=lambda s: s["last"]):  # oldest first, so the freshest log's project wins
        mine = merged.setdefault(state["id"], {**state, "working": False, "waiting": False, "waiting_for": None, "since": None})
        mine["last"] = max(mine["last"], state["last"])
        mine["project"] = state["project"] or mine["project"]
        if state["working"]:
            mine["working"] = True
            mine["since"] = min(filter(None, (mine["since"], state["since"])), default=None)
        if state["waiting"]:
            mine["waiting"], mine["waiting_for"] = True, state["waiting_for"]
    return list(merged.values())


def _state(root: Path, reader, decide, now_epoch: float) -> dict:
    """The most telling log's state: a waiting one first (it needs you), else the working one that has been going the
    longest, else the freshest log's, plus "sessions" (every session seen, see _sessions). Always has the same keys."""
    idle = {"working": False, "waiting": False, "waiting_for": None, "since": None, "last": None, "project": None,
            "sessions": []}
    if not root.is_dir():
        return idle
    states = []
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
            if now_epoch - stat.st_mtime > SCAN_AGE:
                continue
            state = decide(_summary(path, stat.st_mtime, stat.st_size, reader), now_epoch)
        except OSError:
            continue
        if state:
            states.append(state)
    if not states:
        return idle
    waiting = [s for s in states if s["waiting"]]
    working = [s for s in states if s["working"]]
    if waiting:
        top = max(waiting, key=lambda s: s["last"])
    elif working:
        top = min(working, key=lambda s: s["since"] or now_epoch)
    else:
        top = max(states, key=lambda s: s["last"])
    return {**{k: v for k, v in top.items() if k != "id"}, "sessions": _sessions(states)}


def claude_state(now_epoch: float, root: Path | None = None) -> dict:
    return _state(root or usage_stats.CLAUDE_DIR, _claude_summary, _claude_state, now_epoch)


def codex_state(now_epoch: float, root: Path | None = None) -> dict:
    return _state(root or usage_stats.CODEX_DIR, _codex_summary, _codex_state, now_epoch)



def duration_text(seconds: float) -> str:
    """"45 s", "4 min", "1 h 05 min"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"
