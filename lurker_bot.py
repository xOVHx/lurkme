"""
Twitch Lurker Bot
-----------------
Joins your live followed channels, then fills up to 80 channels with the
top live EN streamers. Tracks gifted subs received in chat.

Note: Official Twitch Channel Points and Watch Hours require
the video player to be open — chat presence alone does not count.
Third-party bot points (StreamElements, Nightbot, etc.) DO work
just from being in chat.

Configuration comes from environment variables (or a local .env file).
See README.md for setup.
"""

from __future__ import annotations

import asyncio
import os
import sys

import requests
from dotenv import load_dotenv
from twitchio.ext import commands

# ── Config (env vars on Railway, or a local .env file — never hardcode secrets) ─

load_dotenv()

CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
OAUTH_TOKEN   = os.getenv("OAUTH_TOKEN", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

MAX_CHANNELS      = 80
LANGUAGE          = "en"
JOIN_DELAY        = 0.6    # Twitch allows 20 JOINs per 10 seconds
REFRESH_INTERVAL  = 1800   # Seconds between channel list refreshes
VALIDATE_INTERVAL = 3600   # Twitch requires validating user tokens hourly
REFRESH_MARGIN    = 900    # Refresh the token once it has less than this left
RETRY_DELAY       = 60
HTTP_TIMEOUT      = 10

SCOPE_CHAT    = "chat:read"
SCOPE_FOLLOWS = "user:read:follows"

HELIX_URL    = "https://api.twitch.tv/helix"
TOKEN_URL    = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

IS_TTY = sys.stdout.isatty()  # False on Railway — switches to plain log output

# ── Auth ──────────────────────────────────────────────────────────────────────

def validate_token(token: str) -> dict | None:
    """Return the token's metadata, or None if Twitch says it's invalid or expired."""
    resp = requests.get(VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 401:
        return None
    resp.raise_for_status()
    return resp.json()

def refresh_oauth_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str] | None:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=HTTP_TIMEOUT,
    )
    if resp.ok:
        data = resp.json()
        return data["access_token"], data.get("refresh_token", refresh_token)
    print(f"[auth] Token refresh failed ({resp.status_code}): {resp.text}", flush=True)
    return None

# ── Bot ───────────────────────────────────────────────────────────────────────

