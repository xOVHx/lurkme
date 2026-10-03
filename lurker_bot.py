"""
Twitch Lurker Bot
-----------------
Joins your pinned channels and live followed channels, then fills up to 100
channels (Twitch's per-account limit) with the top live streams (EN by default,
optionally filtered by category). Tracks gifted subs received in chat, records
every gift drop it sees, and keeps stats across restarts.

Extras: Discord alerts (gift cards with an @mention, a daily digest, and a
heads-up if the bot ever needs you) and a live web dashboard.

Note: Official Twitch Channel Points and Watch Hours require
the video player to be open — chat presence alone does not count.
Third-party loyalty points (StreamElements, Streamlabs, etc.) DO work
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
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from twitchio.ext import commands

import cards
import dashboard
from stats_store import StatsStore

# ── Config (env vars or a .env file — never hardcode secrets) ──────────────────

load_dotenv(os.getenv("LURKME_ENV_FILE"))  # None = look for a .env file as usual

CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
OAUTH_TOKEN   = os.getenv("OAUTH_TOKEN", "")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip().lower()
    if raw in ("off", "no", "false"):
        return 0
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[config] {name} must be a number — using {default}", flush=True)
        return default

TWITCH_CHANNEL_LIMIT = 100  # Twitch allows 100 joined chats per account (since May 2024)

# Optional, comma-separated — see README.md
CHANNELS     = list(dict.fromkeys(c.lower().lstrip("#") for c in _env_list("CHANNELS")))  # Always joined
LANGUAGES    = [lang.lower() for lang in _env_list("STREAM_LANGUAGES")] or ["en"]         # Top-stream fill; "any" = all
CATEGORIES   = _env_list("CATEGORIES")                                                    # Top-stream fill; exact Twitch names
MAX_CHANNELS = _env_int("MAX_CHANNELS", TWITCH_CHANNEL_LIMIT)                             # Capped at TWITCH_CHANNEL_LIMIT

# Optional Discord alerts. The webhook URL is a secret: anyone who has it can post to the channel.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USER_ID     = os.getenv("DISCORD_USER_ID", "").strip()  # Numeric ID of the Discord user to @mention
DIGEST_TIME         = os.getenv("DIGEST_TIME", "21:00").strip()  # Daily digest time, "off" to disable
TIMEZONE            = os.getenv("TIMEZONE", "").strip()          # IANA name for DIGEST_TIME; blank = server time

# Optional web dashboard. Without a password it only listens on this machine (use an SSH tunnel).
DASHBOARD_PORT     = _env_int("DASHBOARD_PORT", 8787)  # 0 / "off" disables it
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_HOST     = os.getenv("DASHBOARD_HOST", "").strip() or ("0.0.0.0" if DASHBOARD_PASSWORD else "127.0.0.1")

# Stats database: systemd's StateDirectory, or ./data next to this script
DATA_DIR = (os.getenv("LURKME_DATA_DIR") or os.getenv("STATE_DIRECTORY", "").split(":")[0]
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

JOIN_DELAY        = 0.6    # Twitch allows 20 JOINs per 10 seconds
REFRESH_INTERVAL  = 1800   # Seconds between channel list refreshes
INFO_INTERVAL     = 300    # Seconds between viewer-count/game refreshes for the dashboard
STATS_INTERVAL    = 60     # Seconds between lurk-time bookkeeping ticks
VALIDATE_INTERVAL = 3600   # Twitch requires validating user tokens hourly
REFRESH_MARGIN    = 900    # Refresh the token once it has less than this left
RETRY_DELAY       = 60     # Wait after a failed Twitch API call
PING_INTERVAL     = 60     # PING Twitch when the connection has been quiet this long
STALL_TIMEOUT     = 180    # Restart when nothing at all has arrived for this long
RESTART_DELAY_MIN = 5      # Backoff between restarts, doubling...
RESTART_DELAY_MAX = 300    # ...up to this
HEALTHY_RUN       = 600    # A run that lasted this long resets the backoff
ONLINE_AFTER_DOWN = 600    # Send the "online" card only after this much downtime (no spam on quick restarts)
ALERT_MAX_AGE     = 86400  # Keep retrying an undelivered Discord alert for up to a day
HTTP_TIMEOUT      = 10
EXIT_CONFIG       = 78     # EX_CONFIG: credentials or settings need fixing — restarting won't help

SCOPE_CHAT    = "chat:read"
SCOPE_FOLLOWS = "user:read:follows"

LOGIN_RE    = re.compile(r"[a-z0-9_]{1,25}")
LANGUAGE_RE = re.compile(r"[a-z]{2}|other|any")
WEBHOOK_RE  = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+")
USER_ID_RE  = re.compile(r"[0-9]{15,20}")  # ASCII digits only; Discord IDs are 64-bit
TIME_RE     = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")

LOGIN_FAILED = (":tmi.twitch.tv NOTICE * :Login authentication failed", ":tmi.twitch.tv NOTICE * :Login unsuccessful")

HELIX_URL    = "https://api.twitch.tv/helix"
TOKEN_URL    = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

IS_TTY = sys.stdout.isatty()  # False under systemd — switches to plain log output

# ── Logging: never print a secret ─────────────────────────────────────────────

SECRETS: set[str] = set()  # Every credential seen so far, refreshed tokens included

def remember_secrets(*values: str | None):
    SECRETS.update(v for v in values if v and len(v) >= 6)

def redact(text: str) -> str:
    for secret in SECRETS:
        text = text.replace(secret, "***")
    return text

class _RedactSecrets(logging.Filter):
    """Scrub known secrets from library log lines and their tracebacks before they reach the journal."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = record.exc_text = None
        record.msg, record.args = redact(msg), None
        return True

