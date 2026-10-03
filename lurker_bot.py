"""
Twitch Lurker Bot
-----------------
Joins your live followed channels, then fills up to 80 channels with the
top live EN streamers. Tracks gifted subs received in chat.

Note: Official Twitch Channel Points and Watch Hours require
the video player to be open — chat presence alone does not count.
Third-party bot points (StreamElements, Nightbot, etc.) DO work
just from being in chat.

Built to run unattended: it refreshes its token, rejoins after reconnects,
retries Twitch outages, and restarts itself if the connection stalls. It only
exits when the token is dead and can't be renewed.

Configuration comes from environment variables (or a local .env file).
See README.md for setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

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
RETRY_DELAY       = 60     # Wait after a failed Twitch API call
PING_INTERVAL     = 60     # PING Twitch when the connection has been quiet this long
STALL_TIMEOUT     = 180    # Restart when nothing at all has arrived for this long
RESTART_DELAY_MIN = 5      # Backoff between restarts, doubling...
RESTART_DELAY_MAX = 300    # ...up to this
HEALTHY_RUN       = 600    # A run that lasted this long resets the backoff
HTTP_TIMEOUT      = 10

SCOPE_CHAT    = "chat:read"
SCOPE_FOLLOWS = "user:read:follows"

HELIX_URL    = "https://api.twitch.tv/helix"
TOKEN_URL    = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

IS_TTY = sys.stdout.isatty()  # False on Railway — switches to plain log output

# ── Auth ──────────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Twitch rejected the credentials — retrying won't help until they're replaced."""

