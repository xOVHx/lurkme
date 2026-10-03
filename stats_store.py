"""
Persistent lurkme stats in SQLite: subs gifted to you, gift drops seen in the
chats you lurk in, and time spent in each channel per UTC day.

Stats are a nice-to-have, so nothing here ever raises. If the database file
can't be used the store keeps everything in memory for this run, and a failed
query is logged once and answered with an empty result.
"""

from __future__ import annotations

import contextlib
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 1
BUSY_TIMEOUT   = 5        # Seconds to wait for another connection's write lock
OPEN_ATTEMPTS  = 3        # Tries at opening the file while another program holds that lock
OPEN_RETRY_GAP = 1.0      # Seconds between those tries
MAX_LIMIT      = 500      # Upper bound for summary() / recent_gifts() limits
MAX_LOGGED     = 50       # Distinct error messages logged before going quiet
DAY_SECONDS    = 86400    # Nobody lurks in one channel for more than a whole day per day
MAX_DROP_COUNT = 100_000  # Subs in one gift drop (the biggest real gift bombs are a few thousand)
MAX_MONTHS     = 1200     # Months on one gifted sub (Twitch offers 1, 3, 6 or 12)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Always the latest layout. Files written by older versions are upgraded by _MIGRATIONS.
_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS gifts (id INTEGER PRIMARY KEY, ts REAL, channel TEXT, gifter TEXT, plan TEXT, months INTEGER)",
    "CREATE TABLE IF NOT EXISTS drops (id INTEGER PRIMARY KEY, ts REAL, channel TEXT, kind TEXT, count INTEGER)",
    "CREATE TABLE IF NOT EXISTS presence (day TEXT, channel TEXT, seconds REAL, PRIMARY KEY (day, channel))",
    # Running all-time totals per channel, so all-time summaries don't rescan every drop ever seen
    "CREATE TABLE IF NOT EXISTS drop_totals (channel TEXT PRIMARY KEY, drops INTEGER, subs INTEGER)",
)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS gifts_ts ON gifts (ts)",
    "CREATE INDEX IF NOT EXISTS drops_ts ON drops (ts)",
    "CREATE INDEX IF NOT EXISTS drops_channel ON drops (channel)",
)
# version -> statements that upgrade a file from version - 1. They must be idempotent
# (e.g. CREATE ... IF NOT EXISTS) and never drop data.
_MIGRATIONS: dict[int, tuple[str, ...]] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(value: Any) -> float:
    ts = float(value)
    if not math.isfinite(ts):
        raise ValueError("timestamp must be a finite number")
    return ts

def utc_day(ts: float) -> str:
    """The UTC calendar day of a Unix time, as YYYY-MM-DD."""
    return (_EPOCH + timedelta(seconds=_ts(ts))).date().isoformat()

def utc_day_start(ts: float) -> float:
    """The Unix time of 00:00 UTC on the day of ts — handy as summary(since=...) for "today"."""
    ts = _ts(ts)
    return ts - ts % DAY_SECONDS

def _int(value: Any, default: int, low: int, high: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, n if high is None else min(n, high))

def _limit(value: Any, default: int) -> int:
    return _int(value, default, 1, MAX_LIMIT)

def _seconds(value: Any) -> float:
    try:
        secs = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(secs, DAY_SECONDS) if math.isfinite(secs) and secs > 0 else 0.0

def _text(value: Any, size: int = 200) -> str:
    return str(value)[:size]

def _login(value: Any) -> str:
    """Channels are keyed by Twitch login: lowercase, no leading #."""
    return str(value).strip().lstrip("#").lower()[:64]

def _busy(error: Exception) -> bool:
    """Another connection holds a lock: worth waiting for, unlike a read-only or corrupted file."""
    return isinstance(error, sqlite3.OperationalError) and any(w in str(error).lower() for w in ("locked", "busy"))

