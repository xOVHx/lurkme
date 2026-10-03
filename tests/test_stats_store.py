"""Tests for stats_store.py — run with `python -m unittest discover -s tests -v` from the repo root."""

from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stats_store  # noqa: E402
from stats_store import StatsStore, utc_day, utc_day_start  # noqa: E402

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

def at(iso: str) -> float:
    """Unix time of a naive ISO timestamp read as UTC."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()

NOON     = at("2026-10-03T12:00:00")
MIDNIGHT = at("2026-10-03T00:00:00")
EMPTY    = {"gifts": 0, "drops": 0, "subs_dropped": 0, "lurk_seconds": 0.0, "top_channels": []}

class Logs(list):
    """A log callable that remembers every line."""
    def __call__(self, msg: str):
        self.append(msg)

class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir  = Path(tmp.name)
        self.logs = Logs()

    def open(self, path=None, logs=None) -> StatsStore:
        store = StatsStore(path, log=self.logs if logs is None else logs)
        self.addCleanup(store.close)  # Runs before the temp dir is removed
        return store

    def path(self, name: str = "stats.db") -> str:
        return str(self.dir / name)

    def raw(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def sql(self, path: str, *statements: str):
        """Change a closed stats file behind the store's back."""
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            for stmt in statements:
                conn.execute(stmt)
        finally:
            conn.close()

    def gift(self, store: StatsStore, ts: float = NOON, channel: str = "streamer", **kw) -> int | None:
        args = {"gifter": "Alice", "plan": "1000", "months": 1} | kw
        return store.record_gift(ts=ts, channel=channel, **args)

    def assertJSON(self, value):
        self.assertEqual(json.loads(json.dumps(value, allow_nan=False)), value)

# ── Helpers ───────────────────────────────────────────────────────────────────

