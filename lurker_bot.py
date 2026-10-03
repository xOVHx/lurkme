"""
Twitch Lurker Bot
-----------------
Joins your pinned channels and live followed channels, then fills up to 80
channels with the top live streams (EN by default, optionally filtered by
category). Tracks gifted subs received in chat.

Note: Official Twitch Channel Points and Watch Hours require
the video player to be open — chat presence alone does not count.
Third-party bot points (StreamElements, Nightbot, etc.) DO work
just from being in chat.

Built to run unattended: it refreshes its token, rejoins after reconnects,
retries Twitch outages, and restarts itself if the connection stalls. It only
exits when the token is dead and can't be renewed.

Configuration comes from environment variables, a local .env file, or the file
named by LURKME_ENV_FILE (the VPS installer uses /etc/lurkme/lurkme.env).
See README.md for setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from twitchio.ext import commands

# ── Config (env vars or a .env file — never hardcode secrets) ──────────────────

load_dotenv(os.getenv("LURKME_ENV_FILE"))  # None = look for a .env file as usual

CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
OAUTH_TOKEN   = os.getenv("OAUTH_TOKEN", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]

# Optional, comma-separated — see README.md
CHANNELS   = list(dict.fromkeys(c.lower().lstrip("#") for c in _env_list("CHANNELS")))  # Always joined
LANGUAGES  = [lang.lower() for lang in _env_list("STREAM_LANGUAGES")] or ["en"]         # Top-stream fill; "any" = all
CATEGORIES = _env_list("CATEGORIES")                                                    # Top-stream fill; exact Twitch names

# Optional gift alerts. The webhook URL is a secret: anyone who has it can post to the channel.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USER_ID     = os.getenv("DISCORD_USER_ID", "").strip()  # Numeric ID of the Discord user to @mention

MAX_CHANNELS      = 80     # Twitch allows 100 joined chats per account (since May 2024)
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
EXIT_CONFIG       = 78     # EX_CONFIG: credentials or settings need fixing — restarting won't help

SCOPE_CHAT    = "chat:read"
SCOPE_FOLLOWS = "user:read:follows"

LOGIN_RE    = re.compile(r"[a-z0-9_]{1,25}")
LANGUAGE_RE = re.compile(r"[a-z]{2}|other|any")
WEBHOOK_RE  = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")
USER_ID_RE  = re.compile(r"\d{15,21}")

HELIX_URL    = "https://api.twitch.tv/helix"
TOKEN_URL    = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

IS_TTY = sys.stdout.isatty()  # False under systemd — switches to plain log output

# ── Auth ──────────────────────────────────────────────────────────────────────

def die(msg: str):
    """Exit with EXIT_CONFIG, which tells systemd (RestartPreventExitStatus) not to restart."""
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(EXIT_CONFIG)

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
        except OSError as e:  # Includes every requests error, plus TLS/CA-bundle problems
            print(f"[auth] Can't reach Twitch, retrying in {delay}s: {e}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)

def check_config():
    """Drop malformed CHANNELS / STREAM_LANGUAGES entries with a warning, then log the channel mix."""
    global CHANNELS, LANGUAGES, DISCORD_WEBHOOK_URL, DISCORD_USER_ID
    for name, values, pattern in (("CHANNELS", CHANNELS, LOGIN_RE), ("STREAM_LANGUAGES", LANGUAGES, LANGUAGE_RE)):
        bad = [v for v in values if not pattern.fullmatch(v)]
        if bad:
            print(f"[config] Ignoring invalid {name}: {', '.join(bad)}", flush=True)
    CHANNELS  = [c for c in CHANNELS if LOGIN_RE.fullmatch(c)]
    LANGUAGES = [lang for lang in LANGUAGES if LANGUAGE_RE.fullmatch(lang)] or ["en"]
    if len(CHANNELS) > MAX_CHANNELS:
        print(f"[config] Only the first {MAX_CHANNELS} CHANNELS fit — the rest are ignored", flush=True)

    if DISCORD_WEBHOOK_URL and not WEBHOOK_RE.fullmatch(DISCORD_WEBHOOK_URL):
        print("[config] Ignoring DISCORD_WEBHOOK_URL — it isn't a Discord webhook URL", flush=True)  # Never print it
        DISCORD_WEBHOOK_URL = ""
    if DISCORD_USER_ID and not USER_ID_RE.fullmatch(DISCORD_USER_ID):
        print("[config] Ignoring DISCORD_USER_ID — it must be the numeric user ID, not a username (see README.md)", flush=True)
        DISCORD_USER_ID = ""

    discord = ("on, mentioning you" if DISCORD_USER_ID else "on") if DISCORD_WEBHOOK_URL else "off"
    print(f"[config] Pinned: {len(CHANNELS)}  |  Languages: {', '.join(LANGUAGES)}"
          f"  |  Categories: {', '.join(CATEGORIES) or 'all'}  |  Discord alerts: {discord}", flush=True)

# ── Discord gift alerts ───────────────────────────────────────────────────────

TWITCH_PURPLE = 0x9146FF
SUB_TIERS     = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3", "Prime": "Prime"}

def _md(text: str) -> str:
    """Escape Discord markdown so names like some_streamer_ render as typed."""
    return re.sub(r"([\\*_~`|>\[\]()])", r"\\\1", text)

def build_gift_alert(gift: dict, channel_name: str, avatar_url: str | None) -> dict:
    """The webhook payload for one gifted sub: an @mention plus an embed card."""
    url    = f"https://www.twitch.tv/{gift['channel']}"
    tier   = SUB_TIERS.get(gift["plan"], "Tier 1")
    months = gift["months"]
    embed  = {
        "author":      {"name": f"{channel_name} on Twitch", "url": url},
        "title":       "🎁 You got a gifted sub!",
        "url":         url,
        "description": f"**{_md(gift['gifter'])}** gifted you a sub in **[{_md(channel_name)}]({url})**",
        "color":       TWITCH_PURPLE,
        "fields": [
            {"name": "Tier",   "value": tier, "inline": True},
            {"name": "Length", "value": f"{months} months" if months > 1 else "1 month", "inline": True},
            {"name": "Total",  "value": f"#{gift['total']} since the bot started", "inline": True},
        ],
        "footer":      {"text": "lurkme"},
        "timestamp":   gift["time"],
    }
    if avatar_url:
        embed["author"]["icon_url"] = avatar_url
        embed["thumbnail"]          = {"url": avatar_url}

    payload = {"username": "lurkme", "embeds": [embed], "allowed_mentions": {"parse": []}}
    if DISCORD_USER_ID:
        payload["content"]          = f"<@{DISCORD_USER_ID}>"
        payload["allowed_mentions"] = {"users": [DISCORD_USER_ID]}  # Ping only you, nothing else
    return payload

def send_discord(payload: dict):
    """POST to the webhook, waiting out rate limits. Raises requests errors (whose text includes the URL — don't log it)."""
    for _ in range(5):
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code != 429:
            resp.raise_for_status()
            return
        try:
            wait = float(resp.json()["retry_after"])
        except (ValueError, KeyError, TypeError):
            wait = 2.0
        time.sleep(min(wait, 60))
    raise requests.HTTPError(response=resp)

def check_token(info: dict, can_refresh: bool) -> bool:
    """Exit if the token can't join chat, warn about anything that limits the bot. Returns can_refresh."""
    scopes = info.get("scopes") or []
    if SCOPE_CHAT not in scopes:
        die(f"[auth] OAUTH_TOKEN is missing the {SCOPE_CHAT} scope needed to join chat")
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

    def __init__(self, token: str, refresh_token: str, token_info: dict, can_refresh: bool,
                 gifted_subs: int = 0, pending_alerts: list[dict] | None = None):
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
        self._category_ids: list[str] | None = None if CATEGORIES else []
        self.alerts: asyncio.Queue[dict] = asyncio.Queue()  # Gift alerts waiting for Discord
        self._alert_in_flight: dict | None = None
        for gift in pending_alerts or []:
            self.alerts.put_nowait(gift)

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
        if DISCORD_WEBHOOK_URL:
            self._tasks.append(asyncio.create_task(self._send_alerts()))
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

    def _helix_get(self, path: str, params: dict | list[tuple]) -> dict:
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

    def _get_category_ids(self) -> list[str]:
        if self._category_ids is None:  # Resolve CATEGORIES names once per run
            data  = self._helix_get("games", [("name", name) for name in CATEGORIES])
            found = {g["name"].lower(): g["id"] for g in data.get("data", [])}
            missing = [name for name in CATEGORIES if name.lower() not in found]
            if missing:
                self._log(f"[config] Unknown CATEGORIES (use the exact Twitch name): {', '.join(missing)}")
            if not found:
                self._log("[config] No CATEGORIES matched — filling from all categories")
            self._category_ids = list(found.values())
        return self._category_ids

    def _get_top_streamers(self) -> list[str]:
        params = [("first", 100)]
        if "any" not in LANGUAGES:
            params += [("language", lang) for lang in LANGUAGES]
        params += [("game_id", gid) for gid in self._get_category_ids()]
        data = self._helix_get("streams", params)
        return [s["user_login"] for s in data.get("data", [])]

    # ── Channel management ────────────────────────────────────────────────────

    def _build_target_list(self) -> list[str]:
        """Pinned channels first, then live followed channels, then top streams to fill."""
        followed = self._get_live_followed_channels() if self.has_follows else []
        mine     = list(dict.fromkeys(CHANNELS + followed))
        top      = self._get_top_streamers() if len(mine) < MAX_CHANNELS else []

        merged     = list(dict.fromkeys(mine + top))[:MAX_CHANNELS]
        n_pinned   = min(len(CHANNELS), MAX_CHANNELS)
        n_followed = min(len(mine), MAX_CHANNELS) - n_pinned
        self._log(f"[sync] {n_pinned} pinned  |  {n_followed} followed live  |  "
                  f"{len(merged) - n_pinned - n_followed} top streams to fill")
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
            except Exception as e:  # Never leave the bot sitting in zero channels over one bad response
                self._log(f"[sync] Twitch API error, retrying in {RETRY_DELAY}s: {type(e).__name__}: {e}")
                if getattr(getattr(e, "response", None), "status_code", None) == 401:
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
        msg_id = tags.get("msg-id")
        if msg_id not in ("subgift", "anonsubgift") or tags.get("msg-param-recipient-id") != self.account_id:
            return
        anonymous = msg_id == "anonsubgift" or tags.get("login") == "ananonymousgifter"
        gifter    = "An anonymous gifter" if anonymous else (tags.get("display-name") or tags.get("login") or "Someone")
        self.gifted_subs += 1
        self._log(f"[gift] {gifter} gifted you a sub in #{channel.name}! (total: {self.gifted_subs})")

        if DISCORD_WEBHOOK_URL:
            months = tags.get("msg-param-gift-months", "1")
            self.alerts.put_nowait({
                "channel": channel.name,
                "room_id": tags.get("room-id", ""),
                "gifter":  gifter,
                "plan":    tags.get("msg-param-sub-plan", "1000"),
                "months":  int(months) if months.isdigit() else 1,
                "total":   self.gifted_subs,
                "time":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })

    # ── Discord ───────────────────────────────────────────────────────────────

    def pending_alerts(self) -> list[dict]:
        """Alerts not yet delivered, including one cut off mid-send — handed to the next run on restart."""
        pending = [self._alert_in_flight] if self._alert_in_flight else []
        while not self.alerts.empty():
            pending.append(self.alerts.get_nowait())
        return pending

    def _channel_card(self, gift: dict) -> tuple[str, str | None]:
        """The channel's display name and avatar for the alert. Best effort: falls back to the login name."""
        try:
            users = self._helix_get("users", {"id": gift["room_id"]}).get("data", []) if gift["room_id"] else []
            if users:
                return users[0].get("display_name") or gift["channel"], users[0].get("profile_image_url") or None
        except Exception:
            pass
        return gift["channel"], None

    async def _send_alerts(self):
        while True:
            gift = self._alert_in_flight = await self.alerts.get()
            payload = build_gift_alert(gift, *await asyncio.to_thread(self._channel_card, gift))
            for attempt in range(1, 6):
                try:
                    await asyncio.to_thread(send_discord, payload)
                    self._log(f"[discord] Gift alert sent for #{gift['channel']}")
                    break
                except requests.HTTPError as e:
                    status = getattr(e.response, "status_code", None)
                    if status and 400 <= status < 500 and status != 429:
                        self._log(f"[discord] Webhook rejected the alert (HTTP {status}) — check DISCORD_WEBHOOK_URL")
                        break
                    problem = f"HTTP {status}"
                except Exception as e:
                    problem = type(e).__name__  # Not str(e): requests errors include the webhook URL
                delay = min(RETRY_DELAY, 5 * 2 ** attempt)
                self._log(f"[discord] Couldn't send gift alert ({problem}), retry {attempt}/5 in {delay}s")
                await asyncio.sleep(delay)
            else:
                self._log(f"[discord] Gave up on the gift alert for #{gift['channel']}")
            self._alert_in_flight = None

# ── Entry point ───────────────────────────────────────────────────────────────

def run_bot(bot: LurkerBot):
    """Run the bot until it stops, then tear down its event loop."""
    loop = bot.loop
    try:
        loop.run_until_complete(bot.start())
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        with contextlib.suppress(Exception):
            loop.run_until_complete(bot.close())
        raise
    except Exception as e:
        print(f"[main] Bot crashed: {e!r}", flush=True)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

def send_test_alert():
    """`lurker_bot.py --test-discord`: post a sample alert to check the webhook and the @mention."""
    check_config()
    if not DISCORD_WEBHOOK_URL:
        die("[discord] DISCORD_WEBHOOK_URL isn't set — see README.md")
    sample  = {"channel": "twitch", "room_id": "", "gifter": "lurkme", "plan": "1000", "months": 1, "total": 1,
               "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    payload = build_gift_alert(sample, "Twitch", None)
    payload["embeds"][0]["title"] = "🧪 Test alert: gift alerts are working"
    try:
        send_discord(payload)
    except requests.HTTPError as e:
        die(f"[discord] Discord rejected the test alert (HTTP {getattr(e.response, 'status_code', '?')}) — check DISCORD_WEBHOOK_URL")
    except OSError as e:
        die(f"[discord] Couldn't reach Discord ({type(e).__name__})")  # Not str(e): it includes the webhook URL
    print("[discord] Test alert sent — check your Discord", flush=True)

def main():
    if sys.argv[1:] == ["--test-discord"]:
        send_test_alert()
        return
    if not OAUTH_TOKEN:
        die("OAUTH_TOKEN is not set — see README.md")

    token         = OAUTH_TOKEN.removeprefix("oauth:")
    refresh_token = REFRESH_TOKEN
    can_refresh   = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)
    gifted_subs   = 0
    alerts        = []
    delay         = RESTART_DELAY_MIN
    checked       = False

    while True:
        try:
            token, refresh_token, info = get_valid_token(token, refresh_token, can_refresh)
        except AuthError as e:
            die(f"[auth] {e} — see README.md")
        if not checked:
            check_config()
            can_refresh = check_token(info, can_refresh)
            checked     = True

        asyncio.set_event_loop(asyncio.new_event_loop())  # Each run gets a fresh loop
        bot     = LurkerBot(token, refresh_token, info, can_refresh, gifted_subs, alerts)
        started = time.monotonic()
        run_bot(bot)
        if bot.fatal:
            raise SystemExit(EXIT_CONFIG)

        # Carry state into the next run — the token may have been refreshed meanwhile
        token, refresh_token, gifted_subs = bot.user_token, bot.refresh_token, bot.gifted_subs
        alerts = bot.pending_alerts()
        if time.monotonic() - started >= HEALTHY_RUN:
            delay = RESTART_DELAY_MIN
        print(f"[main] Restarting in {delay}s...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, RESTART_DELAY_MAX)

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
