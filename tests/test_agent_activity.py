"""Is an agent working right now? Judged from the tail of its local log."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_activity as a


def stamp(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Logs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write(self, name, lines, age=0.0):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        mtime = time.time() - age
        os.utime(path, (mtime, mtime))
        return path

    # entries as Claude Code writes them
    @staticmethod
    def prompt(ago, cwd="C:\\Work\\Acme App"):
        return json.dumps({"type": "user", "timestamp": stamp(ago), "cwd": cwd, "message": {"role": "user", "content": "do it"}})

    @staticmethod
    def reply(ago, stop, cwd="C:\\Work\\Acme App"):
        return json.dumps({"type": "assistant", "timestamp": stamp(ago), "cwd": cwd, "message": {"id": "m", "stop_reason": stop, "usage": {}}})

    @staticmethod
    def tool_result(ago, cwd="C:\\Work\\Acme App"):
        return json.dumps({"type": "user", "timestamp": stamp(ago), "cwd": cwd, "message": {"content": [{"type": "tool_result"}]}})

    @staticmethod
    def noise(ago):  # entries that aren't part of the conversation
        return [json.dumps({"type": "attachment", "timestamp": stamp(ago)}), json.dumps({"type": "file-history-snapshot"}),
                json.dumps({"type": "ai-title"}), "not even json"]


class ClaudeTests(Logs):
    def state(self):
        return a.claude_state(time.time(), self.root)

    def test_a_turn_in_progress_is_working(self):
        self.write("p/s.jsonl", [self.prompt(100), self.reply(95, "tool_use"), self.tool_result(90), self.reply(5, "tool_use")], age=5)
        state = self.state()
        self.assertTrue(state["working"])
        self.assertEqual(state["project"], "Acme App")
        self.assertAlmostEqual(time.time() - state["since"], 100, delta=3)

    def test_a_finished_reply_means_idle(self):
        self.write("p/s.jsonl", [self.prompt(100), self.reply(95, "tool_use"), self.tool_result(90), self.reply(20, "end_turn")], age=20)
        state = self.state()
        self.assertFalse(state["working"])
        self.assertIsNone(state["since"])
        self.assertEqual(state["project"], "Acme App")

    def test_other_ways_a_reply_can_end_a_turn(self):
        for stop in ("stop_sequence", "max_tokens", "refusal"):
            self.write(f"{stop}.jsonl", [self.prompt(30), self.reply(10, stop)], age=10)
        self.assertFalse(self.state()["working"])

    def test_a_prompt_just_sent_is_working_before_any_reply_is_written(self):
        self.write("p/s.jsonl", [self.reply(300, "end_turn"), self.prompt(20)], age=20)
        state = self.state()
        self.assertTrue(state["working"])
        self.assertAlmostEqual(time.time() - state["since"], 20, delta=3, msg="the run starts at the new prompt")

    def test_the_run_starts_after_the_last_finished_reply(self):
        self.write("p/s.jsonl", [self.prompt(900), self.reply(890, "end_turn"), self.prompt(200), self.reply(190, "tool_use"),
                                 self.tool_result(150), self.reply(8, "tool_use")], age=8)
        self.assertAlmostEqual(time.time() - self.state()["since"], 200, delta=3)

    def test_a_silent_turn_is_abandoned_after_a_while_but_a_running_tool_is_given_longer(self):
        self.write("a.jsonl", [self.prompt(500), self.tool_result(400)], age=400)  # nobody answered for 400 s
        self.assertFalse(self.state()["working"])
        os.remove(self.root / "a.jsonl")
        self.write("b.jsonl", [self.prompt(500), self.reply(400, "tool_use")], age=400)  # a long command is running
        self.assertTrue(self.state()["working"])
        os.remove(self.root / "b.jsonl")
        self.write("c.jsonl", [self.prompt(900), self.reply(700, "tool_use")], age=700)  # even that has a limit
        self.assertFalse(self.state()["working"])

    def test_logs_untouched_for_a_while_are_not_even_read(self):
        self.write("old.jsonl", [self.prompt(5000), self.reply(4990, "tool_use")], age=a.SCAN_AGE + 60)
        self.assertEqual(self.state(), {"working": False, "since": None, "last": None, "project": None})

    def test_non_conversation_entries_are_ignored(self):
        self.write("p/s.jsonl", [self.prompt(60), self.reply(50, "tool_use"), *self.noise(40), self.tool_result(10), *self.noise(5)], age=5)
        self.assertTrue(self.state()["working"])
        self.write("q/t.jsonl", [self.prompt(60), self.reply(10, "end_turn"), *self.noise(5)], age=5)
        self.assertTrue(self.state()["working"], "(the other log is still going)")

    def test_a_sub_agent_still_running_keeps_the_session_working(self):
        self.write("p/s.jsonl", [self.prompt(300), self.reply(250, "tool_use")], age=250)
        self.write("p/s/subagents/agent-1.jsonl", [self.prompt(120), self.reply(3, "tool_use")], age=3)
        self.assertTrue(self.state()["working"])

    def test_with_two_runs_the_longest_one_is_reported(self):
        self.write("old.jsonl", [self.prompt(300), self.reply(5, "tool_use", cwd="/p/one")], age=5)
        self.write("new.jsonl", [self.prompt(20), self.reply(4, "tool_use", cwd="/p/two")], age=4)
        self.assertEqual(self.state()["project"], "one")

    def test_nothing_to_read(self):
        idle = {"working": False, "since": None, "last": None, "project": None}
        self.assertEqual(a.claude_state(time.time(), self.root / "nope"), idle)
        self.assertEqual(self.state(), idle)
        self.write("empty.jsonl", [], age=1)
        self.assertEqual(self.state(), idle)

    def test_a_huge_log_is_only_read_from_the_end(self):
        filler = [self.reply(5000 - i, "tool_use") for i in range(4000)]
        path = self.write("big.jsonl", [self.prompt(5100), *filler, self.reply(4, "tool_use")], age=4)
        self.assertGreater(path.stat().st_size, a.TAIL_BYTES)
        self.assertTrue(self.state()["working"])


class CodexTests(Logs):
    @staticmethod
    def event(ago, kind=None, **payload):
        body = {"timestamp": stamp(ago), "type": "event_msg", "payload": {"type": kind or "message", **payload}}
        return json.dumps(body)

    @staticmethod
    def context(ago, cwd):
        return json.dumps({"timestamp": stamp(ago), "type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-6.1-sol"}})

    def state(self):
        return a.codex_state(time.time(), self.root)

    def test_a_started_task_is_working(self):
        self.write("r.jsonl", [self.context(100, "C:\\Work\\Alpha"), self.event(95, "task_started"), self.event(30), self.event(4)], age=4)
        state = self.state()
        self.assertTrue(state["working"])
        self.assertEqual(state["project"], "Alpha")
        self.assertAlmostEqual(time.time() - state["since"], 95, delta=3)

    def test_a_completed_or_aborted_task_is_idle(self):
        self.write("a.jsonl", [self.event(100, "task_started"), self.event(20, "task_complete")], age=20)
        self.assertFalse(self.state()["working"])
        os.remove(self.root / "a.jsonl")
        self.write("b.jsonl", [self.event(100, "task_started"), self.event(20, "turn_aborted")], age=20)
        self.assertFalse(self.state()["working"])

    def test_the_latest_marker_wins(self):
        self.write("r.jsonl", [self.event(300, "task_started"), self.event(200, "task_complete"), self.event(50, "task_started"), self.event(3)], age=3)
        self.assertTrue(self.state()["working"])

    def test_an_abandoned_task_stops_counting_after_a_long_silence(self):
        self.write("r.jsonl", [self.event(900, "task_started"), self.event(700)], age=700)
        self.assertFalse(self.state()["working"])

    def test_a_long_run_whose_markers_scrolled_out_of_the_tail_is_judged_by_recent_writes(self):
        busy = [self.event(2000 - i) for i in range(4000)]
        self.write("r.jsonl", [self.event(3000, "task_started"), *busy, self.event(2)], age=2)
        self.assertTrue(self.state()["working"])

    def test_nothing_to_read(self):
        idle = {"working": False, "since": None, "last": None, "project": None}
        self.assertEqual(a.codex_state(time.time(), self.root / "nope"), idle)
        self.assertEqual(self.state(), idle)


class TextTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual([a.duration_text(x) for x in (0, 5, 59.6, 60, 240, 3599, 3600, 3900, -4)],
                         ["0 s", "5 s", "1 min", "1 min", "4 min", "59 min", "1 h 00 min", "1 h 05 min", "0 s"])

    def test_both_providers_are_covered(self):
        self.assertEqual(set(a.STATES), {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