class HelperTests(unittest.TestCase):
    def test_utc_day(self):
        self.assertEqual(utc_day(NOON), "2026-10-03")
        self.assertEqual(utc_day(MIDNIGHT), "2026-10-03")
        self.assertEqual(utc_day(MIDNIGHT - 0.001), "2026-10-02")
        self.assertEqual(utc_day(0), "1970-01-01")
        self.assertEqual(utc_day(-1), "1969-12-31")  # Works on every OS, unlike fromtimestamp

    def test_utc_day_start(self):
        self.assertEqual(utc_day_start(NOON), MIDNIGHT)
        self.assertEqual(utc_day_start(MIDNIGHT), MIDNIGHT)
        self.assertEqual(utc_day_start(MIDNIGHT - 1), MIDNIGHT - 86400)

    def test_bad_timestamps_raise_value_error(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                utc_day(bad)

# ── In-memory basics ─────────────────────────────────────────────────────────

class InMemoryTests(Base):
    def test_memory_paths(self):
        for path in (None, ":memory:", ""):
            store = self.open(path)
            self.assertFalse(store.persistent)
            self.assertEqual(store.path, ":memory:")
            self.assertEqual(self.gift(store), 1)
        self.assertEqual(self.logs, [])

    def test_stores_are_independent(self):
        a, b = self.open(), self.open()
        self.gift(a)
        self.assertEqual(b.summary()["gifts"], 0)

    def test_empty_store(self):
        store = self.open()
        self.assertEqual(store.summary(), EMPTY)
        self.assertEqual(store.summary(since=NOON), EMPTY)
        self.assertEqual(store.recent_gifts(), [])
        self.assertEqual(store.lurk_today(NOON), {})
        self.assertIsNone(store.last_error)

    def test_context_manager_closes(self):
        with StatsStore(None, log=self.logs) as store:
            self.assertEqual(self.gift(store), 1)
        self.assertIsNone(self.gift(store))

# ── Gifts ─────────────────────────────────────────────────────────────────────

class GiftTests(Base):
    def test_record_gift_returns_lifetime_count(self):
        store = self.open()
        self.assertEqual([self.gift(store, ts=NOON + i) for i in range(5)], [1, 2, 3, 4, 5])

    def test_recent_gifts_round_trip_newest_first(self):
        store = self.open()
        self.gift(store, ts=NOON, channel="first", gifter="Alice", plan="1000", months=1)
        self.gift(store, ts=NOON + 60, channel="second", gifter="An anonymous gifter", plan="Prime", months=3)
        self.gift(store, ts=NOON - 60, channel="oldest", gifter="Bob", plan="3000", months=12)
        gifts = store.recent_gifts()
        self.assertEqual(gifts, [
            {"ts": NOON + 60, "channel": "second", "gifter": "An anonymous gifter", "plan": "Prime", "months": 3},
            {"ts": NOON, "channel": "first", "gifter": "Alice", "plan": "1000", "months": 1},
            {"ts": NOON - 60, "channel": "oldest", "gifter": "Bob", "plan": "3000", "months": 12},
        ])
        self.assertIsInstance(gifts[0]["ts"], float)
        self.assertIsInstance(gifts[0]["months"], int)
        self.assertJSON(gifts)

    def test_same_timestamp_newest_insert_first(self):
        store = self.open()
        for name in ("a", "b", "c"):
            self.gift(store, channel=name)
        self.assertEqual([g["channel"] for g in store.recent_gifts()], ["c", "b", "a"])

    def test_recent_gifts_limit_bounds(self):
        store = self.open()
        for i in range(30):
            self.gift(store, ts=NOON + i)
        self.assertEqual(len(store.recent_gifts()), 20)
        self.assertEqual(len(store.recent_gifts(3)), 3)
        self.assertEqual(len(store.recent_gifts(0)), 1)
        self.assertEqual(len(store.recent_gifts(-5)), 1)
        self.assertEqual(len(store.recent_gifts(10**12)), 30)
        self.assertEqual(len(store.recent_gifts(None)), 20)
        self.assertEqual(len(store.recent_gifts("junk")), 20)
        self.assertEqual(len(store.recent_gifts(math.inf)), 20)

    def test_recent_gifts_capped_at_500(self):
        store = self.open()
        for i in range(510):
            self.gift(store, ts=NOON + i)
        self.assertEqual(len(store.recent_gifts(10_000)), 500)

    def test_values_are_normalised(self):
        store = self.open()
        self.gift(store, channel="#SomeStreamer ", months="3")
        self.gift(store, channel="other", months=0)
        self.gift(store, channel="other", months="junk")
        gifts = store.recent_gifts()
        self.assertEqual(gifts[2]["channel"], "somestreamer")
        self.assertEqual([g["months"] for g in gifts], [1, 1, 3])

    def test_huge_months_are_capped_not_lost(self):
        # A malformed IRC tag mustn't cost the gift: SQLite can't store integers of 2**63 and up
        store = self.open()
        for n, months in enumerate((10**20, 2**63, 1e300, "9" * 40, stats_store.MAX_MONTHS + 1), start=1):
            self.assertEqual(self.gift(store, months=months), n)
        self.assertEqual({g["months"] for g in store.recent_gifts()}, {stats_store.MAX_MONTHS})
        self.assertIsNone(store.last_error)

    def test_bad_timestamp_saves_nothing(self):
        store = self.open()
        for bad in (None, "junk", math.nan, math.inf):
            self.assertIsNone(self.gift(store, ts=bad))
        self.assertEqual(store.summary()["gifts"], 0)
        self.assertEqual(self.gift(store), 1)

# ── Drops and summaries ──────────────────────────────────────────────────────

class SummaryTests(Base):
    def drops(self, store: StatsStore, ts: float = NOON):
        # Expected order: b (10 subs, 2 drops), a and d (10 subs, 1 drop — by name), c (5 subs), e (1 sub)
        for channel, counts in (("d", [10]), ("c", [1] * 5), ("a", [10]), ("e", [1]), ("b", [5, 5])):
            for count in counts:
                store.record_drop(ts=ts, channel=channel, kind="single" if count == 1 else "community", count=count)

    def test_top_channels_ordering(self):
        store = self.open()
        self.drops(store)
        expected = [
            {"channel": "b", "drops": 2, "subs": 10},
            {"channel": "a", "drops": 1, "subs": 10},
            {"channel": "d", "drops": 1, "subs": 10},
            {"channel": "c", "drops": 5, "subs": 5},
            {"channel": "e", "drops": 1, "subs": 1},
        ]
        for since in (None, NOON, 0):  # All-time totals and windowed scans must agree
            with self.subTest(since=since):
                s = store.summary(since=since, limit=10)
                self.assertEqual(s["top_channels"], expected)
                self.assertEqual((s["drops"], s["subs_dropped"]), (10, 36))

    def test_top_channels_limit(self):
        store = self.open()
        self.drops(store)
        for since in (None, NOON):
            self.assertEqual([t["channel"] for t in store.summary(since)["top_channels"]], ["b", "a", "d", "c", "e"])
            self.assertEqual([t["channel"] for t in store.summary(since, limit=2)["top_channels"]], ["b", "a"])
            self.assertEqual(len(store.summary(since, limit=0)["top_channels"]), 1)
            self.assertEqual(len(store.summary(since, limit="junk")["top_channels"]), 5)
            self.assertEqual(len(store.summary(since, limit=10**9)["top_channels"]), 5)

    def test_windows(self):
        store     = self.open()
        yesterday = at("2026-10-02T23:00:00")
        early     = at("2026-10-03T01:00:00")
        late      = at("2026-10-03T15:00:00")
        self.gift(store, ts=yesterday)
        self.gift(store, ts=early)
        store.record_drop(ts=yesterday, channel="a", kind="community", count=10)
        store.record_drop(ts=early, channel="b", kind="community", count=3)
        store.record_drop(ts=late, channel="b", kind="single", count=1)
        store.add_presence({"a": 100}, yesterday)
        store.add_presence({"a": 50, "b": 25}, early)

        self.assertEqual(store.summary(), {
            "gifts": 2, "drops": 3, "subs_dropped": 14, "lurk_seconds": 175.0,
            "top_channels": [{"channel": "a", "drops": 1, "subs": 10}, {"channel": "b", "drops": 2, "subs": 4}],
        })
        self.assertEqual(store.summary(since=MIDNIGHT), {
            "gifts": 1, "drops": 2, "subs_dropped": 4, "lurk_seconds": 75.0,
            "top_channels": [{"channel": "b", "drops": 2, "subs": 4}],
        })
        # since is inclusive
        self.assertEqual(store.summary(since=early)["gifts"], 1)
        after_early = store.summary(since=early + 0.5)
        self.assertEqual((after_early["gifts"], after_early["drops"], after_early["subs_dropped"]), (0, 1, 1))
        # Presence is kept per UTC day: a mid-day since still counts that whole day
        self.assertEqual(after_early["lurk_seconds"], 75.0)
        self.assertEqual(store.summary(since=yesterday + 1)["lurk_seconds"], 175.0)
        self.assertEqual(store.summary(since=at("2030-01-01T00:00:00")), EMPTY)
        self.assertEqual(store.summary(since=0)["drops"], 3)
        self.assertEqual(store.summary(since=-1e9)["drops"], 3)

    def test_windowed_summary_uses_indexes(self):
        # A full table scan here would make "today" cost seconds once a year of drops piles up
        store = self.open()
        self.drops(store)
        executed: list[str] = []
        store._conn.set_trace_callback(executed.append)
        store.summary(since=MIDNIGHT)
        store._conn.set_trace_callback(None)
        selects = [sql for sql in executed if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 4)
        for sql in selects:
            plan = [row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + sql)]
            self.assertFalse([step for step in plan if step.startswith("SCAN")], (sql, plan))

    def test_summary_types_and_json(self):
        store = self.open()
        self.drops(store)
        self.gift(store)
        store.add_presence({"a": 1.5}, NOON)
        for s in (store.summary(), store.summary(since=MIDNIGHT)):
            self.assertIsInstance(s["gifts"], int)
            self.assertIsInstance(s["drops"], int)
            self.assertIsInstance(s["subs_dropped"], int)
            self.assertIsInstance(s["lurk_seconds"], float)
            self.assertJSON(s)

    def test_drop_values_are_normalised(self):
        store = self.open()
        store.record_drop(ts=NOON, channel="#Big", kind="community", count="7")
        store.record_drop(ts=NOON, channel="big", kind="single", count=None)  # Unparseable -> 1
        store.record_drop(ts=NOON, channel="big", kind="single", count=-4)    # Never negative
        s = store.summary()
        self.assertEqual(s["top_channels"], [{"channel": "big", "drops": 3, "subs": 8}])
        self.assertEqual(store.summary(since=NOON)["top_channels"], s["top_channels"])

    def test_huge_drop_counts_are_capped(self):
        # Uncapped, two of these overflowed SUM(): windowed summaries came back empty and the
        # file could never be opened again (the store silently ran in memory on every restart)
        path  = self.path()
        store = self.open(path)
        store.record_drop(ts=NOON, channel="a", kind="community", count=5)
        for count in (2**62, 2**62, 10**30, 1e300):
            store.record_drop(ts=NOON, channel="a", kind="community", count=count)
        subs     = 5 + 4 * stats_store.MAX_DROP_COUNT
        expected = {"gifts": 0, "drops": 5, "subs_dropped": subs, "lurk_seconds": 0.0,
                    "top_channels": [{"channel": "a", "drops": 5, "subs": subs}]}
        self.assertEqual(store.summary(), expected)
        self.assertEqual(store.summary(since=0), expected)
        self.assertIsNone(store.last_error)
        store.close()

        again = self.open(path)
        self.assertTrue(again.persistent)
        self.assertEqual(again.summary(since=0), expected)
        self.assertEqual(again.summary(), expected)
        self.assertEqual(self.logs, [])

    def test_bad_drop_timestamp_saves_nothing(self):
        store = self.open()
        store.record_drop(ts=math.nan, channel="a", kind="single", count=1)
        store.record_drop(ts=None, channel="a", kind="single", count=1)
        self.assertEqual(store.summary(), EMPTY)

    def test_running_totals_match_raw_drops(self):
        rng   = random.Random(1234)
        store = self.open()
        expected: dict[str, list[int]] = {}
        for _ in range(600):
            channel = f"ch{rng.randrange(40)}"
            count   = rng.choice([1, 1, 1, 5, 10, 20, 50, 100])
            store.record_drop(ts=NOON + rng.uniform(-1e6, 1e6), channel=channel, kind="community", count=count)
            entry = expected.setdefault(channel, [0, 0])
            entry[0] += 1
            entry[1] += count
        want = sorted(({"channel": c, "drops": d, "subs": s} for c, (d, s) in expected.items()),
                      key=lambda t: (-t["subs"], -t["drops"], t["channel"]))
        all_time = store.summary(limit=500)
        windowed = store.summary(since=NOON - 2e6, limit=500)
        self.assertEqual(all_time["top_channels"], want)
        self.assertEqual(windowed["top_channels"], want)
        self.assertEqual(all_time["drops"], 600)
        self.assertEqual(all_time["subs_dropped"], sum(s for _, s in expected.values()))
        self.assertEqual(all_time, windowed)

