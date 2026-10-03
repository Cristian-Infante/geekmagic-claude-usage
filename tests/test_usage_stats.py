"""Activity stats: the same numbers for Claude and Codex, from their local logs."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import geekmagic_claude as g
import usage_stats as s


def stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ago(**delta) -> datetime:
    return datetime.now().astimezone() - timedelta(**delta)


def local(days_ago: int, hour: int, minute: int = 0) -> datetime:
    """A local wall-clock time `days_ago` days back, so a test can place events on specific calendar days."""
    day = datetime.now().date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


class LogFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        s._cache.clear()

    def claude_log(self, name, entries, mtime=None):
        """entries: (datetime, session id, message id) assistant replies, one JSON line each."""
        lines = [json.dumps({"type": "assistant", "timestamp": stamp(when), "sessionId": sid,
                             "message": {"id": mid, "usage": {"input_tokens": 1, "output_tokens": 1}}})
                 for when, sid, mid in entries]
        self.write(name, lines, mtime)

    def codex_log(self, name, whens, mtime=None):
        lines = [json.dumps({"timestamp": stamp(when), "type": "event_msg",
                             "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 5}}}})
                 for when in whens]
        self.write(name, lines, mtime)

    def write(self, name, lines, mtime=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class ClaudeStatsTests(LogFixture):
    def test_counts_replies_and_sessions_per_window(self):
        self.claude_log("proj/a.jsonl", [(ago(hours=1), "A", "m1"), (ago(hours=3), "A", "m2"), (ago(hours=30), "A", "m3")])
        self.claude_log("proj/b.jsonl", [(ago(days=3), "B", "m4"), (ago(days=3, hours=1), "B", "m5")])
        stats = s.claude_stats(time.time(), self.root)
        self.assertEqual(stats["24h"], {"requests": 2, "sessions": 1})
        self.assertEqual(stats["7d"], {"requests": 5, "sessions": 2})

    def test_a_reply_written_in_several_blocks_counts_once(self):
        when = ago(hours=1)
        self.claude_log("a.jsonl", [(when, "A", "same"), (when, "A", "same"), (when, "A", "other")])
        self.assertEqual(s.claude_stats(time.time(), self.root)["24h"]["requests"], 2)

    def test_subagent_logs_belong_to_the_parent_session(self):
        self.claude_log("proj/parent.jsonl", [(ago(hours=2), "P", "m1")])
        self.claude_log("proj/P/subagents/agent-1.jsonl", [(ago(hours=2), "P", "m2"), (ago(hours=1), "P", "m3")])
        self.assertEqual(s.claude_stats(time.time(), self.root)["24h"], {"requests": 3, "sessions": 1})

    def test_only_assistant_replies_with_usage_count(self):
        self.write("a.jsonl", [
            json.dumps({"type": "user", "timestamp": stamp(ago(hours=1)), "message": {"usage": {}}}),
            json.dumps({"type": "assistant", "timestamp": stamp(ago(hours=1)), "message": {"id": "x"}}),  # no usage
            json.dumps({"type": "assistant", "timestamp": "not a time", "message": {"id": "y", "usage": {}}}),
            "this line is not json {{{ usage assistant",
            json.dumps({"type": "assistant", "timestamp": stamp(ago(hours=1)), "sessionId": "S", "message": {"id": "ok", "usage": {}}}),
        ])
        self.assertEqual(s.claude_stats(time.time(), self.root)["24h"], {"requests": 1, "sessions": 1})

    def test_a_missing_folder_means_no_stats(self):
        self.assertIsNone(s.claude_stats(time.time(), self.root / "nope"))


class CodexStatsTests(LogFixture):
    def test_counts_model_calls_and_sessions_per_window(self):
        self.codex_log("2026/10/02/today.jsonl", [ago(hours=1), ago(hours=2), ago(hours=30)])
        self.codex_log("2026/09/30/older.jsonl", [ago(days=3), ago(days=3, hours=1)])
        stats = s.codex_stats(time.time(), self.root)
        self.assertEqual(stats["24h"], {"requests": 2, "sessions": 1})
        self.assertEqual(stats["7d"], {"requests": 5, "sessions": 2})

    def test_events_without_token_info_are_not_requests(self):
        self.write("a.jsonl", [
            json.dumps({"timestamp": stamp(ago(hours=1)), "payload": {"type": "token_count", "info": None}}),
            json.dumps({"timestamp": stamp(ago(hours=1)), "payload": {"type": "token_count", "info": {"x": 1}}}),
            json.dumps({"timestamp": stamp(ago(hours=1)), "payload": {"type": "message"}}),
        ])
        self.assertEqual(s.codex_stats(time.time(), self.root)["24h"], {"requests": 1, "sessions": 1})

    def test_a_missing_folder_means_no_stats(self):
        self.assertIsNone(s.codex_stats(time.time(), self.root / "nope"))


class SameShapeTests(LogFixture):
    def test_both_providers_give_exactly_the_same_kind_of_numbers(self):
        self.claude_log("c/a.jsonl", [(ago(hours=1), "A", "m1")])
        self.codex_log("x/b.jsonl", [ago(hours=1)])
        claude = s.claude_stats(time.time(), self.root / "c")
        codex = s.codex_stats(time.time(), self.root / "x")
        self.assertEqual(set(claude), set(codex))
        self.assertEqual(set(claude["24h"]), set(codex["24h"]))
        self.assertEqual([d["date"] for d in claude["days"]], [d["date"] for d in codex["days"]])
        self.assertEqual(claude["24h"], codex["24h"])

    def test_the_chart_covers_today_and_the_six_days_before_oldest_first(self):
        self.codex_log("a.jsonl", [ago(hours=1)])
        days = s.codex_stats(time.time(), self.root)["days"]
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1]["date"], datetime.now().date().isoformat())
        self.assertEqual(days[0]["date"], (datetime.now().date() - timedelta(days=6)).isoformat())
        self.assertEqual([d["date"] for d in days], sorted(d["date"] for d in days))

    def test_requests_land_on_their_local_calendar_day(self):
        self.codex_log("a.jsonl", [local(0, 0, 5), local(0, 10), local(1, 23, 30), local(1, 8), local(3, 12), local(6, 1)])
        by_date = {d["date"]: d["requests"] for d in s.codex_stats(time.time(), self.root)["days"]}
        today = datetime.now().date()
        self.assertEqual(by_date[today.isoformat()], 2)
        self.assertEqual(by_date[(today - timedelta(days=1)).isoformat()], 2)
        self.assertEqual(by_date[(today - timedelta(days=3)).isoformat()], 1)
        self.assertEqual(by_date[(today - timedelta(days=6)).isoformat()], 1)
        self.assertEqual(by_date[(today - timedelta(days=2)).isoformat()], 0, "days without activity are there, at zero")

    def test_anything_older_than_the_chart_is_left_out(self):
        self.codex_log("a.jsonl", [local(7, 12), local(20, 12), ago(hours=1)])
        stats = s.codex_stats(time.time(), self.root)
        self.assertEqual(sum(d["requests"] for d in stats["days"]), 1)

    def test_no_activity_at_all_is_zeroes_not_nothing(self):
        stats = s.codex_stats(time.time(), self.root)
        self.assertEqual(stats["24h"], {"requests": 0, "sessions": 0})
        self.assertEqual([d["requests"] for d in stats["days"]], [0] * 7)

    def test_stats_lookup_has_both_providers(self):
        self.assertEqual(set(s.STATS), {"claude", "codex"})


class CacheTests(LogFixture):
    def test_unchanged_logs_are_not_read_twice_but_grown_ones_are(self):
        self.codex_log("a.jsonl", [ago(hours=1)])
        with patch.object(s, "_codex_events", wraps=s._codex_events) as read:
            s.collect(self.root, read, time.time())
            s.collect(self.root, read, time.time())
            self.assertEqual(read.call_count, 1, "second pass served from the cache")
            self.codex_log("a.jsonl", [ago(hours=1), ago(minutes=5)])
            self.assertEqual(len(s.collect(self.root, read, time.time())), 2)
            self.assertEqual(read.call_count, 2)

    def test_logs_untouched_since_before_the_chart_are_skipped_without_reading(self):
        self.codex_log("old.jsonl", [ago(hours=1)], mtime=time.time() - 30 * 86400)
        with patch.object(s, "_codex_events") as read:
            self.assertEqual(s.collect(self.root, read, time.time()), [])
        read.assert_not_called()

    def test_an_unreadable_log_is_skipped(self):
        self.codex_log("a.jsonl", [ago(hours=1)])
        with patch.object(s, "_codex_events", side_effect=OSError("locked")):
            self.assertEqual(s.collect(self.root, s._codex_events, time.time()), [])


class TextTests(unittest.TestCase):
    def test_counts_text_with_singulars_and_separators(self):
        self.assertEqual(s.counts_text("24h", {"requests": 375, "sessions": 4}), "24h · 375 requests · 4 sessions")
        self.assertEqual(s.counts_text("7d", {"requests": 1560, "sessions": 13}), "7d · 1,560 requests · 13 sessions")
        self.assertEqual(s.counts_text("24h", {"requests": 1, "sessions": 1}), "24h · 1 request · 1 session")

    def test_weekday_names_are_english_whatever_the_system_language(self):
        self.assertEqual([s.weekday(f"2026-10-{d:02d}") for d in range(5, 12)], ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"])


class CodexResetsTests(unittest.TestCase):
    def extra(self, count=3, days=12, status="available"):
        return g._codex_extra({"_reset_credits": {"availableCount": count, "credits": [
            {"status": status, "expiresAt": time.time() + days * 86400},
            {"status": "available", "expiresAt": time.time() + (days + 30) * 86400},
        ]}})

    def test_free_resets_and_when_the_next_one_expires(self):
        extra = self.extra()
        self.assertEqual((extra["free_resets"], extra["next_reset_expires_days"]), (3, 12))

    def test_used_ones_do_not_count_towards_the_expiry(self):
        self.assertEqual(self.extra(days=1, status="used")["next_reset_expires_days"], 31)

    def test_missing_fields_are_fine(self):
        self.assertEqual(g._codex_extra({}), {"free_resets": 0, "next_reset_expires_days": None})
        self.assertEqual(g._codex_extra({"_reset_credits": "garbage"}), {"free_resets": 0, "next_reset_expires_days": None})

    def chip(self, **codex_extra):
        return g._resets_chip({"title": "Codex", "codex_extra": codex_extra})

    def test_chip_text_and_urgency(self):
        self.assertEqual(self.chip(free_resets=3, next_reset_expires_days=12), ("3 resets · 12d", g.THEMES["Codex"]["weekly"]))
        self.assertEqual(self.chip(free_resets=1, next_reset_expires_days=3), ("1 reset · 3d", g.STATE_COLORS["warn"]))
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=1), ("2 resets · 1d", g.STATE_COLORS["crit"]))
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=0)[1], g.STATE_COLORS["crit"])
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=None)[0], "2 resets")

    def test_no_chip_without_resets_or_for_claude(self):
        self.assertIsNone(self.chip(free_resets=0, next_reset_expires_days=None))
        self.assertIsNone(g._resets_chip({"title": "Codex"}))
        self.assertIsNone(g._resets_chip({"title": "Claude", "codex_extra": {"free_resets": 3}}))


if __name__ == "__main__":
    unittest.main()
