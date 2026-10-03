import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from autojoin import AutoJoiner, ChannelStore

class AutoJoinTests(unittest.IsolatedAsyncioTestCase):
    def test_durable_uncapped_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.sqlite3"
            store = ChannelStore(path)
            names = [f"viewer{i}" for i in range(150)]
            self.assertEqual(store.add(names), names)
            self.assertEqual(store.add(["viewer0", "invalid-name", "viewer150"]), ["viewer150"])
            store.close()
            restored = ChannelStore(path)
            self.assertEqual(len(restored.channels), 151)
            self.assertEqual(restored.channels[0], "viewer0")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored.close()

    async def test_only_actual_chat_authors_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(joined={"watched"}, nick="mybot", _resync=asyncio.Event(), _log=lambda msg: None)
            auto = AutoJoiner(bot, "all", str(Path(directory) / "channels.sqlite3"), None, ["pinned"])
            auto.observe("@badge=1 :Alice!alice@host PRIVMSG #watched :hello")
            auto.observe(":Alice!alice@host PRIVMSG #watched :again")
            auto.observe(":mybot!mybot@host PRIVMSG #watched :hello")
            auto.observe(":pinned!pinned@host PRIVMSG #watched :hello")
            auto.observe(":outsider!outsider@host PRIVMSG #elsewhere :hello")
            auto.observe(":someone!someone@host NOTICE #watched :not a message")
            self.assertEqual(auto.store.channels, ("alice",))
            auto.ready.set()
            task = asyncio.create_task(auto.run())
            await asyncio.wait_for(bot._resync.wait(), timeout=4)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            auto.store.close()

if __name__ == "__main__":
    unittest.main()