# ── Presence ──────────────────────────────────────────────────────────────────

class PresenceTests(Base):
    def test_upsert_across_calls(self):
        store = self.open()
        store.add_presence({"a": 60, "b": 30}, NOON)
        store.add_presence({"a": 60.5, "c": 10}, NOON + 3600)
        self.assertEqual(store.lurk_today(NOON), {"a": 120.5, "b": 30.0, "c": 10.0})
        self.assertEqual(list(store.lurk_today(NOON)), ["a", "b", "c"])  # Most time first
        self.assertEqual(store.summary()["lurk_seconds"], 160.5)

    def test_day_split(self):
        store = self.open()
        store.add_presence({"a": 100}, MIDNIGHT - 1)
        store.add_presence({"a": 40, "b": 5}, MIDNIGHT)
        self.assertEqual(store.lurk_today(MIDNIGHT - 1), {"a": 100.0})
        self.assertEqual(store.lurk_today(NOON), {"a": 40.0, "b": 5.0})
        self.assertEqual(store.lurk_today(MIDNIGHT + 86400), {})
        self.assertEqual(store.summary(since=MIDNIGHT)["lurk_seconds"], 45.0)
        self.assertEqual(store.summary(since=MIDNIGHT - 1)["lurk_seconds"], 145.0)
        self.assertEqual(store.summary()["lurk_seconds"], 145.0)

    def test_skips_junk_values(self):
        store = self.open()
        store.add_presence({"a": 0, "b": -10, "c": math.nan, "d": math.inf, "e": "junk", "f": None, "g": "2.5",
                            "h": 10**400}, NOON)  # float(10**400) raises OverflowError; it mustn't sink the batch
        self.assertEqual(store.lurk_today(NOON), {"g": 2.5})
        store.add_presence({}, NOON)
        self.assertEqual(store.summary()["lurk_seconds"], 2.5)
        self.assertIsNone(store.last_error)

    def test_lurk_since_a_moment(self):
        # Presence is per UTC day, so a rolling window counts all of its first day; the difference
        # between two all-time readings is exact (what a "last 24 hours" digest should use)
        store = self.open()
        store.add_presence({"a": 3600}, at("2026-10-02T10:00:00"))
        before = store.summary()["lurk_seconds"]
        moment = at("2026-10-02T12:00:00")
        store.add_presence({"a": 600, "b": 60}, at("2026-10-02T20:00:00"))
        store.add_presence({"a": 900}, at("2026-10-03T09:00:00"))
        self.assertEqual(store.summary()["lurk_seconds"] - before, 1560.0)
        self.assertEqual(store.summary(since=moment)["lurk_seconds"], 5160.0)
        self.assertEqual(store.summary(since=utc_day_start(moment))["lurk_seconds"], 5160.0)

    def test_capped_at_a_day_per_day(self):
        store = self.open()
        store.add_presence({"a": 80_000}, NOON)
        store.add_presence({"a": 80_000, "b": 10**9}, NOON)
        self.assertEqual(store.lurk_today(NOON), {"a": 86400.0, "b": 86400.0})

    def test_channel_names_merge(self):
        store = self.open()
        store.add_presence({"Streamer": 10, "#streamer": 5, "streamer": 1}, NOON)
        self.assertEqual(store.lurk_today(NOON), {"streamer": 16.0})

    def test_accepts_pairs_and_rejects_garbage(self):
        store = self.open()
        store.add_presence([("a", 3)], NOON)
        self.assertEqual(store.lurk_today(NOON), {"a": 3.0})
        for args in ((None, NOON), ({"a": 1}, None), ({"a": 1}, math.nan), ({"a": 1}, 1e300), (42, NOON)):
            store.add_presence(*args)
        self.assertEqual(store.lurk_today(NOON), {"a": 3.0})
        self.assertEqual(store.lurk_today(None), {})
        self.assertEqual(store.lurk_today(math.inf), {})

    def test_one_transaction(self):
        store = self.open(self.path())
        store.add_presence({"a": 5}, NOON)
        conn = self.raw(store.path)
        conn.execute("CREATE TRIGGER fail_b BEFORE UPDATE ON presence WHEN NEW.channel = 'b'"
                     " BEGIN SELECT RAISE(ABORT, 'boom'); END")
        store.add_presence({"a": 1, "b": 2, "c": 3}, NOON)  # Fails half-way through the batch
        self.assertIn("boom", store.last_error)
        # Nothing from the failed batch is left behind, not even the zero rows from INSERT OR IGNORE
        self.assertEqual(conn.execute("SELECT channel, seconds FROM presence").fetchall(), [("a", 5.0)])