class LurkerBot(commands.Bot):

    def __init__(self, token: str, refresh_token: str, token_info: dict, can_refresh: bool):
        super().__init__(token=token, prefix="!", initial_channels=[])
        self.user_token    = token
        self.refresh_token = refresh_token
        self.can_refresh   = can_refresh
        self.api_client_id = token_info["client_id"]  # Helix needs the client ID that issued the token
        self.account_id    = token_info["user_id"]
        self.has_follows   = SCOPE_FOLLOWS in (token_info.get("scopes") or [])
        self.joined        = set()
        self.gifted_subs   = 0
        self.failed        = False
        self._sync_lock    = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

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

    # ── Token upkeep ──────────────────────────────────────────────────────────

    def _set_user_token(self, token: str):
        self.user_token = token
        # twitchio 2.x reads these on every IRC (re)connect and Helix call
        self._connection._token = token
        self._http.token        = token

    async def _refresh_user_token(self) -> bool:
        result = await asyncio.to_thread(refresh_oauth_token, CLIENT_ID, CLIENT_SECRET, self.refresh_token)
        if not result:
            return False
        token, self.refresh_token = result
        self._set_user_token(token)
        self._log("[auth] Token refreshed")
        return True

    async def _maintain_token(self):
        while True:
            try:
                info       = await asyncio.to_thread(validate_token, self.user_token)
                expires_in = info["expires_in"] if info else 0
                expiring   = info is None or 0 < expires_in <= REFRESH_MARGIN

                if expiring and self.can_refresh and await self._refresh_user_token():
                    continue
                if info is None:
                    await self._fail("[auth] Token is invalid or expired and could not be refreshed — see README.md")
                    return

                if expiring:
                    delay = RETRY_DELAY
                elif expires_in:
                    delay = min(VALIDATE_INTERVAL, expires_in - REFRESH_MARGIN)
                else:
                    delay = VALIDATE_INTERVAL
            except requests.RequestException as e:
                self._log(f"[auth] Token check failed, retrying in {RETRY_DELAY}s: {e}")
                delay = RETRY_DELAY
            await asyncio.sleep(delay)

    async def _fail(self, reason: str):
        """Log and disconnect; main() then exits non-zero."""
        if self.failed:
            return
        self._log(reason)
        self.failed = True
        await self.close()

    # ── API helpers ───────────────────────────────────────────────────────────

    def _helix_get(self, path: str, params: dict) -> dict:
        resp = requests.get(
            f"{HELIX_URL}/{path}",
            headers={"Client-ID": self.api_client_id, "Authorization": f"Bearer {self.user_token}"},
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _get_live_followed_channels(self) -> list[str]:
        logins, cursor = [], None
        while True:
            params = {"user_id": self.account_id, "first": 100}
            if cursor:
                params["after"] = cursor
            data   = self._helix_get("streams/followed", params)
            logins += [s["user_login"] for s in data.get("data", [])]
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor or len(logins) >= MAX_CHANNELS:
                return logins

    def _get_top_streamers(self) -> list[str]:
        data = self._helix_get("streams", {"first": 100, "language": LANGUAGE})
        return [s["user_login"] for s in data.get("data", [])]

    # ── Channel management ────────────────────────────────────────────────────

    def _build_target_list(self) -> list[str]:
        followed = list(dict.fromkeys(self._get_live_followed_channels())) if self.has_follows else []
        top      = self._get_top_streamers() if len(followed) < MAX_CHANNELS else []

        merged     = list(dict.fromkeys(followed + top))[:MAX_CHANNELS]
        n_followed = min(len(followed), MAX_CHANNELS)
        self._log(f"[sync] {n_followed} followed live  |  {len(merged) - n_followed} top streamers to fill")
        return merged

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

    async def _sync_channels(self, reset: bool = False):
        async with self._sync_lock:
            if reset:
                # New IRC connection — twitchio only rejoins initial_channels, so we start from zero
                self.joined.clear()
            try:
                target = await asyncio.to_thread(self._build_target_list)
            except requests.RequestException as e:
                self._log(f"[sync] Twitch API error, keeping current channels: {e}")
                return

            stale = [ch for ch in self.joined if ch not in target]
            if stale:
                await self.part_channels(stale)
                self.joined.difference_update(stale)
                self._log(f"[part] Left {len(stale)} channels no longer in the list")

            for ch in target:
                if await self._join(ch):
                    self._log(f"[join] #{ch}")
            self._log(f"[sync] Lurking in {len(self.joined)} channels")

    async def _periodic_refresh(self):
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            self._log("[sync] Refreshing channel list...")
            try:
                await self._sync_channels()
            except Exception as e:
                self._log(f"[sync] Refresh failed: {e!r}")

    # ── Events ────────────────────────────────────────────────────────────────

    async def event_ready(self):
        # Fires again after every reconnect, so only start the background loops once
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._maintain_token()),
                asyncio.create_task(self._periodic_refresh()),
            ]
        self._log(f"[ready] Logged in as {self.nick}")
        await self._sync_channels(reset=True)

    async def event_raw_data(self, data):
        # twitchio would otherwise reconnect in a tight loop with a rejected token.
        # Close frames arrive here as an int close code, hence the isinstance check.
        if not isinstance(data, str):
            return
        if "NOTICE * :Login authentication failed" in data or "NOTICE * :Login unsuccessful" in data:
            await self._fail("[auth] Twitch rejected the chat login — token revoked or expired, see README.md")

    async def event_channel_join_failure(self, channel: str):
        self.joined.discard(channel)
        self._log(f"[join] Timed out joining #{channel}")

    async def event_message(self, message):
        pass

    async def event_raw_usernotice(self, channel, tags: dict):
        if tags.get("msg-id") in ("subgift", "anonsubgift"):
            if tags.get("msg-param-recipient-id") == self.account_id:
                gifter = tags.get("display-name") or "someone"
                self.gifted_subs += 1
                self._log(f"[gift] {gifter} gifted you a sub in #{channel.name}!")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not OAUTH_TOKEN:
        raise SystemExit("OAUTH_TOKEN is not set — see README.md")

    token         = OAUTH_TOKEN.removeprefix("oauth:")
    refresh_token = REFRESH_TOKEN
    can_refresh   = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)

    try:
        info = validate_token(token)
        if info is None and can_refresh:
            print("[auth] OAUTH_TOKEN has expired, refreshing...", flush=True)
            result = refresh_oauth_token(CLIENT_ID, CLIENT_SECRET, refresh_token)
            if result:
                token, refresh_token = result
                info = validate_token(token)
    except requests.RequestException as e:
        raise SystemExit(f"[auth] Could not reach Twitch: {e}")

    if info is None:
        raise SystemExit("[auth] OAUTH_TOKEN is invalid or expired and could not be refreshed — see README.md")

    scopes = info.get("scopes") or []
    if SCOPE_CHAT not in scopes:
        raise SystemExit(f"[auth] OAUTH_TOKEN is missing the {SCOPE_CHAT} scope needed to join chat")
    if SCOPE_FOLLOWS not in scopes:
        print(f"[auth] OAUTH_TOKEN lacks {SCOPE_FOLLOWS} — skipping followed channels", flush=True)

    if can_refresh and info["client_id"] != CLIENT_ID:
        print("[auth] OAUTH_TOKEN was issued by a different app than CLIENT_ID, so it can't be refreshed", flush=True)
        can_refresh = False
    if not can_refresh and info.get("expires_in"):
        hours = info["expires_in"] / 3600
        print(f"[auth] Token can't be auto-refreshed — the bot will stop in ~{hours:.1f}h (see README.md)", flush=True)

    bot  = LurkerBot(token, refresh_token, info, can_refresh)
    loop = bot.loop
    try:
        loop.run_until_complete(bot.start())  # returns once bot.close() is called
    except KeyboardInterrupt:
        loop.run_until_complete(bot.close())
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
    if bot.failed:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
