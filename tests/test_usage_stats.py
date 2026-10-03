"""Activity stats: Claude's /usage blocks and Codex's local logs."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geekmagic_claude as g
import usage_stats as s

USAGE_TEXT = """You are currently using your subscription to power your Claude Code usage

Current session: 29% used · resets Oct 3, 1am (America/Bogota)
Current week (all models): 16% used · resets Oct 8, 3pm (America/Bogota)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine.

Last 24h · 375 requests · 4 sessions
  89% of your usage was at >150k context
  Top skills: /pbi 4%
  Top MCP servers: azure-devops 1%

Last 7d · 1560 requests · 13 sessions
  91% of your usage was at >150k context
  75% of your usage came from subagent-heavy sessions
  Top subagents: bot-architecture 5%, general-purpose 4%
"""


class ClaudeStatsTests(unittest.TestCase):
    def test_parses_both_blocks_and_their_notes(self):
        stats = s.parse_claude_stats(USAGE_TEXT)
        self.assertEqual([(w["label"], w["requests"], w["sessions"]) for w in stats["windows"]],
                         [("24h", 375, 4), ("7d", 1560, 13)])
        self.assertEqual(stats["windows"][0]["notes"][1], "Top skills: /pbi 4%")
        self.assertEqual(len(stats["windows"][1]["notes"]), 3)

    def test_unrelated_text_gives_none(self):
        self.assertIsNone(s.parse_claude_stats("Current session: 3% used"))
        self.assertIsNone(s.parse_claude_stats(""))

    def test_tolerates_missing_sessions_and_odd_spacing(self):
        stats = s.parse_claude_stats("Last 30d · 12 requests\n    just a note\n\nTrailing text\n  not a note")
        self.assertEqual(stats["windows"][0]["requests"], 12)
        self.assertIsNone(stats["windows"][0]["sessions"])
        self.assertEqual(stats["windows"][0]["notes"], ["just a note"])

    def test_lines_put_the_numbers_first_and_the_top_lists_next(self):
        lines = s.claude_lines(s.parse_claude_stats(USAGE_TEXT))
        self.assertEqual(lines[:2], ["24h · 375 requests · 4 sessions", "7d · 1,560 requests · 13 sessions"])
        self.assertTrue(lines[2].startswith("Top"))
        self.assertEqual(len(lines), 5)
        self.assertNotIn("your ", " ".join(lines))

    def test_singular_and_no_stats(self):
        self.assertEqual(s.claude_lines(s.parse_claude_stats("Last 24h · 1 requests · 1 sessions"))[0], "24h · 1 request · 1 session")
        self.assertEqual(s.claude_lines(None), ["No activity stats in /usage"])

    def test_a_real_parse_carries_the_stats(self):
        usage = g._parse(USAGE_TEXT)
        self.assertEqual(usage["stats"]["windows"][1]["label"], "7d")
        self.assertEqual(g.stats_lines(usage)[0], "24h · 375 requests · 4 sessions")


class CodexStatsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        s._cache.clear()

    def write(self, name, events, age_h=0.0):
        path = self.root / name
        lines = []
        for hours_ago, info in events:
            ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            lines.append(json.dumps({"timestamp": ts, "type": "event_msg",
                                     "payload": {"type": "token_count", "info": info}}))
        lines.insert(1, json.dumps({"timestamp": ts, "type": "response_item", "payload": {"type": "message"}}))
        path.write_text("\n".join(lines), encoding="utf-8")
        mtime = time.time() - age_h * 3600
        import os
        os.utime(path, (mtime, mtime))

    def test_counts_requests_and_sessions_per_window(self):
        info = {"last_token_usage": {"total_tokens": 10}}
        self.write("today.jsonl", [(1, info), (2, info), (30, info)])          # 2 in 24h, 3 in 7d
        self.write("last_week.jsonl", [(100, info), (101, info)], age_h=100)    # only in 7d
        self.write("ancient.jsonl", [(500, info)], age_h=500)                   # outside everything
        self.assertEqual(s.codex_local_stats(time.time(), self.root),
                         {"24h": {"requests": 2, "sessions": 1}, "7d": {"requests": 5, "sessions": 2}})

    def test_events_without_info_are_not_requests(self):
        self.write("a.jsonl", [(1, None), (1, {"last_token_usage": {}})])
        self.assertEqual(s.codex_local_stats(time.time(), self.root)["24h"], {"requests": 1, "sessions": 1})

    def test_no_logs_means_none_and_missing_folder_too(self):
        self.assertEqual(s.codex_local_stats(time.time(), self.root), {"24h": {"requests": 0, "sessions": 0}, "7d": {"requests": 0, "sessions": 0}})
        self.assertIsNone(s.codex_local_stats(time.time(), self.root / "nope"))

    def test_unchanged_logs_are_not_read_twice_but_grown_ones_are(self):
        info = {"last_token_usage": {}}
        self.write("a.jsonl", [(1, info)])
        s.codex_local_stats(time.time(), self.root)
        before = dict(s._cache)
        s.codex_local_stats(time.time(), self.root)
        self.assertIs(s._cache[str(self.root / "a.jsonl")], before[str(self.root / "a.jsonl")])  # served from the cache
        self.write("a.jsonl", [(1, info), (0.5, info)])
        self.assertEqual(s.codex_local_stats(time.time(), self.root)["24h"]["requests"], 2)

    def test_lines_show_plan_resets_and_counts(self):
        usage = {"codex_extra": {"plan": "plus", "free_resets": 3, "next_reset_expires_days": 12, "credits": None}}
        lines = s.codex_lines(usage, {"24h": {"requests": 26, "sessions": 1}, "7d": {"requests": 1521, "sessions": 17}})
        self.assertEqual(lines, ["Plan: Plus", "Free resets: 3 · next expires in 12d",
                                 "24h · 26 requests · 1 session", "7d · 1,521 requests · 17 sessions"])

    def test_lines_while_counting_and_without_extras(self):
        self.assertEqual(s.codex_lines({}, None), ["24h · counting...", "7d · counting..."])
        unlimited = s.codex_lines({"codex_extra": {"plan": "pro", "credits": "unlimited", "free_resets": 0}}, None)
        self.assertEqual(unlimited[0], "Plan: Pro · unlimited credits")
        self.assertNotIn("Free resets", " ".join(unlimited))


class CodexExtraTests(unittest.TestCase):
    def test_extra_is_built_from_the_account_data(self):
        limits = {
            "planType": "plus", "credits": {"hasCredits": False, "unlimited": False, "balance": "0E-10"},
            "_reset_credits": {"availableCount": 3, "credits": [
                {"status": "available", "expiresAt": time.time() + 12 * 86400},
                {"status": "available", "expiresAt": time.time() + 40 * 86400},
                {"status": "used", "expiresAt": time.time() + 1 * 86400},
            ]},
        }
        extra = g._codex_extra(limits)
        self.assertEqual((extra["plan"], extra["credits"], extra["free_resets"]), ("plus", None, 3))
        self.assertEqual(extra["next_reset_expires_days"], 12)

    def test_extra_survives_missing_fields(self):
        self.assertEqual(g._codex_extra({}), {"plan": None, "credits": None, "free_resets": 0, "next_reset_expires_days": None})
        self.assertEqual(g._codex_extra({"credits": {"unlimited": True}})["credits"], "unlimited")
        self.assertEqual(g._codex_extra({"credits": {"hasCredits": True, "balance": "12.5"}})["credits"], "12.5")


if __name__ == "__main__":
    unittest.main()