# ── Meta ──────────────────────────────────────────────────────────────────────

class MetaTests(Base):
    def test_get_set(self):
        store = self.open()
        self.assertIsNone(store.get_meta("missing"))
        self.assertEqual(store.get_meta("missing", "fallback"), "fallback")
        store.set_meta("last_online", "123.5")
        self.assertEqual(store.get_meta("last_online"), "123.5")
        store.set_meta("last_online", "456")
        self.assertEqual(store.get_meta("last_online", "fallback"), "456")
        store.set_meta("number", 7)
        self.assertEqual(store.get_meta("number"), "7")

    def test_schema_version_is_protected(self):
        store = self.open()
        self.assertEqual(store.get_meta("schema_version"), str(stats_store.SCHEMA_VERSION))
        store.set_meta("schema_version", "99")
        self.assertEqual(store.get_meta("schema_version"), str(stats_store.SCHEMA_VERSION))
        self.assertIn("schema_version", store.last_error)

# ── Files ─────────────────────────────────────────────────────────────────────

class FileTests(Base):
    def test_creates_parent_dirs_and_persists(self):
        path  = self.dir / "deep" / "er" / "stats.db"
        store = StatsStore(str(path), log=self.logs)
        self.assertTrue(store.persistent)
        self.assertEqual(store.path, str(path))
        self.gift(store, channel="a")
        store.record_drop(ts=NOON, channel="a", kind="community", count=5)
        store.add_presence({"a": 60}, NOON)
        store.set_meta("k", "v")
        before = (store.summary(), store.summary(since=MIDNIGHT), store.recent_gifts(), store.lurk_today(NOON))
        store.close()

        again = self.open(path)  # Path objects work too
        self.assertEqual((again.summary(), again.summary(since=MIDNIGHT), again.recent_gifts(),
                          again.lurk_today(NOON)), before)
        self.assertEqual(again.get_meta("k"), "v")
        self.assertEqual(self.gift(again), 2)
        self.assertEqual(self.logs, [])

    def test_expands_user(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            store = self.open("~/lurkme/stats.db")
        self.assertEqual(store.path, str(self.dir / "lurkme" / "stats.db"))
        self.assertTrue((self.dir / "lurkme" / "stats.db").exists())

    def test_schema(self):
        store = self.open(self.path())
        conn  = self.raw(store.path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"gifts", "drops", "presence", "meta"}, tables)
        columns = {t: [row[1] for row in conn.execute(f"PRAGMA table_info({t})")] for t in ("gifts", "drops", "presence", "meta")}
        self.assertEqual(columns, {
            "gifts":    ["id", "ts", "channel", "gifter", "plan", "months"],
            "drops":    ["id", "ts", "channel", "kind", "count"],
            "presence": ["day", "channel", "seconds"],
            "meta":     ["key", "value"],
        })
        indexed = {conn.execute(f"PRAGMA index_info({name})").fetchone()[2] + "@" + table
                   for name, table in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
                                                   " AND sql IS NOT NULL")}
        self.assertLessEqual({"ts@gifts", "ts@drops", "channel@drops"}, indexed)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "1")

    def test_stores_plain_values(self):
        store = self.open(self.path())
        self.gift(store, ts=int(NOON), months="2")
        store.record_drop(ts=int(NOON), channel="a", kind="community", count="5")
        store.add_presence({"a": 3}, NOON)
        conn = self.raw(store.path)
        self.assertEqual(conn.execute("SELECT typeof(ts), typeof(channel), typeof(gifter), typeof(plan), typeof(months)"
                                      " FROM gifts").fetchone(), ("real", "text", "text", "text", "integer"))
        self.assertEqual(conn.execute("SELECT typeof(ts), typeof(channel), typeof(kind), typeof(count)"
                                      " FROM drops").fetchone(), ("real", "text", "text", "integer"))
        self.assertEqual(conn.execute("SELECT day, typeof(channel), typeof(seconds) FROM presence").fetchone(),
                         ("2026-10-03", "text", "real"))

    def test_reopen_is_idempotent(self):
        path = self.path()
        for expected in (1, 2, 3):
            with StatsStore(path, log=self.logs) as store:
                self.assertEqual(self.gift(store), expected)
        self.assertEqual(self.logs, [])

