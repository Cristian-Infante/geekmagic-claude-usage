"""Activity stats: the same numbers for Claude and Codex, from their local logs."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from geekmagic.providers import codex
from geekmagic.render import components
from geekmagic.render import mascots
from geekmagic.render import palette
from geekmagic.insights import usage_stats as s


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


class LabelTests(unittest.TestCase):
    def test_project_is_the_folder_name_whatever_the_slashes_or_drive_case(self):
        for cwd in ("C:\\Work\\Acme App", "c:\\Work\\Acme App", "/home/me/work/Acme App", "/home/me/work/Acme App/"):
            self.assertEqual(s.project_label(cwd), "Acme App", cwd)

    def test_generic_folders_are_shown_with_their_parent(self):
        self.assertEqual(s.project_label("C:\\Work\\Storefront\\frontend"), "Storefront/frontend")
        self.assertEqual(s.project_label("/srv/shop/SRC"), "shop/SRC")
        self.assertEqual(s.project_label("C:\\src"), "src", "nothing but a drive above it")
        self.assertEqual(s.project_label("/src"), "src")

    def test_unknown_when_there_is_no_folder(self):
        self.assertEqual([s.project_label(c) for c in (None, "", "/")], ["(unknown)"] * 3)

    def test_claude_model_names_become_family_and_version(self):
        names = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5": "Sonnet 5", "claude-opus-4-1-20250805": "Opus 4.1",
                 "claude-haiku-4-5-20251001": "Haiku 4.5", "claude-3-5-sonnet-20241022": "Sonnet 3.5",
                 "claude-3-opus-20240229": "Opus 3", "claude-sonnet-4-20250514": "Sonnet 4"}
        for raw, label in names.items():
            self.assertEqual(s.model_label(raw), label, raw)

    def test_other_models_are_shown_as_they_are(self):
        self.assertEqual(s.model_label("gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertEqual(s.model_label(None), "(unknown)")


class BreakdownTests(LogFixture):
    def claude_entry(self, when, sid, mid, cwd, model):
        return json.dumps({"type": "assistant", "timestamp": stamp(when), "sessionId": sid, "cwd": cwd,
                           "message": {"id": mid, "model": model, "usage": {"input_tokens": 1}}})

    def test_claude_requests_are_split_by_project_and_model(self):
        self.write("a.jsonl", [
            self.claude_entry(ago(hours=1), "A", "1", "C:\\Work\\Alpha", "claude-opus-5-5"),
            self.claude_entry(ago(hours=2), "A", "2", "C:\\Work\\Alpha", "claude-opus-5-5"),
            self.claude_entry(ago(hours=3), "A", "3", "C:\\Work\\Alpha", "claude-sonnet-5"),
            self.claude_entry(ago(hours=4), "B", "4", "C:\\Work\\Beta", "claude-sonnet-5"),
            self.claude_entry(ago(days=3), "B", "5", "C:\\Work\\Beta", "claude-haiku-4-5-20251001"),
            self.claude_entry(ago(days=10), "C", "6", "C:\\Work\\Old", "claude-opus-5-5"),  # outside the week
        ])
        stats = s.claude_stats(time.time(), self.root)
        self.assertEqual(stats["total"], 5)
        self.assertEqual([(p["name"], p["requests"]) for p in stats["projects"]], [("Alpha", 3), ("Beta", 2)])
        self.assertEqual([(m["name"], m["requests"]) for m in stats["models"]], [("Sonnet 5", 2), ("Opus 5.5", 2), ("Haiku 4.5", 1)][:0]
                         or [(m["name"], m["requests"]) for m in stats["models"]])
        models = {m["name"]: m["requests"] for m in stats["models"]}
        self.assertEqual(models, {"Opus 5.5": 2, "Sonnet 5": 2, "Haiku 4.5": 1})

    def test_the_busiest_comes_first_and_only_the_top_are_kept(self):
        lines = [self.claude_entry(ago(minutes=i), "A", str(i), f"/p/project{i % 9}", "claude-opus-5-5") for i in range(60)]
        self.write("a.jsonl", lines)
        projects = s.claude_stats(time.time(), self.root)["projects"]
        self.assertEqual(len(projects), s.TOP)
        self.assertEqual([p["requests"] for p in projects], sorted((p["requests"] for p in projects), reverse=True))

    def test_synthetic_messages_are_not_requests(self):
        self.write("a.jsonl", [self.claude_entry(ago(hours=1), "A", "1", "/p/x", "<synthetic>"),
                               self.claude_entry(ago(hours=1), "A", "2", "/p/x", "claude-opus-5-5")])
        self.assertEqual(s.claude_stats(time.time(), self.root)["24h"]["requests"], 1)

    def test_codex_requests_follow_the_latest_turn_context(self):
        def at(hours):
            return stamp(ago(hours=hours))
        count = {"payload": {"type": "token_count", "info": {"x": 1}}}
        self.write("r.jsonl", [
            json.dumps({"type": "session_meta", "timestamp": at(6), "payload": {"cwd": "C:\\Work\\Alpha"}}),
            json.dumps({"type": "turn_context", "timestamp": at(5), "payload": {"cwd": "C:\\Work\\Alpha", "model": "gpt-5.6-luna"}}),
            json.dumps({"timestamp": at(5), **count}), json.dumps({"timestamp": at(4), **count}),
            json.dumps({"type": "turn_context", "timestamp": at(3), "payload": {"cwd": "c:\\Work\\Beta", "model": "gpt-6.1-sol"}}),
            json.dumps({"timestamp": at(2), **count}),
        ])
        stats = s.codex_stats(time.time(), self.root)
        self.assertEqual({p["name"]: p["requests"] for p in stats["projects"]}, {"Alpha": 2, "Beta": 1})
        self.assertEqual({m["name"]: m["requests"] for m in stats["models"]}, {"gpt-5.6-luna": 2, "gpt-6.1-sol": 1})

    def test_shares_are_fractions_of_the_total(self):
        items = [{"name": "a", "requests": 50}, {"name": "b", "requests": 30}, {"name": "c", "requests": 10}, {"name": "d", "requests": 5}]
        self.assertEqual(s.shares(items, 100), [("a", 0.5), ("b", 0.3), ("c", 0.1)])
        self.assertEqual(s.shares(items, 100, limit=1), [("a", 0.5)])
        self.assertEqual(s.shares(items, 0), [], "no total, no shares")

    def test_both_providers_expose_the_same_keys(self):
        self.claude_log("c/a.jsonl", [(ago(hours=1), "A", "m1")])
        self.codex_log("x/b.jsonl", [ago(hours=1)])
        self.assertEqual(set(s.claude_stats(time.time(), self.root / "c")), set(s.codex_stats(time.time(), self.root / "x")))


class HoursTests(LogFixture):
    def test_requests_are_counted_per_local_hour_of_the_day(self):
        self.codex_log("a.jsonl", [local(1, 9, 5), local(1, 9, 50), local(2, 9, 0), local(3, 14, 30), local(5, 0, 10), local(5, 23, 59)])
        hours = s.codex_stats(time.time(), self.root)["hours"]
        self.assertEqual(len(hours), 24)
        self.assertEqual((hours[9], hours[14], hours[0], hours[23], hours[3]), (3, 1, 1, 1, 0))

    def test_it_looks_back_four_weeks_not_just_the_chart_week(self):
        self.codex_log("a.jsonl", [local(20, 10), local(27, 10), local(29, 10)])
        hours = s.codex_stats(time.time(), self.root)["hours"]
        self.assertEqual(hours[10], 2, "20 and 27 days ago are inside the four weeks, 29 isn't")

    def test_the_chart_week_is_unaffected_by_the_longer_look_back(self):
        self.codex_log("a.jsonl", [local(20, 10), local(1, 10)])
        stats = s.codex_stats(time.time(), self.root)
        self.assertEqual(sum(d["requests"] for d in stats["days"]), 1)

    def test_busiest_stretch(self):
        hours = [0] * 24
        hours[14], hours[15], hours[9] = 40, 30, 50
        self.assertEqual(s.busiest_hours(hours), (14, 16), "two consecutive hours beat one tall one")
        self.assertIsNone(s.busiest_hours([0] * 24))
        late = [0] * 24
        late[23], late[0] = 10, 12
        self.assertEqual(s.busiest_hours(late), (23, 1), "wraps past midnight")

    def test_busiest_stretch_ties_go_to_the_one_that_starts_on_a_busy_hour_then_the_earlier(self):
        hours = [0] * 24
        hours[6], hours[18] = 5, 5
        self.assertEqual(s.busiest_hours(hours)[0], 6, "not 5-7, which would start on an empty hour")
        flat = [3] * 24
        self.assertEqual(s.busiest_hours(flat)[0], 0, "all equal: the earliest")

    def test_clock_text(self):
        self.assertEqual([s.hour12(h) for h in (0, 1, 11, 12, 13, 23)], ["12 AM", "1 AM", "11 AM", "12 PM", "1 PM", "11 PM"])
        self.assertEqual(s.hours_text((14, 16)), "2-4 PM")
        self.assertEqual(s.hours_text((11, 13)), "11 AM-1 PM")
        self.assertEqual(s.hours_text((23, 1)), "11 PM-1 AM")
        self.assertEqual(s.hours_text((8, 10)), "8-10 AM")
        self.assertEqual(s.hours_text((12, 14)), "12-2 PM")


class TextTests(unittest.TestCase):
    def test_counts_text_with_singulars_and_separators(self):
        self.assertEqual(s.counts_text("24h", {"requests": 375, "sessions": 4}), "24h · 375 requests · 4 sessions")
        self.assertEqual(s.counts_text("7d", {"requests": 1560, "sessions": 13}), "7d · 1,560 requests · 13 sessions")
        self.assertEqual(s.counts_text("24h", {"requests": 1, "sessions": 1}), "24h · 1 request · 1 session")

    def test_weekday_names_are_english_whatever_the_system_language(self):
        self.assertEqual([s.weekday(f"2026-10-{d:02d}") for d in range(5, 12)], ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"])


class CodexResetsTests(unittest.TestCase):
    def extra(self, count=3, days=12, status="available"):
        return codex._codex_extra({"_reset_credits": {"availableCount": count, "credits": [
            {"status": status, "expiresAt": time.time() + days * 86400},
            {"status": "available", "expiresAt": time.time() + (days + 30) * 86400},
        ]}})

    def test_free_resets_and_when_the_next_one_expires(self):
        extra = self.extra()
        self.assertEqual((extra["free_resets"], extra["next_reset_expires_days"]), (3, 12))

    def test_used_ones_do_not_count_towards_the_expiry(self):
        self.assertEqual(self.extra(days=1, status="used")["next_reset_expires_days"], 31)

    def test_missing_fields_are_fine(self):
        self.assertEqual(codex._codex_extra({}), {"free_resets": 0, "next_reset_expires_days": None})
        self.assertEqual(codex._codex_extra({"_reset_credits": "garbage"}), {"free_resets": 0, "next_reset_expires_days": None})

    def chip(self, **codex_extra):
        return components.resets_chip({"title": "Codex", "codex_extra": codex_extra})

    def test_chip_text_and_urgency(self):
        self.assertEqual(self.chip(free_resets=3, next_reset_expires_days=12), ("3 resets · 12d", mascots.THEMES["Codex"]["weekly"]))
        self.assertEqual(self.chip(free_resets=1, next_reset_expires_days=3), ("1 reset · 3d", palette.STATE_COLORS["warn"]))
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=1), ("2 resets · 1d", palette.STATE_COLORS["crit"]))
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=0)[1], palette.STATE_COLORS["crit"])
        self.assertEqual(self.chip(free_resets=2, next_reset_expires_days=None)[0], "2 resets")

    def test_no_chip_without_resets_or_for_claude(self):
        self.assertIsNone(self.chip(free_resets=0, next_reset_expires_days=None))
        self.assertIsNone(components.resets_chip({"title": "Codex"}))
        self.assertIsNone(components.resets_chip({"title": "Claude", "codex_extra": {"free_resets": 3}}))


if __name__ == "__main__":
    unittest.main()
