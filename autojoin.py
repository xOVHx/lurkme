"""Durable discovery of chat authors' own channels, without an application cap."""
from __future__ import annotations
import asyncio
from pathlib import Path
import re
import sqlite3
import threading
import time

LOGIN = re.compile(r"[a-z0-9_]{1,25}")
MESSAGE = re.compile(r"^(?:@[^ ]+ )?:([a-zA-Z0-9_]{1,25})![^ ]+ PRIVMSG #([a-zA-Z0-9_]{1,25}) :")

class ChannelStore:
    def __init__(self, path: str | Path, limit: int | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS channels (login TEXT PRIMARY KEY, saved_at REAL NOT NULL)")
        self.db.commit()

    @property
    def channels(self) -> tuple[str, ...]:
        with self.lock:
            return tuple(row[0] for row in self.db.execute("SELECT login FROM channels ORDER BY rowid"))

    def add(self, channels: list[str]) -> list[str]:
        added = []
        with self.lock, self.db:
            count = self.db.execute("SELECT COUNT(*) FROM channels").fetchone()[0] if self.limit is not None else 0
            for channel in dict.fromkeys(channels):
                if not LOGIN.fullmatch(channel):
                    continue
                if self.limit is not None and count >= self.limit:
                    break
                result = self.db.execute("INSERT OR IGNORE INTO channels VALUES (?, ?)", (channel, time.time()))
                if result.rowcount:
                    added.append(channel)
                    count += 1
        return added

    def close(self):
        with self.lock:
            self.db.close()

class AutoJoiner:
    def __init__(self, bot, mode: str, path: str, limit: int | None, pinned: list[str]):
        self.bot = bot
        self.store = ChannelStore(path, limit)
        self.pinned = set(pinned)
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()

    def observe(self, data: str):
        candidates = []
        for line in data.splitlines():
            match = MESSAGE.match(line)
            if not match:
                continue
            author, channel = (name.lower() for name in match.groups())
            if channel in self.bot.joined and author != self.bot.nick and author not in self.pinned:
                candidates.append(author)
        if candidates and self.store.add(candidates):
            self.changed.set()

    async def run(self):
        await self.ready.wait()
        while True:
            await self.changed.wait()
            self.changed.clear()
            # Coalesce busy chat activity; discoveries already live in the database.
            await asyncio.sleep(2)
            count = len(await asyncio.to_thread(lambda: self.store.channels))
            self.bot._log(f"[auto] {count} permanent channels saved; scheduling joins")
            self.bot._resync.set()