# ── Falling back to memory ────────────────────────────────────────────────────

class FallbackTests(Base):
    def assertFellBack(self, store: StatsStore, logs: Logs):
        self.assertFalse(store.persistent)
        self.assertEqual(len(logs), 1, logs)
        self.assertIn("keeping stats in memory", logs[0])
        self.assertTrue(store.last_error.startswith("open: "))
        self.assertEqual(self.gift(store), 1)  # Still fully working, just not saved
        store.record_drop(ts=NOON, channel="a", kind="single", count=1)
        self.assertEqual(store.summary()["drops"], 1)
        self.assertEqual(len(logs), 1, logs)

    @unittest.skipIf(IS_ROOT, "root ignores file permissions")
    def test_unwritable_directory(self):
        locked = self.dir / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        for path in (locked / "stats.db", locked / "sub" / "stats.db"):
            with self.subTest(path=path):
                logs = Logs()
                self.assertFellBack(self.open(path, logs), logs)
                self.assertFalse(path.exists())

    @unittest.skipIf(IS_ROOT, "root ignores file permissions")
    def test_read_only_file(self):
        path = self.path()
        with StatsStore(path, log=self.logs) as store:
            self.gift(store)
        os.chmod(path, 0o444)
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        logs = Logs()
        self.assertFellBack(self.open(path, logs), logs)

    def test_parent_is_a_file(self):  # Fails even for root
        blocker = self.dir / "blocker"
        blocker.write_text("not a directory")
        logs = Logs()
        self.assertFellBack(self.open(blocker / "stats.db", logs), logs)
        self.assertEqual(blocker.read_text(), "not a directory")

    def test_path_is_a_directory(self):
        logs = Logs()
        self.assertFellBack(self.open(self.dir, logs), logs)

    def test_corrupted_file_is_left_alone(self):
        path    = Path(self.path())
        garbage = b"definitely not an SQLite database\n" * 200
        path.write_bytes(garbage)
        logs = Logs()
        self.assertFellBack(self.open(path, logs), logs)
        self.assertEqual(path.read_bytes(), garbage)

    def opens(self, path, logs: Logs) -> tuple[StatsStore, int]:
        """Open a store, counting the tries at opening path itself."""
        with mock.patch.object(StatsStore, "_open", autospec=True, side_effect=StatsStore._open) as opened:
            store = self.open(path, logs)
        return store, sum(call.args[1] == os.path.abspath(path) for call in opened.call_args_list)

    def hold_lock(self, path: str) -> sqlite3.Connection:
        """Another program (a backup, the sqlite3 shell, the last run) holding the write lock."""
        other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        return other

    def test_waits_out_a_brief_lock(self):
        path = self.path()
        with StatsStore(path, log=self.logs) as store:
            self.gift(store)
        other   = self.hold_lock(path)
        release = threading.Timer(0.5, other.execute, ("ROLLBACK",))
        self.addCleanup(release.cancel)
        with mock.patch.multiple(stats_store, BUSY_TIMEOUT=0.1, OPEN_RETRY_GAP=0.1, OPEN_ATTEMPTS=50):
            release.start()
            store, tries = self.opens(path, self.logs)
        self.assertTrue(store.persistent)
        self.assertGreater(tries, 1)
        self.assertEqual(self.logs, [])
        self.assertEqual(self.gift(store), 2)  # The lifetime count carries on from the file

    def test_gives_up_on_a_lock_that_stays(self):
        path = self.path()
        with StatsStore(path, log=self.logs) as store:
            self.gift(store)
        self.hold_lock(path)
        logs = Logs()
        with mock.patch.multiple(stats_store, BUSY_TIMEOUT=0.05, OPEN_RETRY_GAP=0.01, OPEN_ATTEMPTS=3):
            store, tries = self.opens(path, logs)
        self.assertEqual(tries, 3)
        self.assertIn("locked", logs[0])
        self.assertFellBack(store, logs)

    def test_no_retries_for_files_that_cant_work(self):
        corrupt = Path(self.path("corrupt.db"))
        corrupt.write_bytes(b"definitely not an SQLite database\n" * 200)
        with mock.patch.object(stats_store, "OPEN_RETRY_GAP", 60):  # A retry would hang the test
            for path in (corrupt, self.dir):
                with self.subTest(path=path):
                    logs = Logs()
                    store, tries = self.opens(path, logs)
                    self.assertEqual(tries, 1)
                    self.assertFellBack(store, logs)

    def test_unusable_path_type(self):
        logs = Logs()
        self.assertFellBack(self.open(12.5, logs), logs)

    def test_raising_logger(self):
        def explode(msg):
            raise RuntimeError("logger is broken")
        path = Path(self.path())
        path.write_bytes(b"junk" * 1000)
        store = self.open(path, explode)
        self.assertFalse(store.persistent)
        store.close()
        self.assertIsNone(self.gift(store))
        self.assertEqual(store.summary(), EMPTY)

