import asyncio
from pathlib import Path
import tempfile
import unittest
from autojoin import ChannelStore
import lurker_bot as module

class FakeBot(module.LurkerBot):
    def __init__(self, store):
        self.auto_join = type("Auto", (), {"store": store})()
        self.has_follows = True
        self.joined = set()
        self._sync_lock = asyncio.Lock()
        self._resync = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.followed = ["followed"]
        self.top = ["top"]
        self.parts = []
    def _get_live_followed_channels(self):
        if self.followed is None:
            raise OSError("simulated outage")
        return self.followed
    def _get_top_streamers(self):
        return self.top
    def _log(self, text):
        pass
    async def join_channels(self, channels):
        pass
    async def part_channels(self, channels):
        self.parts.extend(channels)

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_and_reconnect_keep_all_persistent_channels(self):
        previous_max, previous_delay = module.MAX_CHANNELS, module.JOIN_DELAY
        module.MAX_CHANNELS, module.JOIN_DELAY = 0, 0
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = ChannelStore(Path(directory) / "channels.sqlite3")
                store.add([f"viewer{i}" for i in range(151)])
                bot = FakeBot(store)
                await bot._sync_channels()
                self.assertEqual(len(bot.joined), 153)
                bot.top = ["different"]
                await bot._sync_channels()
                self.assertTrue(set(store.channels) <= bot.joined)
                self.assertEqual(bot.parts, ["top"])
                await bot._sync_channels(reset=True)
                self.assertTrue(set(store.channels) <= bot.joined)
                bot.followed = None
                await bot._sync_channels(reset=True)
                self.assertTrue(set(store.channels) <= bot.joined)
                store.close()
        finally:
            module.MAX_CHANNELS, module.JOIN_DELAY = previous_max, previous_delay

if __name__ == "__main__":
    unittest.main()