def validate_token(token: str) -> dict | None:
    """Return the token's metadata, or None if Twitch says it's invalid or expired."""
    resp = requests.get(VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 401:
        return None
    resp.raise_for_status()
    return resp.json()

def refresh_oauth_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Raises AuthError if Twitch rejects the refresh, requests.RequestException if it's unreachable."""
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
    if resp.status_code in (400, 401, 403):
        raise AuthError(f"Token refresh rejected ({resp.status_code}): {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data.get("refresh_token", refresh_token)

def get_valid_token(token: str, refresh_token: str, can_refresh: bool) -> tuple[str, str, dict]:
    """Validate the token, refreshing it if it has expired. Network errors are retried forever."""
    delay, refreshed = RESTART_DELAY_MIN, False
    while True:
        try:
            info = validate_token(token)
            if info is not None:
                return token, refresh_token, info
            if not can_refresh or refreshed:
                raise AuthError("OAUTH_TOKEN is invalid or expired and could not be refreshed")
            print("[auth] Token has expired, refreshing...", flush=True)
            token, refresh_token = refresh_oauth_token(CLIENT_ID, CLIENT_SECRET, refresh_token)
            refreshed = True
        except requests.RequestException as e:
            print(f"[auth] Can't reach Twitch, retrying in {delay}s: {e}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)

def check_token(info: dict, can_refresh: bool) -> bool:
    """Exit if the token can't join chat, warn about anything that limits the bot. Returns can_refresh."""
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
    return can_refresh

# ── Bot ───────────────────────────────────────────────────────────────────────

class LurkerBot(commands.Bot):

    # Don't dispatch an event for every chat message — only USERNOTICEs matter here
    event_message = None

    def __init__(self, token: str, refresh_token: str, token_info: dict, can_refresh: bool, gifted_subs: int = 0):
        super().__init__(token=token, prefix="!", initial_channels=[])
        # twitchio keeps every chatter it sees until the channel is parted, which
        # leaks memory across 80 busy chats. Nothing here reads that cache.
        self._connection._cache_add = lambda parsed: None

        self.user_token    = token
        self.refresh_token = refresh_token
        self.can_refresh   = can_refresh
        self.api_client_id = token_info["client_id"]  # Helix needs the client ID that issued the token
        self.account_id    = token_info["user_id"]
        self.has_follows   = SCOPE_FOLLOWS in (token_info.get("scopes") or [])
        self.joined        = set()
        self.gifted_subs   = gifted_subs
        self.fatal         = False
        self._stopping     = False
        self._last_data    = time.monotonic()
        self._sync_lock    = asyncio.Lock()
        self._resync       = asyncio.Event()
        self._check_token  = asyncio.Event()
        self._run_task: asyncio.Task | None = None
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._run_task = asyncio.current_task()
        # These outlive twitchio's own reconnects; event_ready fires again after each one
        self._tasks = [
            asyncio.create_task(self._maintain_token()),
            asyncio.create_task(self._periodic_refresh()),
            asyncio.create_task(self._watchdog()),
        ]
        await super().start()

    async def _stop(self, reason: str, fatal: bool):
        """Disconnect and end start(); main() then exits (fatal) or starts a fresh bot."""
        if self._stopping:
            return
        self._stopping = True
        self.fatal     = fatal
        self._log(reason)
        try:
            await self.close()
        except Exception as e:
            self._log(f"[main] Error while disconnecting: {e!r}")
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()  # In case start() is still stuck connecting

    async def _watchdog(self):
        """Restart if the connection goes silent, e.g. after a reconnect that never completed."""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            idle = time.monotonic() - self._last_data
            if idle >= STALL_TIMEOUT:
                await self._stop(f"[conn] Nothing from Twitch for {idle:.0f}s, restarting", fatal=False)
                return
            if idle >= PING_INTERVAL:
                with contextlib.suppress(Exception):  # A dead socket is caught by the stall check
                    await self._connection.send("PING :tmi.twitch.tv")

    # ── Token upkeep ──────────────────────────────────────────────────────────

    def _set_user_token(self, token: str):
        self.user_token = token
        # twitchio 2.x reads these on every IRC (re)connect and Helix call
        self._connection._token = token
        self._http.token        = token

    async def _maintain_token(self):
        delay = 0
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._check_token.wait(), timeout=delay)
            self._check_token.clear()
            try:
                delay = await self._validate_or_refresh()
            except AuthError as e:
                await self._stop(f"[auth] {e} — see README.md", fatal=True)
                return
            except Exception as e:
                self._log(f"[auth] Token check failed, retrying in {RETRY_DELAY}s: {e!r}")
                delay = RETRY_DELAY

    async def _validate_or_refresh(self) -> float:
        """Validate the token and refresh it if it's (nearly) expired. Returns seconds until the next check."""
        info       = await asyncio.to_thread(validate_token, self.user_token)
        expires_in = info["expires_in"] if info else 0
        if info is not None and not 0 < expires_in <= REFRESH_MARGIN:
            return min(VALIDATE_INTERVAL, expires_in - REFRESH_MARGIN) if expires_in else VALIDATE_INTERVAL

        if not self.can_refresh:
            if info is None:
                raise AuthError("Token is invalid or expired and can't be refreshed")
            return RETRY_DELAY
        token, self.refresh_token = await asyncio.to_thread(
            refresh_oauth_token, CLIENT_ID, CLIENT_SECRET, self.refresh_token
        )
        self._set_user_token(token)
        self._log("[auth] Token refreshed")
        return RETRY_DELAY  # Re-validate the new token shortly

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
                self._log(f"[sync] Twitch API error, retrying in {RETRY_DELAY}s: {e}")
                if getattr(e.response, "status_code", None) == 401:
                    self._check_token.set()  # Token died early — refresh it now
                self.loop.call_later(RETRY_DELAY, self._resync.set)
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
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resync.wait(), timeout=REFRESH_INTERVAL)
            self._resync.clear()
            self._log("[sync] Refreshing channel list...")
            try:
                await self._sync_channels()
            except Exception as e:
                self._log(f"[sync] Refresh failed, retrying in {RETRY_DELAY}s: {e!r}")
                self.loop.call_later(RETRY_DELAY, self._resync.set)

    # ── Events ────────────────────────────────────────────────────────────────

    async def event_ready(self):
        self._log(f"[ready] Logged in as {self.nick}")
        await self._sync_channels(reset=True)

    async def event_raw_data(self, data):
        self._last_data = time.monotonic()
        # Close frames arrive here as an int close code, hence the isinstance check
        if not isinstance(data, str):
            return
        if "NOTICE * :Login authentication failed" in data or "NOTICE * :Login unsuccessful" in data:
            # twitchio would otherwise reconnect in a tight loop. A restart re-validates
            # (and refreshes) the token, and exits if it's really dead.
            await asyncio.sleep(0)  # Let twitchio finish handling this line before we disconnect
            await self._stop("[auth] Twitch rejected the chat login, restarting", fatal=False)

    async def event_channel_join_failure(self, channel: str):
        self.joined.discard(channel)
        self._log(f"[join] Timed out joining #{channel}")

    async def event_raw_usernotice(self, channel, tags: dict):
        if tags.get("msg-id") in ("subgift", "anonsubgift"):
            if tags.get("msg-param-recipient-id") == self.account_id:
                gifter = tags.get("display-name") or "someone"
                self.gifted_subs += 1
                self._log(f"[gift] {gifter} gifted you a sub in #{channel.name}! (total: {self.gifted_subs})")

# ── Entry point ───────────────────────────────────────────────────────────────

def run_bot(bot: LurkerBot):
    """Run the bot until it stops, then tear down its event loop."""
    loop = bot.loop
    try:
        loop.run_until_complete(bot.start())
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        bot.fatal = True
        with contextlib.suppress(Exception):
            loop.run_until_complete(bot.close())
    except Exception as e:
        print(f"[main] Bot crashed: {e!r}", flush=True)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

def main():
    if not OAUTH_TOKEN:
        raise SystemExit("OAUTH_TOKEN is not set — see README.md")

    token         = OAUTH_TOKEN.removeprefix("oauth:")
    refresh_token = REFRESH_TOKEN
    can_refresh   = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)
    gifted_subs   = 0
    delay         = RESTART_DELAY_MIN
    checked       = False

    while True:
        try:
            token, refresh_token, info = get_valid_token(token, refresh_token, can_refresh)
        except AuthError as e:
            raise SystemExit(f"[auth] {e} — see README.md")
        if not checked:
            can_refresh = check_token(info, can_refresh)
            checked     = True

        asyncio.set_event_loop(asyncio.new_event_loop())  # Each run gets a fresh loop
        bot     = LurkerBot(token, refresh_token, info, can_refresh, gifted_subs)
        started = time.monotonic()
        run_bot(bot)
        if bot.fatal:
            raise SystemExit(1)

        # Carry state into the next run — the token may have been refreshed meanwhile
        token, refresh_token, gifted_subs = bot.user_token, bot.refresh_token, bot.gifted_subs
        if time.monotonic() - started >= HEALTHY_RUN:
            delay = RESTART_DELAY_MIN
        print(f"[main] Restarting in {delay}s...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, RESTART_DELAY_MAX)

if __name__ == "__main__":
    main()