# ── Errors never escape ───────────────────────────────────────────────────────

class ErrorTests(Base):
    def assertNeutral(self, store: StatsStore):
        self.assertIsNone(self.gift(store))
        self.assertIsNone(store.record_drop(ts=NOON, channel="a", kind="single", count=1))
        self.assertIsNone(store.add_presence({"a": 1}, NOON))
        self.assertEqual(store.summary(), EMPTY)
        self.assertEqual(store.summary(since=NOON, limit=3), EMPTY)
        self.assertEqual(store.recent_gifts(), [])
        self.assertEqual(store.lurk_today(NOON), {})
        self.assertIsNone(store.get_meta("k"))
        self.assertEqual(store.get_meta("k", "fallback"), "fallback")
        self.assertIsNone(store.set_meta("k", "v"))

    def test_closed_database(self):
        for path in (None, self.path()):
            with self.subTest(path=path):
                store = self.open(path)
                self.gift(store)
                store.close()
                store.close()  # Twice is fine
                self.assertNeutral(store)
                self.assertIn("closed", store.last_error)

    def test_each_error_logged_once(self):
        store = self.open()
        store.close()
        self.logs.clear()
        for _ in range(3):
            self.assertNeutral(store)
        self.assertEqual(len(self.logs), len(set(self.logs)))
        self.assertEqual(sum("record_gift" in line for line in self.logs), 1)
        self.assertTrue(all(line.startswith("[stats] ") for line in self.logs))

    def test_logging_goes_quiet_after_many_distinct_errors(self):
        store = self.open()
        for i in range(stats_store.MAX_LOGGED * 3):
            store._fail("op", ValueError(f"problem {i}"))
        self.assertEqual(len(self.logs), stats_store.MAX_LOGGED + 1)
        self.assertIn("not logging any more", self.logs[-1])
        self.assertEqual(store.last_error, f"op: ValueError: problem {stats_store.MAX_LOGGED * 3 - 1}")

    def test_neutral_values_are_fresh_objects(self):
        store = self.open()
        store.close()
        store.summary()["top_channels"].append("mutated")
        store.recent_gifts().append("mutated")
        store.lurk_today(NOON)["x"] = 1.0
        self.assertEqual(store.summary(), EMPTY)
        self.assertEqual(store.recent_gifts(), [])
        self.assertEqual(store.lurk_today(NOON), {})

    def test_locked_database_then_recovers(self):
        store = self.open(self.path())
        store._conn.execute("PRAGMA busy_timeout = 50")  # Don't wait the full 5 s in tests
        self.gift(store)
        other = self.raw(store.path)
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO meta (key, value) VALUES ('held', 'yes')")
        self.assertIsNone(self.gift(store))
        store.record_drop(ts=NOON, channel="a", kind="single", count=1)
        store.add_presence({"a": 1}, NOON)
        self.assertIn("locked", store.last_error)
        self.assertEqual(store.summary()["gifts"], 1)  # WAL readers aren't blocked by a writer
        other.execute("ROLLBACK")
        self.assertEqual(self.gift(store), 2)
        self.assertEqual(store.summary()["drops"], 0)

    def test_corrupted_while_open(self):
        store = self.open(self.path())
        self.gift(store)
        broken = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        broken.execute("CREATE TABLE unrelated (x)")  # A database without our tables
        store._conn.close()
        store._conn = broken
        self.assertNeutral(store)
        self.assertIn("no such table", store.last_error)

    def test_garbage_arguments_never_raise(self):
        store = self.open()
        calls = [
            lambda: store.record_gift(ts=object(), channel=None, gifter=None, plan=None, months=None),
            lambda: store.record_gift(ts=1e400, channel="a", gifter="b", plan="c", months=1),
            lambda: store.record_drop(ts="soon", channel="a", kind="x", count=1),
            lambda: store.record_drop(ts=NOON, channel="a", kind="community", count=2**70),
            lambda: store.add_presence("not a dict", NOON),
            lambda: store.add_presence({"a": 1}, "today"),
            lambda: store.summary(since="yesterday"),
            lambda: store.summary(since=math.nan),
            lambda: store.summary(since=1e300),
            lambda: store.recent_gifts(limit=object()),
            lambda: store.lurk_today("today"),
            lambda: store.get_meta(None),
            lambda: store.set_meta(None, None),
        ]
        for call in calls:
            self.assertJSON(call())
        self.assertEqual(store.summary(since="yesterday"), EMPTY)