def setup_logging():
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RedactSecrets())
    handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    # twitchio logs the raw token when Twitch rejects a login; the bot logs its own token-free line instead
    logging.getLogger("twitchio.websocket").addFilter(
        lambda record: not str(record.msg).startswith("Login unsuccessful with token"))
    # ...and then trips over its own cancelled task while closing. Harmless, but it prints a scary traceback.
    logging.getLogger("asyncio").addFilter(lambda record: not (
        "WSConnection._task_callback" in str(record.msg)
        and record.exc_info and record.exc_info[0] is asyncio.CancelledError))

DIGEST_AT: tuple[int, int] | None = None  # Parsed DIGEST_TIME, set by check_config()
TZ: ZoneInfo | None = None                # Parsed TIMEZONE (None = server's local time)

# ── Discord ───────────────────────────────────────────────────────────────────

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

def fix_hint() -> str:
    """What to run after fixing credentials: the installer under systemd, otherwise a plain restart."""
    if os.getenv("INVOCATION_ID"):  # Set by systemd
        return "sudo bash /opt/lurkme/deploy/install.sh --reconfigure"
    return "Update your .env with new credentials, then start the bot again."

def die(msg: str, alert: bool = True):
    """Exit with EXIT_CONFIG, which tells systemd (RestartPreventExitStatus) not to restart.
    With a webhook configured, a red "needs you" card goes to Discord first."""
    print(msg, file=sys.stderr, flush=True)
    if alert and DISCORD_WEBHOOK_URL:
        try:
            send_discord(cards.attention_card(msg, fix_hint(), DISCORD_USER_ID or None,
                                              command=bool(os.getenv("INVOCATION_ID"))))
        except Exception as e:
            print(f"[discord] Couldn't send the alert ({type(e).__name__})", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_CONFIG)

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
        except OSError as e:  # Includes every requests error, plus TLS/CA-bundle problems
            print(f"[auth] Can't reach Twitch, retrying in {delay}s: {e}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)

def check_config():
    """Validate the optional settings (dropping bad ones with a warning), then log the setup."""
    global CHANNELS, LANGUAGES, MAX_CHANNELS, DISCORD_WEBHOOK_URL, DISCORD_USER_ID
    global DIGEST_AT, TZ, TIMEZONE, DASHBOARD_PORT, DASHBOARD_HOST
    for name, values, pattern in (("CHANNELS", CHANNELS, LOGIN_RE), ("STREAM_LANGUAGES", LANGUAGES, LANGUAGE_RE)):
        bad = [v for v in values if not pattern.fullmatch(v)]
        if bad:
            print(f"[config] Ignoring invalid {name}: {', '.join(bad)}", flush=True)
    CHANNELS  = [c for c in CHANNELS if LOGIN_RE.fullmatch(c)]
    LANGUAGES = [lang for lang in LANGUAGES if LANGUAGE_RE.fullmatch(lang)] or ["en"]

    if not 1 <= MAX_CHANNELS <= TWITCH_CHANNEL_LIMIT:
        print(f"[config] MAX_CHANNELS must be 1–{TWITCH_CHANNEL_LIMIT} (Twitch's limit) — using {TWITCH_CHANNEL_LIMIT}", flush=True)
        MAX_CHANNELS = TWITCH_CHANNEL_LIMIT
    if len(CHANNELS) > MAX_CHANNELS:
        print(f"[config] Only the first {MAX_CHANNELS} CHANNELS fit — the rest are ignored", flush=True)

    if DISCORD_WEBHOOK_URL and not WEBHOOK_RE.fullmatch(DISCORD_WEBHOOK_URL):
        print("[config] Ignoring DISCORD_WEBHOOK_URL — it isn't a Discord webhook URL", flush=True)  # Never print it
        DISCORD_WEBHOOK_URL = ""
    if DISCORD_USER_ID and not USER_ID_RE.fullmatch(DISCORD_USER_ID):
        print("[config] Ignoring DISCORD_USER_ID — it must be the numeric user ID, not a username (see README.md)", flush=True)
        DISCORD_USER_ID = ""

    match = TIME_RE.fullmatch(DIGEST_TIME)
    DIGEST_AT = (int(match[1]), int(match[2])) if match else None
    if not match and DIGEST_TIME.lower() not in ("off", "no", "false", ""):
        print("[config] DIGEST_TIME must look like 21:00 or be off — digest disabled", flush=True)
    TZ = None
    if TIMEZONE:
        try:
            TZ = ZoneInfo(TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"[config] Unknown TIMEZONE {TIMEZONE!r} — using the server's time", flush=True)
            TIMEZONE = ""  # So logs and the digest card say "server time" too

    if not 0 <= DASHBOARD_PORT <= 65535:
        print("[config] DASHBOARD_PORT must be 0–65535 — dashboard disabled", flush=True)
        DASHBOARD_PORT = 0
    if DASHBOARD_PORT and not DASHBOARD_PASSWORD and DASHBOARD_HOST not in ("127.0.0.1", "localhost", "::1"):
        print("[config] The dashboard needs DASHBOARD_PASSWORD to listen beyond this machine — using 127.0.0.1", flush=True)
        DASHBOARD_HOST = "127.0.0.1"

    discord = ("on, mentioning you" if DISCORD_USER_ID else "on") if DISCORD_WEBHOOK_URL else "off"
    digest  = f"{DIGEST_AT[0]:02d}:{DIGEST_AT[1]:02d} {TIMEZONE or 'server time'}" if DIGEST_AT and DISCORD_WEBHOOK_URL else "off"
    board   = f"{DASHBOARD_HOST}:{DASHBOARD_PORT}" if DASHBOARD_PORT else "off"
    print(f"[config] Max channels: {MAX_CHANNELS}  |  Pinned: {len(CHANNELS)}  |  Languages: {', '.join(LANGUAGES)}"
          f"  |  Categories: {', '.join(CATEGORIES) or 'all'}", flush=True)
    print(f"[config] Discord alerts: {discord}  |  Daily digest: {digest}  |  Dashboard: {board}", flush=True)

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

def _to_int(value: str | None, default: int) -> int:
    return int(value) if value and value.isdigit() else default

def _stream_info(stream: dict, source: str) -> dict:
    """The dashboard's view of one live stream from Helix."""
    return {
        "display_name":  stream.get("user_name") or stream.get("user_login"),
        "game":          stream.get("game_name") or None,
        "viewers":       stream.get("viewer_count"),
        "started_at":    stream.get("started_at"),
        "title":         stream.get("title"),
        "thumbnail_url": stream.get("thumbnail_url"),
        "source":        source,
        "live":          True,
    }

# ── Bot ───────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    """State that outlives the bot's in-process restarts (one per process)."""
    store:         StatsStore
    started_at:    float = field(default_factory=time.time)
    previous_seen: float | None = None   # When the previous process was last alive (from the stats DB)
    restarts:      int = 0
    gifted_subs:   int = 0
    alerts:        list = field(default_factory=list)  # Discord messages not yet delivered
    announced:     bool = False
    down_since:    float | None = None   # When the last run ended, for the "back online" card after restarts
    last_digest:   str | None = None     # Also kept in memory so a failed DB write can't repeat the digest
    last_target:   list = field(default_factory=list)  # Last good channel list, used while Helix is down

class LurkerBot(commands.Bot):

    # Don't dispatch an event for every chat message — only USERNOTICEs matter here
    event_message = None

    def __init__(self, token: str, refresh_token: str, token_info: dict, can_refresh: bool, session: Session):
        super().__init__(token=token, prefix="!", initial_channels=[])
        # twitchio keeps every chatter it sees until the channel is parted, which
        # leaks memory across 100 busy chats. Nothing here reads that cache.
        self._connection._cache_add = lambda parsed: None

        self.session       = session
        self.store         = session.store
        self.user_token    = token
        self.refresh_token = refresh_token
        self.can_refresh   = can_refresh
        self.api_client_id = token_info["client_id"]  # Helix needs the client ID that issued the token
        self.account_id    = token_info["user_id"]
        self.has_follows   = SCOPE_FOLLOWS in (token_info.get("scopes") or [])
        self.token_expires = time.time() + token_info["expires_in"] if token_info.get("expires_in") else None
        self.joined        = set()
        self.channel_info: dict[str, dict] = {}   # login -> what the dashboard shows
        self.fatal         = False
        self.fatal_reason  = ""
        self._stopping     = False
        self._last_data    = time.monotonic()
        self._sync_lock    = asyncio.Lock()
        self._resync       = asyncio.Event()
        self._check_token  = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._category_ids: list[str] | None = None if CATEGORIES else []
        self.alerts: asyncio.Queue[dict] = asyncio.Queue()  # Discord messages waiting to be sent
        self._alert_in_flight: dict | None = None
        for item in session.alerts:
            self.alerts.put_nowait(item)

    @property
    def gifted_subs(self) -> int:
        return self.session.gifted_subs

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
            asyncio.create_task(self._housekeeping()),
        ]
        if DISCORD_WEBHOOK_URL:
            self._tasks.append(asyncio.create_task(self._send_alerts()))

        board = None
        if DASHBOARD_PORT:
            try:
                app   = dashboard.create_app(self.status_snapshot, DASHBOARD_PASSWORD or None, get_health=self.is_connected)
                board = await dashboard.start_dashboard(app, DASHBOARD_HOST, DASHBOARD_PORT)
                if not self.session.restarts:
                    self._log(f"[dashboard] Listening on http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
            except Exception as e:  # A bad port or host name must never keep the bot out of chat
                problem = getattr(e, "strerror", None) or e
                self._log(f"[dashboard] Couldn't listen on {DASHBOARD_HOST}:{DASHBOARD_PORT} ({problem})")
        try:
            await super().start()
        finally:
            if board:
                with contextlib.suppress(Exception):
                    await dashboard.stop_dashboard(board)

    async def _stop(self, reason: str, fatal: bool):
        """Disconnect and end start(); main() then exits (fatal) or starts a fresh bot."""
        if self._stopping:
            return
        self._stopping    = True
        self.fatal        = fatal
        self.fatal_reason = reason
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

    async def _housekeeping(self):
        """Every minute: record lurk time and a heartbeat; refresh dashboard info; send the digest when due."""
        last_tick = time.monotonic()
        next_info = last_tick + INFO_INTERVAL
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            now       = time.monotonic()
            elapsed   = min(now - last_tick, 2 * STATS_INTERVAL)
            last_tick = now
            try:
                # Database work runs in a thread: a locked or slow disk must not stall the chat connection
                lurked = {ch: elapsed for ch in self.joined}
                await asyncio.to_thread(self._record_tick, lurked)
                if DASHBOARD_PORT and now >= next_info:  # Viewer counts only matter for the dashboard
                    next_info = now + INFO_INTERVAL
                    await self._refresh_channel_info()
                card = await asyncio.to_thread(self._digest_card)
                if card:
                    self._queue_card("digest", card)
            except Exception as e:
                self._log(f"[stats] Housekeeping failed: {type(e).__name__}: {e}")

    # ── Token upkeep ──────────────────────────────────────────────────────────

    def _set_user_token(self, token: str):
        remember_secrets(token, self.refresh_token)
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
        if info is not None:
            self.token_expires = time.time() + expires_in if expires_in else None
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

    def _by_login(self, path: str, key: str, logins: list[str]) -> dict[str, dict]:
        """Look up streams or users for many logins, 100 per request. Returns login -> object."""
        found = {}
        for i in range(0, len(logins), 100):
            params = [(key, login) for login in logins[i:i + 100]]
            if path == "streams":
                params.append(("first", 100))  # Streams default to 20 per page
            for item in self._helix_get(path, params).get("data", []):
                found[(item.get("user_login") or item.get("login") or "").lower()] = item
        return found

    def _get_live_followed_channels(self) -> list[dict]:
        streams, cursor, seen = [], None, set()
        for _ in range(20):  # A cursor that never advances must not spin forever
            params = {"user_id": self.account_id, "first": 100}
            if cursor:
                params["after"] = cursor
            data    = self._helix_get("streams/followed", params)
            streams += data.get("data", [])
            cursor  = data.get("pagination", {}).get("cursor")
            if not cursor or cursor in seen or len(streams) >= MAX_CHANNELS:
                break
            seen.add(cursor)
        return streams

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

    def _get_top_streamers(self) -> list[dict]:
        params = [("first", 100)]
        if "any" not in LANGUAGES:
            params += [("language", lang) for lang in LANGUAGES]
        params += [("game_id", gid) for gid in self._get_category_ids()]
        return self._helix_get("streams", params).get("data", [])

    # ── Channel management ────────────────────────────────────────────────────

    def _build_target_list(self) -> tuple[list[str], dict[str, dict]]:
        """Pinned channels first, then live followed channels, then top streams to fill.
        Also returns what the dashboard shows for each of them."""
        pinned   = CHANNELS[:MAX_CHANNELS]
        followed = self._get_live_followed_channels() if self.has_follows else []
        info     = {s["user_login"]: _stream_info(s, "followed") for s in followed}
        mine     = list(dict.fromkeys(pinned + list(info)))
        top      = self._get_top_streamers() if len(mine) < MAX_CHANNELS else []
        if not top and len(mine) < MAX_CHANNELS and len(self.joined) > len(mine):
            # We're in top-stream channels now; an empty list is a Helix glitch, not a reason to leave them all
            raise RuntimeError("Twitch returned no live streams")
        for s in top:
            info.setdefault(s["user_login"], _stream_info(s, "top"))
        merged = list(dict.fromkeys(mine + [s["user_login"] for s in top]))[:MAX_CHANNELS]

        # Pinned channels may be offline; look them up, then fetch everyone's avatar (best effort)
        with contextlib.suppress(Exception):
            for login, stream in self._by_login("streams", "user_login", [c for c in pinned if c not in info]).items():
                info[login] = _stream_info(stream, "pinned")
        for login in pinned:
            info.setdefault(login, {"display_name": login, "source": "pinned", "live": False})
            info[login]["source"] = "pinned"
        with contextlib.suppress(Exception):
            for login, user in self._by_login("users", "login", merged).items():
                if login in info:
                    info[login]["display_name"]      = user.get("display_name") or info[login].get("display_name")
                    info[login]["profile_image_url"] = user.get("profile_image_url")

        n_pinned   = len(pinned)
        n_followed = min(len(mine), MAX_CHANNELS) - n_pinned
        self._log(f"[sync] {n_pinned} pinned  |  {n_followed} followed live  |  "
                  f"{len(merged) - n_pinned - n_followed} top streams to fill")
        return merged, {login: info[login] for login in merged if login in info}

    async def _refresh_channel_info(self):
        """Update viewer counts, games and live status for the dashboard (one Helix call per 100 channels)."""
        logins = sorted(self.joined)
        if not logins:
            return
        try:
            live = await asyncio.to_thread(self._by_login, "streams", "user_login", logins)
        except Exception:
            return  # Stale numbers on the dashboard are fine; the next sync retries
        for login in logins:
            entry = self.channel_info.setdefault(login, {"display_name": login, "source": "top"})
            if login in live:
                fresh = _stream_info(live[login], entry.get("source", "top"))
                fresh["profile_image_url"] = entry.get("profile_image_url")
                entry.update(fresh)
            else:
                entry["live"], entry["viewers"] = False, None

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
                target, info = await asyncio.to_thread(self._build_target_list)
                self.session.last_target, self.channel_info = target, info
            except Exception as e:  # Never leave the bot sitting in zero channels over one bad response
                self._log(f"[sync] Twitch API error, retrying in {RETRY_DELAY}s: {type(e).__name__}: {e}")
                if getattr(getattr(e, "response", None), "status_code", None) == 401:
                    self._check_token.set()  # Token died early — refresh it now
                self.loop.call_later(RETRY_DELAY, self._resync.set)
                if self.joined:
                    return  # Keep the channels we're in until Helix answers again
                # A fresh connection during the outage: rejoin the last good list, or at least the pinned channels
                target = self.session.last_target or CHANNELS[:MAX_CHANNELS]
                if not target:
                    return
                self._log(f"[sync] Rejoining {len(target)} channels from the last good list meanwhile")

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

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connection.is_alive and time.monotonic() - self._last_data < STALL_TIMEOUT

    def status_snapshot(self) -> dict:
        """Everything the dashboard shows (cached for a couple of seconds). No secrets: no tokens, no webhook URL."""
        cached = getattr(self, "_snapshot", None)
        if cached and time.monotonic() - cached[0] < 2:
            return cached[1]
        snapshot = self._build_snapshot()
        self._snapshot = (time.monotonic(), snapshot)
        return snapshot

    def _build_snapshot(self) -> dict:
        now   = time.time()
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lurk  = self.store.lurk_today(now)
        idle  = time.monotonic() - self._last_data
        channels = []
        for login in sorted(self.joined):
            info = self.channel_info.get(login, {})
            channels.append({
                "login":              login,
                "display_name":       info.get("display_name") or login,
                "game":               info.get("game"),
                "viewers":            info.get("viewers"),
                "started_at":         info.get("started_at"),
                "title":              info.get("title"),
                "thumbnail_url":      info.get("thumbnail_url"),
                "profile_image_url":  info.get("profile_image_url"),
                "source":             info.get("source", "top"),
                "live":               bool(info.get("live")),
                "lurk_seconds_today": lurk.get(login, 0.0),
            })
        return {
            "generated_at": now,
            "bot": {
                "nick":               self.nick,
                "started_at":         self.session.started_at,
                "connected":          self.is_connected(),
                "last_data_age":      idle,
                "token_expires_in":   self.token_expires - now if self.token_expires else None,
                "token_auto_refresh": self.can_refresh,
                "max_channels":       MAX_CHANNELS,
                "languages":          LANGUAGES,
                "categories":         CATEGORIES,
                "discord":            bool(DISCORD_WEBHOOK_URL),
                "restarts":           self.session.restarts,
                "stats_saved":        self.store.persistent,
            },
            "channels":     channels,
            "stats":        {"today": self.store.summary(since=today), "all_time": self.store.summary()},
            "recent_gifts": self.store.recent_gifts(20),
        }

    # ── Events ────────────────────────────────────────────────────────────────

    async def event_ready(self):
        self._log(f"[ready] Logged in as {self.nick}")
        first = not self.session.announced
        self.session.announced = True
        await self._sync_channels(reset=True)
        if first:
            self._announce_online(self.session.previous_seen)  # Since the previous process was last alive
        elif self.session.down_since:
            self._announce_online(self.session.down_since)     # After an in-process restart
        self.session.down_since = None

    async def event_error(self, error: Exception, data: str | None = None):
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self._log(redact(f"[error] {text.rstrip()}"))

    async def event_raw_data(self, data):
        self._last_data = time.monotonic()
        # Close frames arrive here as an int close code, hence the isinstance check
        if not isinstance(data, str):
            return
        # Match whole server lines only: chat messages can contain the same words
        if any(line.startswith(LOGIN_FAILED) for line in data.split("\r\n")):
            # twitchio would otherwise reconnect in a tight loop. A restart re-validates
            # (and refreshes) the token, and exits if it's really dead.
            await asyncio.sleep(0)  # Let twitchio finish handling this line before we disconnect
            await self._stop("[auth] Twitch rejected the chat login, restarting", fatal=False)

    async def event_channel_join_failure(self, channel: str):
        self.joined.discard(channel)
        self._log(f"[join] Timed out joining #{channel}")

    async def event_raw_usernotice(self, channel, tags: dict):
        msg_id = tags.get("msg-id")
        now    = time.time()
        if msg_id in ("submysterygift", "anonsubmysterygift"):  # A community gift drop in a channel we're in
            count = _to_int(tags.get("msg-param-mass-gift-count"), 1)
            self.store.record_drop(ts=now, channel=channel.name, kind="community", count=count)
            return
        if msg_id not in ("subgift", "anonsubgift"):
            return
        if not tags.get("msg-param-community-gift-id"):  # Part of a drop already counted above otherwise
            self.store.record_drop(ts=now, channel=channel.name, kind="single", count=1)
        if tags.get("msg-param-recipient-id") != self.account_id:
            return

        anonymous = msg_id == "anonsubgift" or tags.get("login") == "ananonymousgifter"
        gifter    = "An anonymous gifter" if anonymous else (tags.get("display-name") or tags.get("login") or "Someone")
        plan      = tags.get("msg-param-sub-plan", "1000")
        months    = _to_int(tags.get("msg-param-gift-months"), 1)
        self.session.gifted_subs += 1
        lifetime = self.store.record_gift(ts=now, channel=channel.name, gifter=gifter, plan=plan, months=months)
        self._log(f"[gift] {gifter} gifted you a sub in #{channel.name}! "
                  f"(this run: {self.gifted_subs}, all-time: {lifetime if lifetime is not None else '?'})")

        if DISCORD_WEBHOOK_URL:
            self._enqueue({"kind": "gift", "lifetime": lifetime, "gift": {
                "channel": channel.name,
                "room_id": tags.get("room-id", ""),
                "gifter":  gifter,
                "plan":    plan,
                "months":  months,
                "total":   self.gifted_subs,
                "time":    datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            }})

    # ── Discord ───────────────────────────────────────────────────────────────

    def _enqueue(self, item: dict):
        if DISCORD_WEBHOOK_URL:
            self.alerts.put_nowait({**item, "queued_at": time.time()})
            self._save_outbox()

    def _queue_card(self, label: str, payload: dict):
        self._enqueue({"kind": "card", "label": label, "payload": payload})

    def _save_outbox(self):
        """Keep undelivered alerts in the stats DB so they survive service restarts and updates too."""
        pending = ([self._alert_in_flight] if self._alert_in_flight else []) + list(self.alerts._queue)
        self.store.set_meta("outbox", json.dumps(pending))

    def _record_tick(self, lurked: dict[str, float]):
        if lurked:
            self.store.add_presence(lurked, time.time())
        self.store.set_meta("last_seen", str(time.time()))

    def _announce_online(self, since: float | None):
        """A green card when the bot comes back after real downtime (or on its very first start)."""
        down = time.time() - since if since else None
        if down is not None and down < ONLINE_AFTER_DOWN:
            return
        hint = f"http://localhost:{DASHBOARD_PORT} (via SSH tunnel)" if DASHBOARD_PORT and not DASHBOARD_PASSWORD \
            else (f"port {DASHBOARD_PORT}" if DASHBOARD_PORT else None)
        self._queue_card("online", cards.online_card(self.nick or "?", len(self.joined), MAX_CHANNELS, down, hint))

    def _digest_card(self) -> dict | None:
        """Once a day at DIGEST_TIME: a summary card of the last 24 hours. Runs in a worker thread."""
        if not (DIGEST_AT and DISCORD_WEBHOOK_URL):
            return None
        local  = datetime.now(TZ) if TZ else datetime.now().astimezone()
        today  = local.date().isoformat()
        due    = (local.hour, local.minute) >= DIGEST_AT
        # The in-memory copy stops a repeat every minute if the DB can't be written; >= copes with the clock going back
        last = max(filter(None, (self.store.get_meta("last_digest"), self.session.last_digest)), default=None)
        if last is not None and (not due or last >= today):
            return None
        all_time = self.store.summary()
        lurked   = all_time["lurk_seconds"]
        if last is None:  # First run ever: today's digest is still to come only if its time hasn't passed
            self.session.last_digest = today if due else (local.date() - timedelta(days=1)).isoformat()
            self.store.set_meta("last_digest", self.session.last_digest)
            self.store.set_meta("digest_lurk_total", str(lurked))
            return None
        self.session.last_digest = today
        self.store.set_meta("last_digest", today)

        # Lurk time is stored per UTC day, so "since 24h ago" would count up to two whole days.
        # The difference between all-time totals at each digest is exact.
        period = self.store.summary(since=time.time() - 86400)
        try:
            period["lurk_seconds"] = max(0.0, lurked - float(self.store.get_meta("digest_lurk_total") or 0))
        except ValueError:
            pass
        self.store.set_meta("digest_lurk_total", str(lurked))
        return cards.digest_card(self.nick or "?", period, all_time, len(self.joined), MAX_CHANNELS,
                                 time.time() - self.session.started_at, TIMEZONE or "server time")

    def pending_alerts(self) -> list[dict]:
        """Messages not yet delivered, including one cut off mid-send — handed to the next run on restart."""
        pending = [self._alert_in_flight] if self._alert_in_flight else []
        while not self.alerts.empty():
            pending.append(self.alerts.get_nowait())
        return pending

    def _channel_card(self, gift: dict) -> tuple[str, str | None]:
        """The channel's display name and avatar for a gift card. Best effort: falls back to the login name."""
        info = self.channel_info.get(gift["channel"], {})
        if info.get("profile_image_url"):
            return info.get("display_name") or gift["channel"], info["profile_image_url"]
        try:
            users = self._helix_get("users", {"id": gift["room_id"]}).get("data", []) if gift["room_id"] else []
            if users:
                return users[0].get("display_name") or gift["channel"], users[0].get("profile_image_url") or None
        except Exception:
            pass
        return gift["channel"], None

    async def _send_alerts(self):
        while True:
            item = self._alert_in_flight = await self.alerts.get()
            try:
                if item["kind"] == "gift":
                    gift    = item["gift"]
                    label   = f"gift alert for #{gift['channel']}"
                    name, avatar = await asyncio.to_thread(self._channel_card, gift)
                    payload = cards.gift_card(gift, name, avatar, item.get("lifetime"), DISCORD_USER_ID or None)
                else:
                    label, payload = f"{item['label']} card", item["payload"]
            except Exception as e:  # One bad item must not stop every later alert
                self._log(f"[discord] Skipped an alert that couldn't be built: {type(e).__name__}: {e}")
                self._alert_in_flight = None
                self._save_outbox()
                continue
            # Keep retrying through a Discord outage (backoff up to 10 min), but not forever
            give_up = (item.get("queued_at") or time.time()) + ALERT_MAX_AGE
            attempt = 0
            while True:
                attempt += 1
                try:
                    await asyncio.to_thread(send_discord, payload)
                    self._log(f"[discord] Sent the {label}")
                    break
                except requests.HTTPError as e:
                    status = getattr(e.response, "status_code", None)
                    if status and 400 <= status < 500 and status != 429:
                        self._log(f"[discord] Webhook rejected the {label} (HTTP {status}) — check DISCORD_WEBHOOK_URL")
                        break
                    problem = f"HTTP {status}"
                except Exception as e:
                    problem = type(e).__name__  # Not str(e): requests errors include the webhook URL
                if time.time() >= give_up:
                    self._log(f"[discord] Gave up on the {label} after {attempt} tries")
                    break
                delay = min(600, 5 * 2 ** min(attempt, 7))
                if attempt <= 3 or attempt % 10 == 0:
                    self._log(f"[discord] Couldn't send the {label} ({problem}), retrying in {delay}s")
                await asyncio.sleep(delay)
            self._alert_in_flight = None
            self._save_outbox()

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
        die("[discord] DISCORD_WEBHOOK_URL isn't set — see README.md", alert=False)
    try:
        send_discord(cards.test_card(DISCORD_USER_ID or None))
    except requests.HTTPError as e:
        die(f"[discord] Discord rejected the test alert (HTTP {getattr(e.response, 'status_code', '?')}) — "
            "check DISCORD_WEBHOOK_URL", alert=False)
    except OSError as e:
        die(f"[discord] Couldn't reach Discord ({type(e).__name__})", alert=False)  # Not str(e): it includes the URL
    print("[discord] Test alert sent — check your Discord", flush=True)

def _load_outbox(store: StatsStore) -> list[dict]:
    """Discord alerts a previous process couldn't deliver."""
    try:
        items = json.loads(store.get_meta("outbox") or "[]")
        return [item for item in items if isinstance(item, dict) and item.get("kind") in ("gift", "card")]
    except (ValueError, TypeError):
        return []

def main():
    setup_logging()
    remember_secrets(OAUTH_TOKEN.removeprefix("oauth:"), REFRESH_TOKEN, CLIENT_SECRET, DISCORD_WEBHOOK_URL,
                     DASHBOARD_PASSWORD)
    if sys.argv[1:] == ["--test-discord"]:
        send_test_alert()
        return
    check_config()  # First, so even a startup failure can alert Discord
    if not OAUTH_TOKEN:
        die("OAUTH_TOKEN is not set — see README.md")

    store = StatsStore(os.path.join(DATA_DIR, "lurkme.db"), log=lambda msg: print(msg, flush=True))
    if not store.persistent:
        print("[stats] Stats aren't being saved to disk, so they reset on restart. "
              "On a VPS, run the installer again: sudo bash /opt/lurkme/deploy/install.sh", flush=True)
    seen    = store.get_meta("last_seen")
    session = Session(store=store, previous_seen=float(seen) if seen else None, alerts=_load_outbox(store))

    token         = OAUTH_TOKEN.removeprefix("oauth:")
    refresh_token = REFRESH_TOKEN
    can_refresh   = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)
    delay         = RESTART_DELAY_MIN
    checked       = False

    while True:
        try:
            token, refresh_token, info = get_valid_token(token, refresh_token, can_refresh)
        except AuthError as e:
            die(f"[auth] {e} — see README.md")
        if not checked:
            can_refresh = check_token(info, can_refresh)
            checked     = True

        asyncio.set_event_loop(asyncio.new_event_loop())  # Each run gets a fresh loop
        bot     = LurkerBot(token, refresh_token, info, can_refresh, session)
        started = time.monotonic()
        run_bot(bot)
        session.alerts = bot.pending_alerts()
        if bot.fatal:
            die(bot.fatal_reason or "[auth] The bot stopped because its credentials need fixing — see README.md")

        # Carry state into the next run — the token may have been refreshed meanwhile
        token, refresh_token = bot.user_token, bot.refresh_token
        session.restarts += 1
        if session.down_since is None:
            session.down_since = time.time()
        if time.monotonic() - started >= HEALTHY_RUN:
            delay = RESTART_DELAY_MIN
        print(f"[main] Restarting in {delay}s...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, RESTART_DELAY_MAX)

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