def _empty_summary() -> dict:
    return {"gifts": 0, "drops": 0, "subs_dropped": 0, "lurk_seconds": 0.0, "top_channels": []}

@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """BEGIN ... COMMIT (the connection is in autocommit mode), rolling back if anything fails."""
    if conn.in_transaction:  # Left open by a COMMIT that failed earlier
        conn.execute("ROLLBACK")
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise

# ── Store ─────────────────────────────────────────────────────────────────────

class StatsStore:
    """Thread-safe stats database. Methods never raise: on error they log once and return an empty value."""

    def __init__(self, path: str | os.PathLike | None, log: Callable[[str], None] = print):
        self.last_error: str | None = None  # Set whenever a database operation fails
        self.path        = ":memory:"
        self._log        = log
        self._lock       = threading.Lock()
        self._logged: set[str] = set()     # Error lines already logged
        self._conn: sqlite3.Connection | None = None

        try:
            target = os.fspath(path) if path is not None else ""
            if target and target != ":memory:":
                target = os.path.abspath(os.path.expanduser(target))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                self._conn = self._open_file(target)
                self.path  = target
        except Exception as e:
            detail          = f"{type(e).__name__}: {e}"
            self.last_error = f"open: {detail}"
            self._say(f"[stats] Can't use the stats file {path} ({detail}) — keeping stats in memory until restart")
        if self._conn is None:
            try:
                self._conn = self._open(":memory:")
            except Exception as e:  # Only if SQLite itself is broken
                self._fail("open", e)

    @property
    def persistent(self) -> bool:
        """False when stats only live in memory and are lost on exit."""
        return self.path != ":memory:"

    def __enter__(self) -> StatsStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _open_file(self, target: str) -> sqlite3.Connection:
        """Open the stats file, waiting out a lock held for a while by a backup, the sqlite3 shell or the last run."""
        attempt = 1
        while True:
            try:
                return self._open(target)
            except sqlite3.OperationalError as e:
                if attempt >= OPEN_ATTEMPTS or not _busy(e):
                    raise
            attempt += 1
            time.sleep(OPEN_RETRY_GAP)

    def _open(self, target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT, check_same_thread=False, isolation_level=None)
        try:
            with contextlib.suppress(sqlite3.Error):  # Not every filesystem supports WAL
                conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _migrate(self, conn: sqlite3.Connection):
        """Create or upgrade the schema in one transaction. Never drops anything; always writes, to catch read-only files."""
        with _transaction(conn):
            existing = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for stmt in _TABLES:
                conn.execute(stmt)
            row     = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            fresh   = not existing & {"meta", "gifts", "drops", "presence"}
            version = SCHEMA_VERSION if fresh else _int(row[0] if row else 1, 1, 1)
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS.get(step, ()):
                    conn.execute(stmt)
            for stmt in _INDEXES:
                conn.execute(stmt)
            self._reconcile_totals(conn)
            if version > SCHEMA_VERSION:
                self._say(f"[stats] The stats file is from a newer lurkme (schema {version}) — using it as-is")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(max(version, SCHEMA_VERSION)),))

    @staticmethod
    def _reconcile_totals(conn: sqlite3.Connection):
        """Rebuild drop_totals from drops if they disagree (e.g. a file whose drops were written elsewhere).
        Sums use TOTAL(), which can't overflow like SUM() can: one absurd count mustn't lock us out of the file."""
        have = conn.execute("SELECT TOTAL(drops), TOTAL(subs) FROM drop_totals").fetchone()
        want = conn.execute("SELECT COUNT(*), TOTAL(count) FROM drops").fetchone()
        if tuple(have) != tuple(want):
            conn.execute("DELETE FROM drop_totals")
            conn.execute("INSERT INTO drop_totals (channel, drops, subs)"
                         " SELECT channel, COUNT(*), TOTAL(count) FROM drops GROUP BY channel")

    # ── Error handling ────────────────────────────────────────────────────────

    def _say(self, msg: str):
        with contextlib.suppress(Exception):  # A broken logger mustn't break stats, let alone the bot
            self._log(msg)

    def _fail(self, op: str, error: Exception):
        detail          = f"{type(error).__name__}: {error}"[:300]
        self.last_error = f"{op}: {detail}"
        line            = f"[stats] {op} failed, carrying on without it: {detail}"
        if line in self._logged or len(self._logged) > MAX_LOGGED:
            return
        self._logged.add(line)
        if len(self._logged) > MAX_LOGGED:
            line = "[stats] Too many database errors — not logging any more of them"
        self._say(line)

    def _run(self, op: str, neutral: Any, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run fn under the lock. Any error is logged once per distinct message and answered with neutral."""
        with self._lock:
            try:
                if self._conn is None:
                    raise sqlite3.ProgrammingError("no database connection")
                return fn(self._conn)
            except Exception as e:
                self._fail(op, e)
                return neutral

    # ── Writes ────────────────────────────────────────────────────────────────

    def record_gift(self, *, ts: float, channel: str, gifter: str, plan: str, months: int) -> int | None:
        """Save a sub gifted to you. Returns how many you've been gifted all-time, or None if it couldn't be saved."""
        def op(conn: sqlite3.Connection) -> int:
            row = (_ts(ts), _login(channel), _text(gifter), _text(plan, 16), _int(months, 1, 1, MAX_MONTHS))
            with _transaction(conn):
                conn.execute("INSERT INTO gifts (ts, channel, gifter, plan, months) VALUES (?, ?, ?, ?, ?)", row)
                return conn.execute("SELECT COUNT(*) FROM gifts").fetchone()[0]
        return self._run("record_gift", None, op)

    def record_drop(self, *, ts: float, channel: str, kind: str, count: int) -> None:
        """Save a gift event seen in a joined chat: kind "community" (a gift bomb of count subs) or "single"."""
        def op(conn: sqlite3.Connection):
            login, subs = _login(channel), _int(count, 1, 0, MAX_DROP_COUNT)
            row         = (_ts(ts), login, _text(kind, 16), subs)
            with _transaction(conn):
                conn.execute("INSERT INTO drops (ts, channel, kind, count) VALUES (?, ?, ?, ?)", row)
                conn.execute("INSERT OR IGNORE INTO drop_totals (channel, drops, subs) VALUES (?, 0, 0)", (login,))
                conn.execute("UPDATE drop_totals SET drops = drops + 1, subs = subs + ? WHERE channel = ?", (subs, login))
        self._run("record_drop", None, op)

    def add_presence(self, seconds_by_channel: dict[str, float], ts: float) -> None:
        """Add time spent in each channel to the UTC day of ts, in one transaction (capped at a day per day)."""
        def op(conn: sqlite3.Connection):
            day   = utc_day(ts)
            added = [(_login(channel), _seconds(value)) for channel, value in dict(seconds_by_channel).items()]
            added = [(login, secs) for login, secs in added if secs]  # Skips zero, negative and junk values
            if not added:
                return
            with _transaction(conn):
                conn.executemany("INSERT OR IGNORE INTO presence (day, channel, seconds) VALUES (?, ?, 0)",
                                 [(day, login) for login, _ in added])
                conn.executemany("UPDATE presence SET seconds = MIN(seconds + ?, ?) WHERE day = ? AND channel = ?",
                                 [(secs, DAY_SECONDS, day, login) for login, secs in added])
        self._run("add_presence", None, op)

    def set_meta(self, key: str, value: str) -> None:
        def op(conn: sqlite3.Connection):
            if str(key) == "schema_version":
                raise ValueError("schema_version is managed by StatsStore")
            with _transaction(conn):
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (str(key), str(value)))
        self._run("set_meta", None, op)

    # ── Reads ─────────────────────────────────────────────────────────────────

    def summary(self, since: float | None = None, limit: int = 5) -> dict:
        """Gifts, drops, lurk time and top drop channels since a Unix time (None = all time).
        Lurk time is kept per UTC day, so it counts the whole UTC day of since onwards: exact for a since from
        utc_day_start(), up to a day too much for a rolling "last 24 hours" (diff two all-time readings for that)."""
        def op(conn: sqlite3.Connection) -> dict:
            top_n = _limit(limit, 5)
            with _transaction(conn, "DEFERRED"):  # One consistent snapshot for all the numbers
                if since is None:
                    gifts       = conn.execute("SELECT COUNT(*) FROM gifts").fetchone()[0]
                    drops, subs = conn.execute("SELECT TOTAL(drops), TOTAL(subs) FROM drop_totals").fetchone()
                    lurk        = conn.execute("SELECT TOTAL(seconds) FROM presence").fetchone()[0]
                    top         = conn.execute(
                        "SELECT channel, drops, subs FROM drop_totals WHERE drops > 0"
                        " ORDER BY subs DESC, drops DESC, channel LIMIT ?", (top_n,)).fetchall()
                else:
                    start       = _ts(since)
                    gifts       = conn.execute("SELECT COUNT(*) FROM gifts WHERE ts >= ?", (start,)).fetchone()[0]
                    drops, subs = conn.execute(
                        "SELECT COUNT(*), TOTAL(count) FROM drops WHERE ts >= ?", (start,)).fetchone()
                    lurk        = conn.execute(
                        "SELECT TOTAL(seconds) FROM presence WHERE day >= ?", (utc_day(start),)).fetchone()[0]
                    # "+channel" stops SQLite walking the whole table in channel order instead of using drops_ts
                    top         = conn.execute(
                        "SELECT channel, COUNT(*) AS n, TOTAL(count) AS s FROM drops WHERE ts >= ?"
                        " GROUP BY +channel ORDER BY s DESC, n DESC, channel LIMIT ?", (start, top_n)).fetchall()
            # TOTAL() is a float that never overflows; the numbers are still whole
            return {
                "gifts":        _int(gifts, 0, 0),
                "drops":        _int(drops, 0, 0),
                "subs_dropped": _int(subs, 0, 0),
                "lurk_seconds": float(lurk),
                "top_channels": [{"channel": ch, "drops": _int(n, 0, 0), "subs": _int(s, 0, 0)} for ch, n, s in top],
            }
        return self._run("summary", _empty_summary(), op)

    def recent_gifts(self, limit: int = 20) -> list[dict]:
        """Subs gifted to you, newest first."""
        def op(conn: sqlite3.Connection) -> list[dict]:
            rows = conn.execute("SELECT ts, channel, gifter, plan, months FROM gifts"
                                " ORDER BY ts DESC, id DESC LIMIT ?", (_limit(limit, 20),))
            return [{"ts": ts, "channel": channel, "gifter": gifter, "plan": plan, "months": months}
                    for ts, channel, gifter, plan, months in rows]
        return self._run("recent_gifts", [], op)

    def lurk_today(self, ts: float) -> dict[str, float]:
        """Seconds spent in each channel on the UTC day of ts, most first."""
        def op(conn: sqlite3.Connection) -> dict[str, float]:
            rows = conn.execute("SELECT channel, seconds FROM presence WHERE day = ?"
                                " ORDER BY seconds DESC, channel", (utc_day(ts),))
            return {channel: float(seconds) for channel, seconds in rows}
        return self._run("lurk_today", {}, op)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        def op(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
            return default if row is None else row[0]
        return self._run("get_meta", default, op)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the database. Safe to call twice; afterwards every method returns its empty value."""
        with self._lock:
            if self._conn is None:
                return
            try:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("PRAGMA optimize")
                self._conn.close()
            except Exception as e:
                self._fail("close", e)