# ── Schema upgrades ───────────────────────────────────────────────────────────

class MigrationTests(Base):
    def make(self) -> str:
        path = self.path()
        with StatsStore(path, log=self.logs) as store:
            self.gift(store)
            store.record_drop(ts=NOON, channel="a", kind="community", count=5)
            store.record_drop(ts=NOON, channel="b", kind="single", count=1)
            store.add_presence({"a": 10}, NOON)
        return path

    def test_newer_schema_is_used_as_is(self):
        path = self.make()
        self.sql(path, "UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        logs  = Logs()
        store = self.open(path, logs)
        self.assertTrue(store.persistent)
        self.assertEqual(store.get_meta("schema_version"), "999")  # Never downgraded
        self.assertEqual(len(logs), 1)
        self.assertIn("newer lurkme", logs[0])
        self.assertEqual(self.gift(store), 2)

    def test_garbage_schema_version(self):
        path = self.make()
        self.sql(path, "UPDATE meta SET value = 'banana' WHERE key = 'schema_version'")
        store = self.open(path)
        self.assertEqual(store.get_meta("schema_version"), "1")
        self.assertEqual(store.summary()["gifts"], 1)

    def test_tables_without_meta(self):
        path = self.path()
        self.sql(path, "CREATE TABLE gifts (id INTEGER PRIMARY KEY, ts REAL, channel TEXT, gifter TEXT, plan TEXT, months INTEGER)",
                 f"INSERT INTO gifts (ts, channel, gifter, plan, months) VALUES ({NOON}, 'a', 'b', '1000', 1)")
        store = self.open(path)
        self.assertEqual(store.get_meta("schema_version"), "1")
        self.assertEqual(self.gift(store), 2)
        self.assertEqual(self.logs, [])

    def test_running_totals_rebuilt_when_out_of_sync(self):
        path = self.make()
        self.sql(path, "DROP TABLE drop_totals",
                 f"INSERT INTO drops (ts, channel, kind, count) VALUES ({NOON}, 'c', 'community', 50)")
        store = self.open(path)
        expected = [{"channel": "c", "drops": 1, "subs": 50}, {"channel": "a", "drops": 1, "subs": 5},
                    {"channel": "b", "drops": 1, "subs": 1}]
        self.assertEqual(store.summary()["top_channels"], expected)
        self.assertEqual(store.summary(since=0)["top_channels"], expected)
        self.assertEqual(store.summary()["subs_dropped"], 56)
        self.assertEqual(self.raw(path).execute("SELECT DISTINCT typeof(drops), typeof(subs) FROM drop_totals").fetchall(),
                         [("integer", "integer")])

    def test_oversized_counts_in_the_file_dont_lock_us_out(self):
        # Rows written before counts were capped (or by hand) whose SUM() overflows 64-bit integers
        path = self.make()
        self.sql(path, *[f"INSERT INTO drops (ts, channel, kind, count) VALUES ({NOON}, 'big', 'community', {2**62})"] * 2)
        for _ in range(2):  # Rebuilding the running totals, then using them as they are
            store = self.open(path)
            self.assertTrue(store.persistent)
            for since in (None, 0, MIDNIGHT):
                with self.subTest(since=since):
                    s = store.summary(since=since)
                    self.assertEqual(s["drops"], 4)
                    self.assertEqual(s["subs_dropped"], 2**63)  # 2**63 + 6 as a float
                    self.assertEqual([t["channel"] for t in s["top_channels"]], ["big", "a", "b"])
                    self.assertJSON(s)
            store.record_drop(ts=NOON, channel="big", kind="single", count=1)  # Adding past 2**63 is fine too
            self.assertEqual(store.summary()["drops"], 5)
            self.assertIsNone(store.last_error)
            store.close()
            self.sql(path, "DELETE FROM drops WHERE kind = 'single' AND channel = 'big'")
        self.assertEqual(self.logs, [])

    def test_upgrade_runs_pending_migrations_once(self):
        path = self.make()
        migrations = {2: ("ALTER TABLE gifts ADD COLUMN note TEXT",
                          "INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated', 'yes')")}
        with mock.patch.object(stats_store, "SCHEMA_VERSION", 2), mock.patch.object(stats_store, "_MIGRATIONS", migrations):
            with StatsStore(path, log=self.logs) as store:
                self.assertEqual(store.get_meta("schema_version"), "2")
                self.assertEqual(store.get_meta("migrated"), "yes")
                self.assertEqual(store.summary()["gifts"], 1)  # Old data kept
                store.set_meta("migrated", "already")
            with StatsStore(path, log=self.logs) as store:  # Already at 2: nothing runs again
                self.assertEqual(store.get_meta("migrated"), "already")
            fresh = self.open(self.path("fresh.db"))  # New files start at the latest version
            self.assertEqual(fresh.get_meta("schema_version"), "2")
            self.assertIsNone(fresh.get_meta("migrated"))
        columns = [row[1] for row in self.raw(path).execute("PRAGMA table_info(gifts)")]
        self.assertIn("note", columns)
        self.assertEqual(self.logs, [])

    def test_failed_migration_falls_back_without_touching_the_file(self):
        path = self.make()
        with mock.patch.object(stats_store, "SCHEMA_VERSION", 2), \
             mock.patch.object(stats_store, "_MIGRATIONS", {2: ("THIS IS NOT SQL",)}):
            logs  = Logs()
            store = self.open(path, logs)
        self.assertFalse(store.persistent)
        self.assertEqual(len(logs), 1)
        self.assertEqual(self.raw(path).execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "1")

# ── Threads ───────────────────────────────────────────────────────────────────

class ConcurrencyTests(Base):
    THREADS = 8
    ROUNDS  = 40

    def hammer(self, stores: list[StatsStore]) -> tuple[list, list]:
        counts, errors = [], []
        barrier = threading.Barrier(self.THREADS)

        def worker(n: int):
            store = stores[n % len(stores)]
            try:
                barrier.wait()
                for i in range(self.ROUNDS):
                    counts.append(self.gift(store, ts=NOON + i, channel=f"ch{n}"))
                    store.record_drop(ts=NOON + i, channel=f"drop{n % 3}", kind="community", count=2)
                    store.add_presence({f"ch{n}": 1.0, "shared": 0.5}, NOON)
                    store.summary()
                    store.summary(since=MIDNIGHT)
                    store.recent_gifts(5)
                    store.lurk_today(NOON)
                    store.set_meta(f"thread{n}", str(i))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertFalse(any(t.is_alive() for t in threads))
        return counts, errors

    def check(self, store: StatsStore, counts: list, errors: list):
        total = self.THREADS * self.ROUNDS
        self.assertEqual(errors, [])
        self.assertEqual(self.logs, [])
        self.assertEqual(sorted(counts), list(range(1, total + 1)))  # Every write saw a distinct lifetime count
        s = store.summary(limit=10)
        self.assertEqual((s["gifts"], s["drops"], s["subs_dropped"]), (total, total, total * 2))
        self.assertEqual(s["lurk_seconds"], total * 1.5)
        per_drop_channel = {f"drop{k}": sum(self.ROUNDS for n in range(self.THREADS) if n % 3 == k) for k in range(3)}
        self.assertEqual({t["channel"]: t["drops"] for t in s["top_channels"]}, per_drop_channel)
        self.assertEqual(store.summary(since=MIDNIGHT, limit=10), s)
        today = store.lurk_today(NOON)
        self.assertEqual(today["shared"], total * 0.5)
        self.assertEqual({today[f"ch{n}"] for n in range(self.THREADS)}, {float(self.ROUNDS)})
        self.assertEqual(store.get_meta("thread0"), str(self.ROUNDS - 1))

    def test_eight_threads_one_file(self):
        store = self.open(self.path())
        self.check(store, *self.hammer([store]))

    def test_eight_threads_in_memory(self):
        store = self.open()
        self.check(store, *self.hammer([store]))

    def test_two_stores_sharing_a_file(self):
        path   = self.path()
        stores = [self.open(path), self.open(path)]
        self.check(stores[0], *self.hammer(stores))

    def test_close_while_writing(self):
        store = self.open(self.path())
        stop, errors = threading.Event(), []

        def worker(n: int):
            try:
                while not stop.is_set():
                    self.gift(store, channel=f"ch{n}")
                    store.add_presence({f"ch{n}": 1}, NOON)
                    store.summary()
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(self.THREADS)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        store.close()
        time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertIsNone(self.gift(store))

if __name__ == "__main__":
    unittest.main()
