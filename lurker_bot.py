"""
Twitch Lurker Bot
-----------------
Joins your followed channels + top 80 EN streamers.
Tracks gifted subs received in chat.

Note: Official Twitch Channel Points and Watch Hours require
the video player to be open — chat presence alone does not count.
Third-party bot points (StreamElements, Nightbot, etc.) DO work
just from being in chat.
"""

from __future__ import annotations

import asyncio
import os
import sys
import requests
from twitchio.ext import commands

# ── Config (reads from env vars on Railway, falls back to defaults locally) ───

CLIENT_ID      = os.getenv("CLIENT_ID",      "ankargtqxwxgu2aik19pnjf7xbe33z")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET",  "228sw56twcduvvvmjjauamd5tv9acj")
USER_CLIENT_ID = os.getenv("USER_CLIENT_ID", "gp762nuuoqcoxypju8c569th9wz7q5")
OAUTH_TOKEN    = os.getenv("OAUTH_TOKEN",    "oauth:0a8t3cqjyg36gpr7yhdi6ao3iyq7re")
REFRESH_TOKEN  = os.getenv("REFRESH_TOKEN",  "p1b29af7vjl655ny1mz8ag68xjsc2buu9p736eq7d9fu4r4fib")

MAX_CHANNELS     = 80
LANGUAGE         = "en"
JOIN_DELAY       = 0.6
REFRESH_INTERVAL = 1800

IS_TTY = sys.stdout.isatty()  # False on Railway — switches to plain log output

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_app_access_token(client_id: str, client_secret: str) -> str | None:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "client_credentials",
        },
    )
    if resp.ok:
        return resp.json()["access_token"]
    print(f"[auth] Failed to get app token: {resp.text}")
    return None

def refresh_oauth_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str] | None:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if resp.ok:
        data = resp.json()
        return data["access_token"], data["refresh_token"]
    return None

# ── Bot ───────────────────────────────────────────────────────────────────────

class LurkerBot(commands.Bot):

    def __init__(self, oauth_token: str, client_id: str, app_token: str, refresh_token: str):
        super().__init__(token=oauth_token, prefix="!", initial_channels=[])
        self.client_id     = client_id
        self.app_token     = app_token
        self.user_token    = oauth_token.replace("oauth:", "")
        self.refresh_token = refresh_token
        self.joined        = set()
        self.gifted_subs   = 0

    # ── Display ───────────────────────────────────────────────────────────────

    def _status(self):
        line = f"  Channels: {len(self.joined)}/{MAX_CHANNELS}  |  Gifted subs: {self.gifted_subs}"
        if IS_TTY:
            sys.stdout.write(f"\r{line:<80}")
            sys.stdout.flush()

    def _log(self, msg: str):
        if IS_TTY:
            sys.stdout.write(f"\r{msg:<80}\n")
            self._status()
        else:
            print(msg, flush=True)

    # ── API helpers ───────────────────────────────────────────────────────────

    def _app_headers(self) -> dict:
        return {"Client-ID": self.client_id, "Authorization": f"Bearer {self.app_token}"}

    def _user_headers(self) -> dict:
        return {"Client-ID": USER_CLIENT_ID, "Authorization": f"Bearer {self.user_token}"}

    def _get_user_id(self) -> str | None:
        resp = requests.get("https://api.twitch.tv/helix/users", headers=self._user_headers())
        if resp.ok:
            data = resp.json().get("data", [])
            return data[0]["id"] if data else None
        return None

    def _get_live_followed_channels(self, user_id: str) -> list[str]:
        resp = requests.get(
            "https://api.twitch.tv/helix/channels/followed",
            headers=self._user_headers(),
            params={"user_id": user_id, "first": 100},
        )
        if not resp.ok:
            return []

        logins = [c["broadcaster_login"] for c in resp.json().get("data", [])]
        if not logins:
            return []

        params = [("user_login", l) for l in logins] + [("first", 100)]
        resp2 = requests.get(
            "https://api.twitch.tv/helix/streams",
            headers=self._user_headers(),
            params=params,
        )
        if resp2.ok:
            return [s["user_login"] for s in resp2.json()["data"]]
        return []

    def _get_top_streamers(self, count: int) -> list[str]:
        resp = requests.get(
            "https://api.twitch.tv/helix/streams",
            headers=self._app_headers(),
            params={"first": count, "language": LANGUAGE},
        )
        if resp.ok:
            return [s["user_login"] for s in resp.json()["data"]]
        self._log(f"[api] Failed to fetch top streams: {resp.text}")
        return []

    # ── Channel management ────────────────────────────────────────────────────

    def _build_target_list(self) -> list[str]:
        user_id  = self._get_user_id()
        followed = self._get_live_followed_channels(user_id) if user_id else []

        remaining = MAX_CHANNELS - len(followed)
        top       = self._get_top_streamers(remaining) if remaining > 0 else []

        self._log(f"[sync] {len(followed)} followed live  |  {len(top)} top streamers to fill")

        seen, merged = set(), []
        for ch in followed + top:
            if ch not in seen:
                seen.add(ch)
                merged.append(ch)
        return merged[:MAX_CHANNELS]

    async def _join(self, channel: str) -> bool:
        if channel in self.joined or len(self.joined) >= MAX_CHANNELS:
            return False
        try:
            await self.join_channels([channel])
            self.joined.add(channel)
            await asyncio.sleep(JOIN_DELAY)
            return True
        except Exception as e:
            self._log(f"[join] Failed #{channel}: {e}")
            return False

    async def _sync_channels(self):
        target = self._build_target_list()
        for ch in target:
            if len(self.joined) >= MAX_CHANNELS:
                break
            if await self._join(ch):
                self._log(f"[join] #{ch}")
        self._log(f"[sync] Lurking in {len(self.joined)} channels")

    async def _periodic_refresh(self):
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            self._log("[sync] Refreshing channel list...")
            self.joined.clear()
            await self._sync_channels()

    # ── Events ────────────────────────────────────────────────────────────────

    async def event_ready(self):
        self.joined.clear()

        result = refresh_oauth_token(CLIENT_ID, CLIENT_SECRET, self.refresh_token)
        if result:
            new_token, new_refresh = result
            self.refresh_token = new_refresh
            self._token     = new_token
            self.user_token = new_token
            self._log("[auth] Token refreshed")

        self._log(f"[ready] Logged in as {self.nick}")
        await self._sync_channels()
        asyncio.create_task(self._periodic_refresh())

    async def event_message(self, message):
        pass

    async def event_raw_usernotice(self, channel, tags: dict):
        if tags.get("msg-id") == "subgift":
            recipient = tags.get("msg-param-recipient-user-name", "")
            if recipient.lower() == self.nick.lower():
                gifter = tags.get("display-name", "someone")
                self.gifted_subs += 1
                self._log(f"[gift] {gifter} gifted you a sub in #{channel.name}!")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app_token = get_app_access_token(CLIENT_ID, CLIENT_SECRET)
    if not app_token:
        raise SystemExit("Could not obtain app access token — check your credentials.")

    LurkerBot(OAUTH_TOKEN, CLIENT_ID, app_token, REFRESH_TOKEN).run()
