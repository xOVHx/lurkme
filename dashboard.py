"""
Dashboard
---------
A live web page showing what lurkme is doing: the channels it's in (stream
previews, game, viewers, how long it has lurked), the subs gifted to you, the
gift drops it has seen, your odds, and the bot's health.

Served with aiohttp (installed with twitchio). Everything on the page comes
from Twitch and is treated as hostile: the page builds its DOM with
textContent only, loads images only from Twitch's CDN, links only to twitch.tv
channel pages, and a strict Content-Security-Policy backs all of that up.
With a password, every route except /healthz sits behind HTTP Basic auth;
without one, only localhost and IP-address URLs are served (no DNS rebinding).

Try it with made-up data:  python dashboard.py --demo [PORT]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import math
import os
import random
import time
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Union

from aiohttp import web
from aiohttp.http_exceptions import HttpProcessingError

StatusGetter = Callable[[], Union[dict, Awaitable[dict]]]

REALM = "lurkme"
CSP   = ("default-src 'none'; script-src 'self'; style-src 'self'; "
         "img-src 'self' https://static-cdn.jtvnw.net data:; connect-src 'self'; "
         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

SECURITY_HEADERS = {
    "Content-Security-Policy":      CSP,
    "X-Content-Type-Options":       "nosniff",
    "Referrer-Policy":              "no-referrer",
    "Cache-Control":                "no-store",
    "X-Frame-Options":              "DENY",
    "Cross-Origin-Opener-Policy":   "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy":           "camera=(), microphone=(), geolocation=()",
    "Server":                       "lurkme",  # Instead of aiohttp's name and version
}

AUTH_MAX_FAILURES = 10    # Wrong passwords from one address within AUTH_WINDOW...
AUTH_WINDOW       = 300   # ...lock that address out until the window ends
AUTH_MAX_TRACKED  = 4096  # Addresses remembered at once (bounds memory under a spray)
JSON_MAX_DEPTH    = 32
LOG_MAX_DISTINCT  = 20    # Distinct status errors logged before going quiet

# ── Helpers ───────────────────────────────────────────────────────────────────

def _print(msg: str):
    print(msg, flush=True)

def _password_from(header: str) -> bytes | None:
    """The password in an "Authorization: Basic ..." header (any username), or None if it's malformed."""
    scheme, _, encoded = header.strip().partition(" ")
    encoded = encoded.strip()
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):  # Bad padding/alphabet, or non-ASCII text
        return None
    _user, colon, password = decoded.partition(b":")
    return password if colon else None

def _is_local_host(host: str | None) -> bool:
    """True for localhost names and IP literals: Host values a DNS-rebinding web page can't send."""
    host = (host or "").strip().lower()
    if host.startswith("["):                 # [::1]:8787
        name = host[1:].partition("]")[0]
    elif host.count(":") == 1:               # 127.0.0.1:8787 or localhost:8787
        name = host.partition(":")[0]
    else:                                    # localhost, or a bare IPv6 address
        name = host
    name = name.rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False

def _plain(value: Any, depth: int = 0) -> Any:
    """Status data as strict JSON values: no NaN/Infinity, and nothing that isn't plain data."""
    if depth > JSON_MAX_DEPTH:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_plain(item, depth + 1) for item in sorted(value, key=str)]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None  # Never str() an unknown object: its repr could hold something private

def _to_json(status: dict) -> str:
    return json.dumps(_plain(status), allow_nan=False, separators=(",", ":"))

class _Lockout:
    """Counts wrong passwords per client address, so a public dashboard can't be brute-forced.
    Only different attempts count: a stale browser tab resending an old password is one failure, not one per poll."""

    def __init__(self, max_failures: int = AUTH_MAX_FAILURES, window: float = AUTH_WINDOW,
                 clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.window       = window
        self.clock        = clock
        self._key         = os.urandom(16)  # Attempts are kept as keyed hashes, never as the text itself
        self._failures: dict[str, tuple[int, float, set[bytes]]] = {}  # address -> (count, first failure, attempts)

    def retry_after(self, addr: str) -> float:
        """Seconds until addr may try again; 0 if it isn't locked out."""
        entry = self._failures.get(addr)
        if entry is None:
            return 0.0
        count, since, _seen = entry
        left = since + self.window - self.clock()
        if left <= 0:
            del self._failures[addr]
            return 0.0
        return left if count >= self.max_failures else 0.0

    def failed(self, addr: str, attempt: str | None = None):
        """Count a wrong attempt (the Authorization header) from addr, unless it already counted in this window."""
        now = self.clock()
        count, since, seen = self._failures.get(addr, (0, now, set()))
        if now - since >= self.window:
            count, since, seen = 0, now, set()
        if attempt is not None:
            digest = hashlib.blake2b(attempt.encode("utf-8", "surrogateescape"), digest_size=16, key=self._key).digest()
            if digest in seen:
                return
            seen.add(digest)
        self._failures[addr] = (count + 1, since, seen)
        if len(self._failures) > AUTH_MAX_TRACKED:
            self._prune(now)

    def succeeded(self, addr: str):
        self._failures.pop(addr, None)

    def _prune(self, now: float):
        self._failures = {a: e for a, e in self._failures.items() if now - e[1] < self.window}
        if len(self._failures) > AUTH_MAX_TRACKED:  # Still full: keep the newest half
            newest = sorted(self._failures.items(), key=lambda item: item[1][1])[AUTH_MAX_TRACKED // 2:]
            self._failures = dict(newest)

# ── App ───────────────────────────────────────────────────────────────────────

async def _add_security_headers(request: web.Request, response: web.StreamResponse):
    """Runs for every response the app makes, errors included (401, 403, 404, 405, 429, 500). The one exception is
    a request aiohttp can't even parse (bad bytes in the URL, a header over 8 KB): its parser answers with a plain-text
    400 and closes the connection before the app sees the request, so that reply goes out without these headers."""
    response.headers.update(SECURITY_HEADERS)

def create_app(get_status: StatusGetter, password: str | None = None,
               log: Callable[[str], None] = _print, get_health: Callable[[], bool] | None = None) -> web.Application:
    """The dashboard. get_status() returns the status dict (LurkerBot.status_snapshot); it may also be async.
    With a password, everything but /healthz needs HTTP Basic auth (any username).
    get_health() is a cheap "connected?" check for the unauthenticated /healthz; without it /healthz reads the full status."""
    secret  = password.encode("utf-8") if password else None
    lockout = _Lockout()
    logged: set[str] = set()

    def note(error: Exception):
        """Log a status failure once per distinct message — never sent to the browser."""
        msg = f"{type(error).__name__}: {error}"
        if msg not in logged and len(logged) < LOG_MAX_DISTINCT:
            logged.add(msg)
            log(f"[dashboard] Couldn't build the status: {msg}")

    async def read_status() -> dict:
        status = get_status()
        if inspect.isawaitable(status):
            status = await status
        if not isinstance(status, dict):
            raise TypeError(f"get_status() returned {type(status).__name__}, not a dict")
        return status

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if secret is None:
            # No password means "this machine only". A page on some other site can point its own
            # domain at 127.0.0.1 (DNS rebinding); refusing non-local Host headers stops it reading this.
            if not _is_local_host(request.host):
                raise web.HTTPForbidden(text="Open the dashboard through localhost, or set DASHBOARD_PASSWORD.")
            return await handler(request)

        addr = request.remote or "?"
        wait = lockout.retry_after(addr)
        if wait:
            raise web.HTTPTooManyRequests(text="Too many wrong passwords. Try again in a few minutes.",
                                          headers={"Retry-After": str(math.ceil(wait))})
        header = request.headers.get("Authorization")
        given  = _password_from(header) if header is not None else None
        if given is not None and hmac.compare_digest(given, secret):
            lockout.succeeded(addr)
            return await handler(request)
        if header is not None:  # A browser's first request has no header; only real attempts count
            lockout.failed(addr, header)
        raise web.HTTPUnauthorized(text="This dashboard needs a password.",
                                   headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})

    def asset(body: str, content_type: str):
        async def handler(request: web.Request) -> web.Response:
            response = web.Response(text=body, content_type=content_type)
            response.enable_compression()
            return response
        return handler

    async def api_status(request: web.Request) -> web.Response:
        try:
            body = _to_json(await read_status())
        except Exception as e:
            note(e)
            return web.json_response({"error": "status unavailable"}, status=500)
        response = web.Response(text=body, content_type="application/json")
        response.enable_compression()
        return response

    async def healthz(request: web.Request) -> web.Response:
        try:
            if get_health is not None:  # No database work for an endpoint anyone can hit
                connected = get_health() is True
            else:
                bot       = (await read_status()).get("bot")
                connected = isinstance(bot, dict) and bot.get("connected") is True
        except Exception as e:
            note(e)
            connected = False
        return web.Response(text="ok" if connected else "disconnected", status=200 if connected else 503)

    app = web.Application(middlewares=[guard])
    app.on_response_prepare.append(_add_security_headers)
    app.router.add_get("/",           asset(PAGE_HTML, "text/html"))
    app.router.add_get("/app.js",     asset(APP_JS, "application/javascript"))
    app.router.add_get("/app.css",    asset(APP_CSS, "text/css"))
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/healthz",    healthz)
    return app

def _not_a_bad_request(record: logging.LogRecord) -> bool:
    """Drops aiohttp's traceback for a request it couldn't parse: that's the client's problem (a scanner, garbage on
    the port), and on a public dashboard it would fill the bot's log. Real errors still get logged."""
    return not (record.exc_info and isinstance(record.exc_info[1], HttpProcessingError))

_HTTP_LOG = logging.getLogger("lurkme.dashboard.http")
_HTTP_LOG.addFilter(_not_a_bad_request)

async def start_dashboard(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start serving app on host:port (0 = any free port; see runner.addresses). Raises OSError if it can't listen."""
    runner = web.AppRunner(app, access_log=None, logger=_HTTP_LOG)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner

async def stop_dashboard(runner: web.AppRunner) -> None:
    await runner.cleanup()

# ── Demo (python dashboard.py --demo) ─────────────────────────────────────────

_DEMO_STARTED = time.time() - 3 * 86400 - 4 * 3600 - 17 * 60

_DEMO_CHANNELS = [
    ("aurora_plays", "AuroraPlays", "Just Chatting"), ("pixelpaws", "PixelPaws", "Minecraft"),
    ("nightowltv", "NightOwlTV", "VALORANT"), ("speedrun_sam", "speedrun_sam", "Super Mario 64"),
    ("cozycrafter", "CozyCrafter", "Stardew Valley"), ("lunalurks", "LunaLurks", "Art"),
    ("bigbossbattles", "BigBossBattles", "ELDEN RING"), ("chefkiko", "ChefKiko", "Food & Drink"),
    ("tactical_tia", "Tactical_Tia", "Counter-Strike"), ("retrorhea", "RetroRhea", "Retro"),
    ("dj_moonbeam", "DJ_Moonbeam", "Music"), ("frostbyte", "Frostbyte", "Fortnite"),
    ("questqueen", "QuestQueen", "World of Warcraft"), ("garage_gabe", "Garage_Gabe", "Science & Technology"),
    ("midlane_mo", "MidlaneMo", "League of Legends"), ("poolside_pat", "PoolsidePat", "Pools, Hot Tubs, and Beaches"),
    ("chesswizard", "ChessWizard", "Chess"), ("slowtv_sven", "SlowTV_Sven", "Travel & Outdoors"),
    ("gta_gwen", "GTA_Gwen", "Grand Theft Auto V"), ("indie_ivy", "IndieIvy", "Hollow Knight"),
    ("rocket_rex", "RocketRex", "Rocket League"), ("tarot_tara", "TarotTara", None),
    ("apex_arlo", "ApexArlo", "Apex Legends"), ("sim_racer_sol", "SimRacerSol", "iRacing"),
    ("<img src=x onerror=alert(1)>", "<img src=x onerror=alert(1)>", "<script>alert('xss')</script>"),
    ("deckbuilder_dee", "DeckbuilderDee", "Slay the Spire"), ("vtuber_kumo", "Kumo☁️", "Just Chatting"),
    ("lofi_lane", "lofi_lane", "Music"), ("horror_hal", "HorrorHal", "Resident Evil 4"),
    ("puzzlepip", "PuzzlePip", "Tetris"), ("mmo_marla", "MMO_Marla", "Final Fantasy XIV Online"),
    ("strat_stan", "StratStan", "Age of Empires II"), ("kartking", "KartKing", "Mario Kart 8 Deluxe"),
    ("fishing_fern", "FishingFern", "Fishing Planet"),
]

_DEMO_TITLES = [
    "24h stream for charity 💜 !donate", "ranked grind until diamond", "chill vibes + viewer games",
    "FIRST PLAYTHROUGH — no spoilers please", "subathon day 3 | every sub adds 5 min",
    "<b>bold</b> & \"quoted\" & 'single' — rendered as text", "drops enabled! !drops",
    "a very long title that keeps going and going so the card has to clip it nicely without breaking "
    "the layout on a small phone screen or a large monitor",
]

def demo_status() -> dict:
    """Made-up status data for --demo, with hostile strings mixed in to show they're rendered as plain text."""
    now   = time.time()
    rng   = random.Random(int(now // 10))
    day   = now - now % 86400
    today = now - day
    channels = []
    for i, (login, name, game) in enumerate(_DEMO_CHANNELS):
        source = "pinned" if i < 3 else "followed" if i < 10 else "top"
        live   = not (i == 2 or i == 9)
        base   = 92000 * 0.83 ** i + 40
        channels.append({
            "login":              login,
            "display_name":       name,
            "game":               game,
            "viewers":            int(base * rng.uniform(0.97, 1.03)) if live else None,
            "started_at":         datetime.fromtimestamp(now - 1200 - (i * 2711) % 30000, timezone.utc)
                                  .isoformat(timespec="seconds") if live else None,
            "title":              _DEMO_TITLES[i % len(_DEMO_TITLES)] if i % 11 else None,
            "thumbnail_url":      "javascript:alert(1)" if i == 24 else None,  # Ignored: not Twitch's CDN
            "profile_image_url":  None,
            "source":             source,
            "live":               live,
            "lurk_seconds_today": max(0.0, today - (i * 977) % 9000),
        })
    lurk_today = sum(ch["lurk_seconds_today"] for ch in channels)
    newest = now - now % 180  # A "new" gift every three minutes, so the celebration can be seen
    gifts = [
        (newest, "aurora_plays", "GenerousGiraffe", "1000", 1), (now - 5400, "frostbyte", "An anonymous gifter", "1000", 1),
        (now - 31000, "chefkiko", "<b>Mallory</b>", "2000", 3), (now - 90000, "midlane_mo", "SubSanta", "1000", 1),
        (now - 200000, "nightowltv", "An anonymous gifter", "3000", 1), (now - 420000, "pixelpaws", "Kindred", "Prime", 6),
    ]

    def top(rows):
        return [{"channel": c, "drops": d, "subs": s} for c, d, s in rows]

    return {
        "generated_at": now,
        "bot": {
            "nick":               "lurkme_demo",
            "started_at":         _DEMO_STARTED,
            "connected":          True,
            "last_data_age":      rng.uniform(0.2, 4.0),
            "token_expires_in":   None,
            "token_auto_refresh": True,
            "max_channels":       100,
            "languages":          ["en"],
            "categories":         [],
            "discord":            True,
            "restarts":           1,
        },
        "channels": channels,
        "stats": {
            "today":    {"gifts": 2, "drops": 57, "subs_dropped": 913, "lurk_seconds": lurk_today,
                         "top_channels": top([("aurora_plays", 9, 240), ("frostbyte", 6, 150), ("chefkiko", 4, 95),
                                              ("midlane_mo", 7, 61), ("bigbossbattles", 3, 30)])},
            "all_time": {"gifts": 37, "drops": 2412, "subs_dropped": 41980, "lurk_seconds": 3.1e7,
                         "top_channels": top([("aurora_plays", 301, 8120), ("frostbyte", 255, 6400),
                                              ("midlane_mo", 190, 3990), ("questqueen", 120, 2210),
                                              ("chefkiko", 98, 1505)])},
        },
        "recent_gifts": [{"ts": ts, "channel": ch, "gifter": who, "plan": plan, "months": months}
                         for ts, ch, who, plan, months in gifts],
    }

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Preview the lurkme dashboard with made-up data.")
    parser.add_argument("--demo", metavar="PORT", nargs="?", const=8787, type=int, help="serve demo data (default port 8787)")
    parser.add_argument("--password", help="require this password, to try the login prompt")
    args = parser.parse_args(argv)
    if args.demo is None:
        parser.print_help()
        raise SystemExit(2)

    async def serve():
        runner = await start_dashboard(create_app(demo_status, args.password), "127.0.0.1", args.demo)
        _print(f"[dashboard] Demo at http://127.0.0.1:{runner.addresses[0][1]}  (Ctrl+C to stop)")
        try:
            await asyncio.Event().wait()
        finally:
            await stop_dashboard(runner)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())

# ── Page assets ───────────────────────────────────────────────────────────────
# One static page. No inline scripts, styles or event handlers, so the CSP can forbid them all.

_ICON = 'class="i" viewBox="0 0 24 24" aria-hidden="true"'

PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0e0e10">
<meta name="robots" content="noindex, nofollow">
<title>lurkme</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'%3E%3Crect width='40' height='40' rx='11' fill='%239146FF'/%3E%3Cellipse cx='14' cy='20' rx='5' ry='6' fill='white'/%3E%3Cellipse cx='26' cy='20' rx='5' ry='6' fill='white'/%3E%3Ccircle cx='12.6' cy='21' r='2.5' fill='%230e0e10'/%3E%3Ccircle cx='24.6' cy='21' r='2.5' fill='%230e0e10'/%3E%3C/svg%3E">
<link rel="stylesheet" href="app.css">
<script src="app.js" defer></script>
</head>
<body class="loading">
<a class="skip" href="#channels-section">Skip to channels</a>

<header class="top">
  <div class="wrap top-inner">
    <div class="brand">
      <svg class="logo" viewBox="0 0 40 40" aria-hidden="true">
        <defs><linearGradient id="logo-grad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#a970ff"/><stop offset="1" stop-color="#6a1fe0"/></linearGradient></defs>
        <rect width="40" height="40" rx="11" fill="url(#logo-grad)"/>
        <g class="logo-eyes"><ellipse cx="14" cy="20" rx="5" ry="6"/><ellipse cx="26" cy="20" rx="5" ry="6"/></g>
        <g class="logo-pupils"><circle cx="12.6" cy="21" r="2.5"/><circle cx="24.6" cy="21" r="2.5"/></g>
      </svg>
      <div class="brand-text">
        <h1>lurkme</h1>
        <p class="nick" id="nick">connecting…</p>
      </div>
    </div>
    <div class="status">
      <span class="pill" id="conn" data-tone="wait"><span class="dot" aria-hidden="true"></span><span id="conn-label">Connecting…</span></span>
      <span class="meta"><span class="meta-k">Up</span> <span id="uptime">—</span></span>
      <span class="meta token" id="token" data-tone="muted">Token —</span>
      <span class="meta updated" id="updated">Waiting for data…</span>
    </div>
  </div>
</header>

<div class="wrap">
  <div class="banner" id="banner" role="status" aria-live="polite" data-tone="warn" hidden>
    <svg """ + _ICON + r"""><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
    <span id="banner-text"></span>
  </div>
</div>

<main class="wrap">
  <section class="tiles" aria-label="Stats at a glance">
    <article class="tile hero" id="tile-gifts-today">
      <p class="tile-label"><svg """ + _ICON + r"""><path d="M20 12v10H4V12M2 7h20v5H2zM12 22V7M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7zM12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/></svg>Gifts today</p>
      <p class="tile-value" id="gifts-today">0</p>
      <p class="tile-sub" id="gifts-today-sub">&nbsp;</p>
    </article>
    <article class="tile">
      <p class="tile-label"><svg """ + _ICON + r"""><circle cx="12" cy="8" r="7"/><path d="M8.2 13.9 7 23l5-3 5 3-1.2-9.1"/></svg>Gifts all-time</p>
      <p class="tile-value" id="gifts-all">0</p>
      <p class="tile-sub" id="gifts-all-sub">&nbsp;</p>
    </article>
    <article class="tile">
      <p class="tile-label"><svg """ + _ICON + r"""><path d="M8 19v2M8 13v2M16 19v2M16 13v2M12 21v2M12 15v2M20 16.6A5 5 0 0 0 18 7h-1.3A8 8 0 1 0 4 15.3"/></svg>Drops seen today</p>
      <p class="tile-value" id="drops-today">0</p>
      <p class="tile-sub" id="drops-today-sub">&nbsp;</p>
    </article>
    <article class="tile">
      <p class="tile-label"><svg """ + _ICON + r"""><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>Your odds</p>
      <p class="tile-value" id="odds">—</p>
      <p class="tile-sub" id="odds-sub">&nbsp;</p>
    </article>
    <article class="tile">
      <p class="tile-label"><svg """ + _ICON + r"""><rect x="2" y="7" width="20" height="15" rx="2"/><path d="m17 2-5 5-5-5"/></svg>Channels</p>
      <p class="tile-value"><span id="chan-count">0</span><span class="tile-of" id="chan-max"></span></p>
      <div class="meter" id="chan-meter-track" role="progressbar" aria-label="Channel slots used" aria-valuemin="0"><span id="chan-meter"></span></div>
      <p class="tile-sub" id="chan-sub">&nbsp;</p>
    </article>
    <article class="tile">
      <p class="tile-label"><svg """ + _ICON + r"""><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>Lurk time today</p>
      <p class="tile-value" id="lurk-today">0m</p>
      <p class="tile-sub" id="lurk-sub">&nbsp;</p>
    </article>
  </section>

  <div class="layout">
    <section class="panel channels" id="channels-section" aria-labelledby="channels-h">
      <div class="panel-head">
        <div>
          <h2 id="channels-h">Lurking in</h2>
          <p class="panel-sub" id="channels-sub">&nbsp;</p>
        </div>
        <div class="controls">
          <label class="search">
            <svg """ + _ICON + r"""><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            <input id="filter" type="search" placeholder="Filter by name, game or title" autocomplete="off" spellcheck="false" aria-label="Filter channels" aria-keyshortcuts="/">
            <kbd aria-hidden="true">/</kbd>
          </label>
          <div class="seg" role="group" aria-label="Sort channels by">
            <button type="button" id="sort-viewers" aria-pressed="true">Viewers</button>
            <button type="button" id="sort-name" aria-pressed="false">Name</button>
            <button type="button" id="sort-lurk" aria-pressed="false">Lurk time</button>
          </div>
        </div>
      </div>
      <div class="chips" role="group" aria-label="Show channels">
        <button type="button" class="chip" id="src-all" aria-pressed="true">All <span class="chip-n" id="src-all-n">0</span></button>
        <button type="button" class="chip" id="src-pinned" data-source="pinned" aria-pressed="false" hidden>Pinned <span class="chip-n" id="src-pinned-n">0</span></button>
        <button type="button" class="chip" id="src-followed" data-source="followed" aria-pressed="false" hidden>Followed <span class="chip-n" id="src-followed-n">0</span></button>
        <button type="button" class="chip" id="src-top" data-source="top" aria-pressed="false" hidden>Top streams <span class="chip-n" id="src-top-n">0</span></button>
      </div>
      <div class="grid" id="channels" aria-live="off">
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
        <div class="ch sk" aria-hidden="true"><div class="ch-media"></div><div class="ch-body"><span class="sk-line"></span><span class="sk-line short"></span></div></div>
      </div>
      <p class="empty" id="channels-empty" hidden></p>
      <button type="button" class="more" id="more" aria-controls="channels" hidden></button>
    </section>

    <aside class="side">
      <section class="panel" aria-labelledby="gifts-h">
        <div class="panel-head">
          <h2 id="gifts-h">Recent gifts</h2>
          <span class="count" id="gifts-count"></span>
        </div>
        <ol class="gifts" id="gifts"></ol>
        <div class="empty" id="gifts-empty" hidden>
          <span class="empty-art" aria-hidden="true"></span>
          <p><strong>No gifts yet.</strong> Every drop lurkme sees is another roll of the dice — they'll show up here the moment one lands.</p>
        </div>
      </section>

      <section class="panel" aria-labelledby="top-h">
        <div class="panel-head">
          <h2 id="top-h">Top channels for gift drops</h2>
        </div>
        <div class="tabs" role="tablist" aria-label="Period">
          <button type="button" role="tab" id="tab-today" aria-selected="true" aria-controls="top-list">Today</button>
          <button type="button" role="tab" id="tab-all" aria-selected="false" aria-controls="top-list" tabindex="-1">All-time</button>
        </div>
        <ol class="toplist" id="top-list" role="tabpanel" aria-labelledby="top-h"></ol>
        <p class="empty small" id="top-empty" hidden>No gift drops seen yet</p>
      </section>
    </aside>
  </div>
</main>

<footer class="foot wrap">
  <dl class="facts">
    <div><dt>Languages</dt><dd id="f-langs">—</dd></div>
    <div><dt>Categories</dt><dd id="f-cats">—</dd></div>
    <div><dt>Discord alerts</dt><dd id="f-discord">—</dd></div>
    <div><dt>Max channels</dt><dd id="f-max">—</dd></div>
    <div><dt>Restarts</dt><dd id="f-restarts">—</dd></div>
  </dl>
  <p class="foot-note">“Today” starts at 00:00 UTC · refreshes every 10 seconds · press <kbd>/</kbd> to filter channels</p>
</footer>

<div class="toasts" id="toasts" aria-live="polite"></div>
<noscript><p class="noscript">The dashboard needs JavaScript to load its data.</p></noscript>
</body>
</html>
"""

APP_CSS = r"""
:root {
  --bg: #0e0e10;
  --surface: #18181b;
  --surface-2: #1f1f23;
  --surface-3: #2a2a31;
  --border: #2c2c33;
  --border-hi: #3d3d47;
  --text: #efeff1;
  --text-2: #adadb8;
  --muted: #84848f;
  --purple: #9146ff;
  --purple-hi: #bf94ff;
  --purple-lo: #772ce8;
  --purple-wash: rgba(145, 70, 255, .16);
  --good: #00d17a;
  --bad: #ff5a5f;
  --warn: #ffb31a;
  --live: #eb0400;
  --pinned: #ffca5f;
  --followed: #ff7ad9;
  --topc: #6fb7ff;
  --radius: 16px;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif,
          "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji";
  --icon-gift: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20 12v10H4V12M2 7h20v5H2zM12 22V7M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7zM12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z'/%3E%3C/svg%3E");
  color-scheme: dark;
}

*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0;
  min-height: 100vh;
  font: 15px/1.5 var(--font);
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background:
    radial-gradient(900px 420px at 10% -8%, rgba(145, 70, 255, .26), transparent 65%),
    radial-gradient(700px 360px at 95% -5%, rgba(191, 148, 255, .10), transparent 60%);
}
a { color: inherit; text-decoration: none; }
a[href]:hover { color: var(--purple-hi); }
button, input { font: inherit; color: inherit; }
h1, h2, p { margin: 0; }
:focus-visible { outline: 2px solid var(--purple-hi); outline-offset: 2px; border-radius: 6px; }
kbd {
  font: 600 11px/1 var(--font);
  padding: 3px 6px;
  border-radius: 5px;
  border: 1px solid var(--border-hi);
  background: var(--surface-2);
  color: var(--text-2);
}
.i { width: 18px; height: 18px; flex: none; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

.wrap {
  max-width: 1480px;
  margin: 0 auto;
  padding-left: max(16px, env(safe-area-inset-left));
  padding-right: max(16px, env(safe-area-inset-right));
}
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 16px; top: 12px; z-index: 50; background: var(--purple); color: #fff; padding: 8px 12px; border-radius: 8px; }

/* ── Header ── */
.top {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(14, 14, 16, .78);
  -webkit-backdrop-filter: saturate(150%) blur(14px);
  backdrop-filter: saturate(150%) blur(14px);
  border-bottom: 1px solid rgba(255, 255, 255, .06);
}
.top-inner { display: flex; align-items: center; justify-content: space-between; gap: 12px 20px; flex-wrap: wrap; padding-top: 12px; padding-bottom: 12px; }
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.logo { width: 40px; height: 40px; flex: none; filter: drop-shadow(0 4px 14px rgba(145, 70, 255, .45)); }
.logo-eyes { fill: #fff; transform-box: fill-box; transform-origin: center; animation: blink 6s infinite; }
.logo-pupils { fill: #0e0e10; animation: look 9s ease-in-out infinite; }
@keyframes blink { 0%, 93%, 100% { transform: scaleY(1); } 96% { transform: scaleY(.1); } }
@keyframes look {
  0%, 30% { transform: translateX(0); }
  38%, 62% { transform: translateX(3px); }
  70%, 100% { transform: translateX(0); }
}
.brand-text { min-width: 0; }
h1 { font-size: 21px; font-weight: 800; letter-spacing: -.02em; line-height: 1.1; }
.nick { color: var(--text-2); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 46vw; }
.status { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; font-size: 13px; color: var(--text-2); }
.meta { white-space: nowrap; font-variant-numeric: tabular-nums; }
.meta-k { color: var(--muted); }
.token[data-tone="ok"] { color: var(--good); }
.token[data-tone="warn"] { color: var(--warn); }
.token[data-tone="bad"] { color: var(--bad); }
.token[data-tone="muted"], .updated { color: var(--muted); }

.pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px 5px 10px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text);
  font-weight: 600;
  white-space: nowrap;
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
.pill[data-tone="ok"] { border-color: rgba(0, 209, 122, .35); }
.pill[data-tone="ok"] .dot { background: var(--good); animation: ping 2.4s ease-out infinite; }
.pill[data-tone="bad"] { border-color: rgba(255, 90, 95, .45); background: rgba(255, 90, 95, .1); }
.pill[data-tone="bad"] .dot { background: var(--bad); }
.pill[data-tone="wait"] .dot { animation: fade 1s ease-in-out infinite alternate; }
@keyframes ping {
  0% { box-shadow: 0 0 0 0 rgba(0, 209, 122, .6); }
  70%, 100% { box-shadow: 0 0 0 9px rgba(0, 209, 122, 0); }
}
@keyframes fade { from { opacity: .3; } to { opacity: 1; } }

/* ── Banner & toasts ── */
.banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
  padding: 11px 14px;
  border-radius: 12px;
  font-size: 14px;
  background: rgba(255, 179, 26, .08);
  border: 1px solid rgba(255, 179, 26, .35);
  color: #ffd98a;
  animation: drop-in .35s ease-out;
}
.banner[data-tone="bad"] { background: rgba(255, 90, 95, .09); border-color: rgba(255, 90, 95, .4); color: #ffb3b5; }
@keyframes drop-in { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }

.toasts {
  position: fixed;
  right: max(16px, env(safe-area-inset-right));
  bottom: max(16px, env(safe-area-inset-bottom));
  z-index: 40;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: min(380px, calc(100vw - 32px));
}
.toast {
  padding: 12px 16px;
  border-radius: 12px;
  background: linear-gradient(135deg, #2a1650, #1d1233);
  border: 1px solid rgba(191, 148, 255, .45);
  box-shadow: 0 12px 40px rgba(0, 0, 0, .5), 0 0 30px rgba(145, 70, 255, .25);
  font-weight: 600;
  overflow-wrap: anywhere;
  animation: toast-in .45s cubic-bezier(.2, .9, .3, 1.3);
}
.toast.out { opacity: 0; transform: translateY(8px); transition: opacity .35s, transform .35s; }
@keyframes toast-in { from { opacity: 0; transform: translateY(16px) scale(.96); } to { opacity: 1; transform: none; } }

/* ── Stat tiles ── */
main { padding-top: 18px; padding-bottom: 8px; }
.tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.tile {
  position: relative;
  min-width: 0;
  padding: 16px 16px 14px;
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(255, 255, 255, .025), transparent 60%), var(--surface);
  border: 1px solid var(--border);
  transition: border-color .2s, transform .2s;
  animation: rise .5s ease-out both;
}
.tile:hover { border-color: var(--border-hi); }
.tile:nth-child(2) { animation-delay: .04s; }
.tile:nth-child(3) { animation-delay: .08s; }
.tile:nth-child(4) { animation-delay: .12s; }
.tile:nth-child(5) { animation-delay: .16s; }
.tile:nth-child(6) { animation-delay: .2s; }
.tile.hero {
  background:
    radial-gradient(120% 140% at 100% 0%, rgba(191, 148, 255, .28), transparent 55%),
    linear-gradient(135deg, #5b1fc4, #2c1162 70%);
  border-color: rgba(191, 148, 255, .35);
  overflow: hidden;
}
.tile-label { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; color: var(--text-2); }
.tile-label .i { width: 16px; height: 16px; color: var(--purple-hi); }
.hero .tile-label, .hero .tile-label .i { color: #e6d8ff; }
.tile-value {
  margin-top: 6px;
  font-size: clamp(26px, 6.4vw, 34px);
  font-weight: 750;
  letter-spacing: -.02em;
  line-height: 1.15;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tile-of { font-size: .55em; color: var(--muted); font-weight: 600; margin-left: 2px; }
.tile-sub { margin-top: 4px; font-size: 12.5px; line-height: 1.4; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hero .tile-sub { color: #cbb6f5; }
.meter { margin-top: 8px; height: 6px; border-radius: 99px; background: var(--purple-wash); overflow: hidden; }
.meter span { display: block; height: 100%; width: 0; border-radius: inherit; background: linear-gradient(90deg, var(--purple-lo), var(--purple-hi)); transition: width .8s cubic-bezier(.2, .8, .2, 1); }
.tile.celebrate { animation: celebrate 1.2s ease-out; }
@keyframes celebrate {
  0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(191, 148, 255, .7); }
  25% { transform: scale(1.04); }
  100% { transform: scale(1); box-shadow: 0 0 0 22px rgba(191, 148, 255, 0); }
}
.confetti {
  position: absolute;
  left: 50%;
  top: 45%;
  width: 7px;
  height: 11px;
  border-radius: 2px;
  background: var(--c, #fff);
  pointer-events: none;
  animation: confetti 1.2s cubic-bezier(.15, .7, .3, 1) forwards;
}
@keyframes confetti {
  from { transform: translate(-50%, -50%) rotate(0); opacity: 1; }
  to { transform: translate(calc(-50% + var(--dx, 0px)), calc(-50% + var(--dy, 0px))) rotate(var(--r, 180deg)); opacity: 0; }
}

/* Skeleton while the first status loads */
.loading .tile-value, .loading .tile-sub, .loading .panel-sub {
  color: transparent !important;
  border-radius: 6px;
  background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 50%, var(--surface-2) 75%);
  background-size: 300% 100%;
  animation: shimmer 1.3s linear infinite;
}
.loading .tile-value { max-width: 60%; }
.loading .tile-sub, .loading .panel-sub { max-width: 80%; }
.loading .tile-of { color: transparent; }
@keyframes shimmer { from { background-position: 100% 0; } to { background-position: 0 0; } }
.sk .ch-media, .sk-line {
  background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 50%, var(--surface-2) 75%);
  background-size: 300% 100%;
  animation: shimmer 1.3s linear infinite;
}
.sk-line { display: block; height: 12px; border-radius: 6px; margin: 4px 0 10px; }
.sk-line.short { width: 55%; }

/* ── Panels ── */
.layout { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; margin-top: 16px; }
.side { display: grid; gap: 16px; align-content: start; min-width: 0; }
.panel { min-width: 0; padding: 16px; border-radius: var(--radius); background: var(--surface); border: 1px solid var(--border); }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
h2 { font-size: 17px; font-weight: 750; letter-spacing: -.01em; }
.panel-sub { margin-top: 2px; font-size: 13px; color: var(--muted); min-height: 1.4em; }
.count { font-size: 12px; color: var(--muted); padding-top: 3px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; width: 100%; }
.search {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1 1 220px;
  min-width: 0;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  transition: border-color .15s, box-shadow .15s;
}
.search:focus-within { border-color: var(--purple); box-shadow: 0 0 0 3px var(--purple-wash); }
.search .i { position: absolute; left: 10px; width: 16px; height: 16px; color: var(--muted); }
.search input { width: 100%; min-width: 0; padding: 8px 34px 8px 34px; border: 0; background: transparent; outline: none; font-size: 14px; }
.search input::placeholder { color: var(--muted); }
.search kbd { position: absolute; right: 8px; }
.search:focus-within kbd { display: none; }
.seg { display: inline-flex; padding: 3px; gap: 2px; border-radius: 10px; background: var(--surface-2); border: 1px solid var(--border); }
.seg button, .tabs button {
  padding: 5px 11px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background .15s, color .15s;
}
.seg button:hover, .tabs button:hover { color: var(--text); }
.seg button[aria-pressed="true"], .tabs button[aria-selected="true"] { background: var(--purple); color: #fff; box-shadow: 0 2px 10px rgba(145, 70, 255, .35); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color .15s, background .15s, color .15s;
}
.chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--purple-hi); }
.chip[data-source="pinned"]::before { background: var(--pinned); }
.chip[data-source="followed"]::before { background: var(--followed); }
.chip[data-source="top"]::before { background: var(--topc); }
.chip:hover { border-color: var(--border-hi); color: var(--text); }
.chip[aria-pressed="true"] { background: var(--purple-wash); border-color: rgba(145, 70, 255, .6); color: var(--text); }
.chip-n { color: var(--muted); font-variant-numeric: tabular-nums; }

/* ── Channel cards ── */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(232px, 1fr)); gap: 14px; }
.ch {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  border-radius: 13px;
  overflow: hidden;
  background: var(--surface-2);
  border: 1px solid var(--border);
  transition: transform .2s ease, border-color .2s, box-shadow .2s;
}
.ch:hover { transform: translateY(-3px); border-color: rgba(145, 70, 255, .6); box-shadow: 0 12px 30px rgba(0, 0, 0, .45), 0 0 0 1px rgba(145, 70, 255, .2); }
.ch.enter { animation: rise .45s ease-out both; }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.ch-media { position: relative; display: block; aspect-ratio: 16 / 9; overflow: hidden; background: var(--surface-3); flex: none; }
.ch-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .35s ease, transform .5s ease, filter .3s; }
.ch-img.ready { opacity: 1; }
.ch:hover .ch-img { transform: scale(1.045); }
.ch-initial {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 46px;
  font-weight: 800;
  color: rgba(255, 255, 255, .93);
  text-shadow: 0 4px 18px rgba(0, 0, 0, .35);
  background:
    radial-gradient(circle at 30% 25%, rgba(255, 255, 255, .18), transparent 55%),
    linear-gradient(135deg, hsl(var(--hue, 265) 68% 46%), hsl(calc(var(--hue, 265) + 45) 70% 22%));
}
.offline .ch-img, .offline .ch-initial { filter: grayscale(.85) brightness(.55); }
.badge {
  position: absolute;
  padding: 2px 7px;
  border-radius: 5px;
  font-size: 11.5px;
  font-weight: 700;
  line-height: 1.5;
  color: #fff;
  background: rgba(0, 0, 0, .72);
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ch-live { top: 8px; left: 8px; display: inline-flex; align-items: center; gap: 5px; background: var(--live); letter-spacing: .05em; font-size: 10.5px; }
.ch-live::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: #fff; animation: fade 1.1s ease-in-out infinite alternate; }
.offline .ch-live { background: rgba(0, 0, 0, .72); color: var(--text-2); }
.offline .ch-live::before { background: var(--muted); animation: none; }
.ch-viewers { left: 8px; bottom: 8px; }
.ch-viewers::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 5px; border-radius: 50%; background: var(--live); vertical-align: 1px; }
.ch-uptime { right: 8px; bottom: 8px; }
.ch-body { display: flex; flex-direction: column; gap: 8px; padding: 11px 12px 12px; min-width: 0; flex: 1; }
.ch-head { display: flex; align-items: flex-start; gap: 9px; min-width: 0; }
.ch-avatar { width: 34px; height: 34px; border-radius: 50%; flex: none; object-fit: cover; background: var(--surface-3); border: 2px solid var(--border-hi); opacity: 0; transition: opacity .3s; }
.ch-avatar.ready { opacity: 1; }
.live .ch-avatar { border-color: var(--purple); }
.ch-names { min-width: 0; flex: 1; }
.ch-name { display: block; font-weight: 700; font-size: 15px; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ch-game { font-size: 12.5px; color: var(--purple-hi); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.offline .ch-game { color: var(--muted); }
.src {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-2);
  background: rgba(255, 255, 255, .05);
  border: 1px solid var(--border);
}
.src::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--topc); }
.src[data-source="pinned"]::before { background: var(--pinned); }
.src[data-source="followed"]::before { background: var(--followed); }
.ch-title {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
  min-height: 2.8em;
}
.ch-foot { display: flex; align-items: center; gap: 8px; margin-top: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.ch-lurk { white-space: nowrap; }
.ch-bar { flex: 1; height: 4px; border-radius: 99px; background: var(--purple-wash); overflow: hidden; min-width: 30px; }
.ch-bar span { display: block; height: 100%; width: 0; border-radius: inherit; background: var(--purple); transition: width .8s ease; }
.ch-hot {
  align-self: flex-start;
  max-width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 700;
  color: #ffd98a;
  background: rgba(255, 179, 26, .1);
  border: 1px solid rgba(255, 179, 26, .3);
}

.more {
  display: block;
  width: 100%;
  margin-top: 14px;
  padding: 10px;
  border-radius: 11px;
  border: 1px dashed var(--border-hi);
  background: transparent;
  color: var(--text-2);
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color .15s, color .15s, background .15s;
}
.more:hover { border-color: var(--purple); color: var(--text); background: var(--purple-wash); }

/* ── Recent gifts ── */
.gifts, .toplist { list-style: none; margin: 0; padding: 0; }
.gift { display: flex; align-items: center; gap: 11px; padding: 10px 0; border-top: 1px solid var(--border); min-width: 0; }
.gift:first-child { border-top: 0; padding-top: 2px; }
.gift.new { animation: glow 2.4s ease-out; }
@keyframes glow { 0% { background: rgba(145, 70, 255, .35); } 100% { background: transparent; } }
.gift-icon { width: 38px; height: 38px; flex: none; display: grid; place-items: center; border-radius: 11px; background: var(--purple-wash); }
.gift-icon::before { content: ""; width: 19px; height: 19px; background: var(--purple-hi); -webkit-mask: var(--icon-gift) center / contain no-repeat; mask: var(--icon-gift) center / contain no-repeat; }
.gift-icon[data-tier="2"] { background: rgba(111, 183, 255, .14); }
.gift-icon[data-tier="2"]::before { background: var(--topc); }
.gift-icon[data-tier="3"] { background: rgba(255, 202, 95, .14); }
.gift-icon[data-tier="3"]::before { background: var(--pinned); }
.gift-main { flex: 1; min-width: 0; }
.gift-line { display: flex; align-items: center; gap: 7px; min-width: 0; font-size: 14px; }
.gift-who { min-width: 0; color: var(--text); font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gift-meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gift-meta { font-size: 12.5px; color: var(--muted); }
.gift-ch { color: var(--purple-hi); font-weight: 600; }
.tier { flex: none; padding: 0 6px; border-radius: 5px; font-size: 11px; font-weight: 700; line-height: 1.6; color: var(--text-2); background: rgba(255, 255, 255, .07); }
.gift-time { flex: none; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }

/* ── Top channels ── */
.tabs { display: inline-flex; padding: 3px; gap: 2px; margin-bottom: 12px; border-radius: 10px; background: var(--surface-2); border: 1px solid var(--border); }
.top-row { display: flex; gap: 11px; align-items: center; padding: 8px 0; }
.rank {
  width: 26px;
  height: 26px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 8px;
  font-size: 12.5px;
  font-weight: 800;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.top-row:first-child .rank { color: #1a1206; background: linear-gradient(135deg, #ffe08a, #f5b324); border-color: transparent; }
.top-main { flex: 1; min-width: 0; }
.top-line { display: flex; justify-content: space-between; gap: 10px; font-size: 14px; }
.top-name { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.top-num { flex: none; font-weight: 700; font-variant-numeric: tabular-nums; }
.bar { height: 6px; margin: 5px 0 3px; border-radius: 99px; background: var(--purple-wash); overflow: hidden; }
.bar span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--purple-lo), var(--purple-hi)); transform-origin: left; animation: grow .7s cubic-bezier(.2, .8, .2, 1) both; }
@keyframes grow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.top-meta { font-size: 12px; color: var(--muted); }

/* ── Empty states & footer ── */
.empty { padding: 26px 12px; text-align: center; color: var(--muted); font-size: 14px; }
.empty.small { padding: 14px 4px; }
.empty strong { color: var(--text-2); }
.empty-art {
  display: block;
  width: 54px;
  height: 54px;
  margin: 0 auto 12px;
  border-radius: 16px;
  background: var(--purple-wash);
  position: relative;
  animation: bob 3.2s ease-in-out infinite;
}
.empty-art::before { content: ""; position: absolute; inset: 14px; background: var(--purple-hi); -webkit-mask: var(--icon-gift) center / contain no-repeat; mask: var(--icon-gift) center / contain no-repeat; }
@keyframes bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
.foot { padding-top: 22px; padding-bottom: max(28px, env(safe-area-inset-bottom)); color: var(--muted); font-size: 13px; }
.facts { display: flex; flex-wrap: wrap; gap: 10px 26px; margin: 0; padding: 16px 0 12px; border-top: 1px solid var(--border); }
.facts div { min-width: 0; }
.facts dt { font-size: 11.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
.facts dd { margin: 2px 0 0; color: var(--text); font-weight: 600; overflow-wrap: anywhere; }
.facts dd[data-tone="ok"]::before, .facts dd[data-tone="off"]::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; vertical-align: 1px; }
.facts dd[data-tone="ok"]::before { background: var(--good); }
.facts dd[data-tone="off"]::before { background: var(--muted); }
.foot-note { font-size: 12px; }
.noscript { margin: 20px; padding: 14px; border-radius: 12px; background: var(--surface); }

/* ── Responsive ── */
@media (min-width: 720px) {
  .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
  .controls { width: auto; }
  .search { flex: 0 1 280px; }
  .panel { padding: 18px 20px 20px; }
  main { padding-top: 22px; }
}
@media (min-width: 1180px) {
  .tiles { grid-template-columns: repeat(6, minmax(0, 1fr)); }
  .layout { grid-template-columns: minmax(0, 1fr) 380px; gap: 18px; }
}
@media (max-width: 560px) {
  .top { position: relative; }
  .top-inner { padding-top: 10px; padding-bottom: 10px; gap: 10px; }
  h1 { font-size: 19px; }
  .logo { width: 36px; height: 36px; }
  .status { gap: 6px 12px; width: 100%; font-size: 12.5px; }
  .updated { margin-left: auto; }
  .tile { padding: 14px 13px 12px; }
  .panel.channels { padding: 2px 0 0; background: none; border: 0; }
  .grid { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .ch { flex-direction: row; }
  .ch:hover { transform: none; }
  .ch-media { width: 112px; align-self: stretch; aspect-ratio: auto; min-height: 92px; }
  .ch-initial { font-size: 32px; }
  .ch-uptime { display: none; }
  .ch-live { top: 6px; left: 6px; }
  .ch-viewers { left: 6px; bottom: 6px; }
  .ch-body { padding: 9px 10px 10px; gap: 5px; }
  .ch-avatar { display: none; }
  .ch-name { font-size: 14.5px; }
  .ch-title { -webkit-line-clamp: 1; min-height: 0; font-size: 12.5px; }
  .src { padding: 1px 6px; font-size: 10.5px; }
  .seg { width: 100%; }
  .seg button { flex: 1; }
}
@media (hover: none) { .ch:hover { transform: none; } .ch:hover .ch-img { transform: none; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
"""

APP_JS = r"""'use strict';
// lurkme dashboard. Every string in the status JSON (names, titles, games, gifters) comes from
// Twitch and is treated as hostile: the DOM is built with createElement + textContent only, images
// only ever load from Twitch's CDN, and links only ever point at twitch.tv channel pages.
(() => {
  const CDN        = 'https://static-cdn.jtvnw.net/';
  const TWITCH     = 'https://www.twitch.tv/';
  const REFRESH_MS = 10000;
  const TIMEOUT_MS = 8000;
  const THUMB_TTL  = 300;  // Seconds between stream preview reloads
  const TIERS      = {1000: 'Tier 1', 2000: 'Tier 2', 3000: 'Tier 3', Prime: 'Prime'};
  const TIER_KEYS  = {1000: '1', 2000: '2', 3000: '3', Prime: 'prime'};
  const SOURCES    = {pinned: 'Pinned', followed: 'Followed', top: 'Top'};
  const CONFETTI   = ['#9146ff', '#bf94ff', '#ffca5f', '#00d17a', '#ff7ad9', '#6fb7ff'];
  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  // ── Defensive readers: any field may be missing, null or the wrong type ──
  const $     = (id) => document.getElementById(id);
  const has   = (o, k) => (typeof k === 'string' || typeof k === 'number') && Object.prototype.hasOwnProperty.call(o, k);
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const obj   = (v) => (isObj(v) ? v : {});
  const arr   = (v) => (Array.isArray(v) ? v : []);
  const num   = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const whole = (v) => { const n = num(v); return n === null || n < 0 ? 0 : Math.round(n); };
  const str   = (v) => (typeof v === 'string' ? v : num(v) !== null ? String(v) : '');
  const clip  = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

  // ── Formatting ──
  const numberFormat = (options) => {
    try {
      const format = new Intl.NumberFormat('en-US', options);
      return (n) => format.format(n);
    } catch (e) {
      return (n) => String(Math.round(n));
    }
  };
  const fmtInt     = numberFormat({maximumFractionDigits: 0});
  const fmtCompact = numberFormat({notation: 'compact', maximumFractionDigits: 1});
  const plural     = (n, word) => fmtInt(n) + ' ' + word + (n === 1 ? '' : 's');

  function human(s) {  // "3d 4h", "5h 12m", "42m", "under a minute" (like the Discord cards)
    s = num(s);
    if (s === null || s < 60) return 'under a minute';
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return h ? `${fmtInt(d)}d ${h}h` : `${fmtInt(d)}d`;
    if (h) return m ? `${h}h ${m}m` : `${h}h`;
    return `${m}m`;
  }
  const clock = (s) => { s = num(s); return s === null ? '—' : s < 60 ? `${Math.max(0, Math.floor(s))}s` : human(s); };
  function hoursOf(s) {  // Summed lurk time reads best in hours: "412h", "5h 12m", "42m"
    s = num(s);
    if (s === null || s < 60) return '0m';
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h >= 100 || !m ? `${fmtInt(h)}h` : `${h}h ${m}m`;
  }
  function dateOf(ts, withTime) {
    const date = new Date(ts * 1000);
    if (!Number.isFinite(date.getTime())) return '';
    return withTime ? date.toLocaleString() : date.toLocaleDateString();
  }
  function ago(ts, now) {
    ts = num(ts);
    if (ts === null) return '';
    const d = now - ts;
    if (d < 45) return 'just now';
    if (d < 3600) return `${Math.max(1, Math.floor(d / 60))}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    if (d < 30 * 86400) return `${Math.floor(d / 86400)}d ago`;
    return dateOf(ts, false);
  }
  function isoOf(ts) {
    try { return new Date(ts * 1000).toISOString(); } catch (e) { return ''; }
  }
  function parseIso(v) {
    const ms = Date.parse(str(v));
    return Number.isFinite(ms) ? ms / 1000 : null;
  }

  // ── The only ways a URL from the status data reaches the page ──
  function cdn(raw, width, height) {
    const url = str(raw).split('{width}').join(String(width)).split('{height}').join(String(height));
    if (!url.startsWith(CDN)) return null;
    try {
      const href = new URL(url).href;
      return href.startsWith(CDN) ? href : null;
    } catch (e) {
      return null;
    }
  }
  function channelUrl(login) {
    try {
      return login ? TWITCH + encodeURIComponent(login) : '';
    } catch (e) {
      return '';  // A lone UTF-16 surrogate can't be encoded (URIError): show the name without a link
    }
  }

  // ── DOM helpers (text only, never HTML) ──
  function h(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }
  function setText(node, text) {
    text = String(text);
    if (node.textContent !== text) node.textContent = text;
  }
  function setLink(a, login) {
    const url = channelUrl(login);
    if (url) {
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    } else {
      a.removeAttribute('href');
    }
  }
  function twitchLink(login, cls, text) {
    const a = h('a', cls, text);
    setLink(a, login);
    return a;
  }
  function hueOf(s) {
    let x = 7;
    for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) >>> 0;
    return x % 360;
  }
  const initialOf = (s) => (Array.from(s.trim())[0] || '?').toUpperCase();

  const prefs = {  // Per-browser conveniences only; the page works without them
    get(key) { try { return window.localStorage.getItem('lurkme.' + key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem('lurkme.' + key, value); } catch (e) { /* private mode */ } },
  };

  // ── State ──
  const byName = (a, b) => (a.sortKey < b.sortKey ? -1 : a.sortKey > b.sortKey ? 1 : 0);
  const SORTS = {
    viewers: (a, b) => (b.live - a.live) || (b.viewersSort - a.viewersSort) || byName(a, b),
    name:    byName,
    lurk:    (a, b) => (b.lurk - a.lurk) || byName(a, b),
  };
  const savedSort = prefs.get('sort');
  const state = {
    data: null, offset: 0, lastOk: 0, error: '', timer: 0, busy: false,
    channels: [], cards: new Map(), giftTimes: [], newestGift: null, latestGift: null,
    sort: has(SORTS, savedSort) ? savedSort : 'viewers',
    tab: prefs.get('tab') === 'all_time' ? 'all_time' : 'today',
    source: 'all', filter: '', expanded: prefs.get('expanded') === '1',
  };
  const serverNow = () => Date.now() / 1000 + state.offset;  // Corrects for a browser clock that's off
  const shown = new WeakMap();  // Last value each animated number showed

  function countTo(node, value) {
    const from = shown.has(node) ? shown.get(node) : 0;
    shown.set(node, value);
    if (from === value || reduceMotion || document.hidden || typeof requestAnimationFrame !== 'function') {
      setText(node, fmtInt(value));
      return;
    }
    const start = performance.now(), duration = 800;
    const step = (t) => {
      if (shown.get(node) !== value) return;  // A newer value took over
      const p = Math.min(1, Math.max(0, (t - start) / duration)), eased = 1 - Math.pow(1 - p, 3);
      setText(node, fmtInt(Math.round(from + (value - from) * eased)));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ── Header ──
  function renderHeader(d) {
    const bot = obj(d.bot);
    const nick = clip(str(bot.nick).trim(), 40);
    setText($('nick'), nick ? 'Lurking as ' + nick : 'Not logged in yet');
    const gifts = whole(obj(obj(d.stats).today).gifts);
    document.title = (gifts ? `🎁 ${fmtInt(gifts)} · ` : '') + 'lurkme' + (nick ? ' · ' + nick : '');
  }

  function renderConnection() {
    const bot = obj(state.data && state.data.bot);
    let tone, label;
    if (state.error === 'auth') { tone = 'bad'; label = 'Signed out'; }
    else if (state.error) { tone = 'bad'; label = state.data ? 'Dashboard offline' : 'Unreachable'; }
    else if (!state.data) { tone = 'wait'; label = 'Connecting…'; }
    else if (bot.connected === true) { tone = 'ok'; label = 'Connected'; }
    else { tone = 'bad'; label = 'Disconnected from Twitch'; }
    const pill = $('conn');
    pill.dataset.tone = tone;
    const age = num(bot.last_data_age);
    pill.title = age === null ? '' : `Last message from Twitch ${clock(age)} ago`;
    setText($('conn-label'), label);
  }

  function renderToken(bot, now) {
    const node = $('token');
    let text, tone = 'ok';
    const left = num(bot.token_expires_in), generated = num(obj(state.data).generated_at);
    if (bot.token_auto_refresh === true) {
      text = 'Token auto-refresh on';
    } else if (left === null) {
      text = 'Token never expires';
      tone = 'muted';
    } else {
      const remaining = left - (generated === null ? 0 : now - generated);
      text = remaining > 0 ? 'Token expires in ' + human(remaining) : 'Token expired';
      tone = remaining > 3600 ? 'warn' : 'bad';
    }
    setText(node, text);
    node.dataset.tone = tone;
  }

  function renderBanner() {
    const banner = $('banner');
    const age = state.lastOk ? (Date.now() - state.lastOk) / 1000 : null;
    let text = '';
    if (state.error === 'auth') text = 'The dashboard password was rejected. Reload the page to sign in again.';
    else if (state.error && state.data) text = `Couldn't refresh — showing data from ${clock(age)} ago. Retrying every 10 seconds.`;
    else if (state.error) text = "Can't reach lurkme yet. Retrying every 10 seconds…";
    banner.hidden = !text;
    banner.dataset.tone = state.error === 'auth' ? 'bad' : 'warn';
    if (text) setText($('banner-text'), text);
  }

  // ── Stat tiles ──
  function renderTiles(d) {
    const stats = obj(d.stats), today = obj(stats.today), all = obj(stats.all_time), bot = obj(d.bot);
    countTo($('gifts-today'), whole(today.gifts));
    countTo($('gifts-all'), whole(all.gifts));
    countTo($('drops-today'), whole(today.drops));
    setText($('gifts-all-sub'), whole(all.drops) ? `from ${plural(whole(all.drops), 'drop')} seen` : 'since lurkme started counting');
    setText($('drops-today-sub'), plural(whole(today.subs_dropped), 'sub') + ' given out');

    const wins = whole(all.gifts), drops = whole(all.drops);
    const winsToday = whole(today.gifts), dropsToday = whole(today.drops);
    if (wins) {
      setText($('odds'), '1 in ' + fmtInt(Math.max(1, Math.round(drops / wins))));
      setText($('odds-sub'), winsToday
        ? `today: 1 in ${fmtInt(Math.max(1, Math.round(dropsToday / winsToday)))}`
        : 'drops per gift you win');
    } else {
      setText($('odds'), '—');
      setText($('odds-sub'), drops ? 'No wins yet — keep lurking' : 'No gift drops seen yet');
    }

    const n = state.channels.length, max = whole(bot.max_channels);
    countTo($('chan-count'), n);
    setText($('chan-max'), max ? '/' + fmtInt(max) : '');
    $('chan-meter').style.width = (max ? Math.min(100, (n / max) * 100) : 0).toFixed(1) + '%';
    const track = $('chan-meter-track');
    track.setAttribute('aria-valuenow', String(n));
    track.setAttribute('aria-valuemax', String(max || n));
    const viewers = state.channels.reduce((sum, c) => sum + (c.live && c.viewers ? c.viewers : 0), 0);
    setText($('chan-sub'), viewers ? `${fmtCompact(viewers)} viewers combined` : n ? 'nobody live right now' : 'joining channels…');

    let lurk = num(today.lurk_seconds);
    if (lurk === null) lurk = state.channels.reduce((sum, c) => sum + c.lurk, 0);
    setText($('lurk-today'), hoursOf(lurk));
    setText($('lurk-sub'), 'summed across ' + plural(n, 'channel'));
  }

  // ── Channels ──
  function readChannels(d, now) {
    const keys = new Set(), out = [];
    arr(d.channels).forEach((raw, i) => {
      if (!isObj(raw)) return;
      const login = str(raw.login).trim();
      const name = clip(str(raw.display_name).trim() || login || 'unknown', 60);
      let key = login.toLowerCase() || '#' + i;
      while (keys.has(key)) key += '+';
      keys.add(key);
      const viewers = num(raw.viewers);
      const live = raw.live === true || (raw.live !== false && viewers !== null);
      const game = clip(str(raw.game).trim(), 80), title = clip(str(raw.title).replace(/\s+/g, ' ').trim(), 300);
      let thumb = live ? cdn(raw.thumbnail_url, 320, 180) : null;
      if (thumb && !thumb.includes('?')) thumb += '?t=' + Math.floor(now / THUMB_TTL);  // Fresh preview every 5 min
      out.push({
        key, login, name, game, title, live,
        source: has(SOURCES, raw.source) ? raw.source : 'top',
        viewers: live && viewers !== null && viewers >= 0 ? Math.round(viewers) : null,
        viewersSort: live && viewers !== null ? viewers : -1,
        started: live ? parseIso(raw.started_at) : null,
        lurk: Math.max(0, num(raw.lurk_seconds_today) || 0),
        thumb, avatar: cdn(raw.profile_image_url, 300, 300),
        sortKey: name.toLowerCase(),
        haystack: [name, login, game, title].join('\n').toLowerCase(),
      });
    });
    return out;
  }

  function makeCard() {
    const root = h('article', 'ch enter');
    root.addEventListener('animationend', () => root.classList.remove('enter'));
    const media = h('a', 'ch-media');
    const initial = h('span', 'ch-initial');
    const img = h('img', 'ch-img');
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.referrerPolicy = 'no-referrer';
    const live = h('span', 'badge ch-live');
    const viewers = h('span', 'badge ch-viewers');
    const uptime = h('span', 'badge ch-uptime');
    media.append(initial, img, live, viewers, uptime);

    const avatar = h('img', 'ch-avatar');
    avatar.alt = '';
    avatar.loading = 'lazy';
    avatar.referrerPolicy = 'no-referrer';
    avatar.hidden = true;  // Shown (and so lazily loaded) once there's a URL; fades in when ready
    const name = h('a', 'ch-name'), game = h('p', 'ch-game'), names = h('div', 'ch-names');
    names.append(name, game);
    const src = h('span', 'src');
    const head = h('div', 'ch-head');
    head.append(avatar, names, src);
    const title = h('p', 'ch-title');
    const hot = h('span', 'ch-hot');
    hot.hidden = true;
    const lurk = h('span', 'ch-lurk'), bar = h('span', 'ch-bar'), fill = h('span'), foot = h('div', 'ch-foot');
    bar.append(fill);
    foot.append(lurk, bar);
    const body = h('div', 'ch-body');
    body.append(head, title, hot, foot);
    root.append(media, body);

    const card = {root, media, initial, img, live, viewers, uptime, avatar, name, game, src, title, hot, lurk, fill, foot,
                  images: [], imageIndex: 0, imageKey: null, avatarUrl: null, started: null};
    img.addEventListener('load', () => img.classList.add('ready'));
    img.addEventListener('error', () => showImage(card, card.imageIndex + 1));
    avatar.addEventListener('load', () => avatar.classList.add('ready'));
    avatar.addEventListener('error', () => { avatar.hidden = true; });
    return card;
  }

  function showImage(card, index) {  // Stream preview, then profile picture, then the coloured initial underneath
    card.imageIndex = index;
    const url = card.images[index];
    if (url) {
      card.img.src = url;  // A loaded image stays on screen until its replacement has loaded
    } else {
      card.img.classList.remove('ready');
      card.img.removeAttribute('src');
    }
  }

  function updateCard(card, c, now, hot) {
    card.root.classList.toggle('offline', !c.live);
    card.root.classList.toggle('live', c.live);
    setLink(card.media, c.login);
    setLink(card.name, c.login);
    card.media.setAttribute('aria-label', `${c.name} on Twitch`);
    setText(card.name, c.name);
    setText(card.game, c.game || (c.live ? 'No category' : 'Offline'));
    setText(card.title, c.title);
    card.title.title = c.title;
    card.title.hidden = !c.title;
    setText(card.live, c.live ? 'LIVE' : 'OFFLINE');
    setText(card.viewers, c.viewers === null ? '' : fmtCompact(c.viewers));
    card.viewers.title = c.viewers === null ? '' : plural(c.viewers, 'viewer');
    card.viewers.hidden = c.viewers === null;
    card.started = c.started;
    updateUptime(card, now);
    setText(card.src, SOURCES[c.source]);
    card.src.dataset.source = c.source;

    setText(card.initial, initialOf(c.name));
    card.initial.style.setProperty('--hue', String(hueOf(c.login || c.name)));
    const images = [c.thumb, c.avatar].filter(Boolean);
    const imageKey = images.join(' ');
    if (imageKey !== card.imageKey) {
      card.imageKey = imageKey;
      card.images = images;
      showImage(card, 0);
    }
    if (c.avatar !== card.avatarUrl) {
      card.avatarUrl = c.avatar;
      card.avatar.classList.remove('ready');
      card.avatar.hidden = !c.avatar;
      if (c.avatar) card.avatar.src = c.avatar;
      else card.avatar.removeAttribute('src');
    }

    const elapsed = Math.max(60, now % 86400);  // Seconds since 00:00 UTC
    const share = Math.min(1, c.lurk / elapsed);
    setText(card.lurk, hoursOf(c.lurk) + ' today');
    card.fill.style.width = (share * 100).toFixed(1) + '%';
    card.foot.title = `In chat for ${Math.round(share * 100)}% of today (UTC)`;
    const subs = hot ? whole(hot.subs) : 0;
    card.hot.hidden = !subs;
    if (subs) setText(card.hot, `${plural(subs, 'sub')} dropped today`);
  }

  function updateUptime(card, now) {
    const text = card.started === null ? '' : clock(now - card.started);
    setText(card.uptime, text);
    card.uptime.hidden = !text;
    card.uptime.title = text ? 'Live for ' + text : '';
  }

  function renderChannels(d) {
    const now = serverNow();
    const hot = new Map();
    arr(obj(obj(d.stats).today).top_channels).forEach((row) => {
      if (isObj(row) && str(row.channel)) hot.set(str(row.channel).toLowerCase(), row);
    });
    state.channels = readChannels(d, now);
    const keep = new Set();
    for (const c of state.channels) {
      let card = state.cards.get(c.key);
      if (!card) {
        card = makeCard();
        state.cards.set(c.key, card);
      }
      updateCard(card, c, now, hot.get(c.login.toLowerCase()));
      keep.add(c.key);
    }
    for (const [key, card] of Array.from(state.cards)) {
      if (!keep.has(key)) {
        card.root.remove();
        state.cards.delete(key);
      }
    }
    const counts = {all: state.channels.length, pinned: 0, followed: 0, top: 0};
    let live = 0, viewers = 0;
    for (const c of state.channels) {
      counts[c.source] += 1;
      if (c.live) live += 1;
      viewers += c.viewers || 0;
    }
    for (const [key, chip] of chipButtons()) {
      setText($(chip.id + '-n'), fmtInt(counts[key]));
      chip.hidden = key !== 'all' && !counts[key];
    }
    if (state.source !== 'all' && !counts[state.source]) state.source = 'all';
    const parts = [plural(counts.all, 'channel')];
    if (counts.all) parts.push(`${fmtInt(live)} live`);
    if (viewers) parts.push(`${fmtCompact(viewers)} viewers`);
    setText($('channels-sub'), parts.join(' · '));
    syncControls();
    layoutChannels();
  }

  function layoutChannels() {  // Filter, sort, and move the existing cards into place (no flicker)
    const grid = $('channels');
    const query = state.filter.trim().toLowerCase();
    const matches = state.channels
      .filter((c) => (state.source === 'all' || c.source === state.source) && (!query || c.haystack.includes(query)))
      .sort(SORTS[state.sort]);
    const narrow = !!(window.matchMedia && window.matchMedia('(max-width: 560px)').matches);
    const limit = state.expanded || query ? Infinity : narrow ? 12 : 24;
    const visible = matches.slice(0, limit);
    const more = $('more');
    more.hidden = matches.length <= (narrow ? 12 : 24) || !!query;
    setText(more, state.expanded ? 'Show fewer' : `Show all ${fmtInt(matches.length)} channels`);
    more.setAttribute('aria-expanded', String(state.expanded));
    visible.forEach((c, i) => {
      const node = state.cards.get(c.key).root;
      if (grid.children[i] !== node) grid.insertBefore(node, grid.children[i] || null);
    });
    while (grid.children.length > visible.length) grid.removeChild(grid.lastChild);

    const empty = $('channels-empty');
    if (!state.data) return;
    if (!state.channels.length) {
      setText(empty, 'Not in any channels yet — lurkme joins them right after it connects to Twitch.');
    } else if (!visible.length) {
      setText(empty, query ? `No channels match “${clip(state.filter.trim(), 60)}”.` : 'No channels in this group right now.');
    }
    empty.hidden = visible.length > 0;
  }

  // ── Recent gifts ──
  function renderGifts(d) {
    const now = serverNow();
    const gifts = arr(d.recent_gifts).filter(isObj).slice(0, 100);
    const previous = state.newestGift;
    let newest = previous === null ? 0 : previous;
    const fresh = [];
    state.giftTimes = [];
    const items = gifts.map((g) => {
      const ts = num(g.ts), plan = str(g.plan), months = whole(g.months);
      const li = h('li', 'gift');
      const icon = h('span', 'gift-icon');
      icon.dataset.tier = has(TIER_KEYS, plan) ? TIER_KEYS[plan] : '1';
      const main = h('div', 'gift-main');
      const line = h('p', 'gift-line');
      const tier = (has(TIERS, plan) ? TIERS[plan] : 'Sub') + (months > 1 ? ` · ${fmtInt(months)} months` : '');
      line.append(h('strong', 'gift-who', clip(str(g.gifter).trim() || 'Someone', 60)), h('span', 'tier', tier));
      const meta = h('p', 'gift-meta');
      const channel = str(g.channel).trim();
      meta.append(document.createTextNode('gifted you a sub in '), twitchLink(channel, 'gift-ch', clip(channel || 'a channel', 40)));
      main.append(line, meta);
      const time = h('time', 'gift-time', ago(ts, now));
      if (ts !== null) {
        time.dateTime = isoOf(ts);
        time.title = dateOf(ts, true);
        state.giftTimes.push([time, ts]);
        if (previous !== null && ts > previous) {
          li.classList.add('new');
          fresh.push(g);
        }
        newest = Math.max(newest, ts);
      }
      li.append(icon, main, time);
      return li;
    });
    $('gifts').replaceChildren(...items);
    $('gifts-empty').hidden = items.length > 0;
    setText($('gifts-count'), items.length ? `last ${fmtInt(items.length)}` : '');
    state.latestGift = gifts.reduce((m, g) => (num(g.ts) !== null && (m === null || g.ts > m) ? g.ts : m), null);
    state.newestGift = newest;
    renderGiftAge(now);
    if (fresh.length) celebrate(fresh);
  }

  function renderGiftAge(now) {
    const today = whole(obj(obj(obj(state.data).stats).today).gifts);
    const latest = state.latestGift;
    setText($('gifts-today-sub'), latest !== null && today ? 'latest ' + ago(latest, now) : today ? 'subs gifted to you' : 'none yet today');
  }

  function celebrate(gifts) {
    for (const g of gifts.slice(0, 3)) {
      toast(`🎁 ${clip(str(g.gifter).trim() || 'Someone', 40)} gifted you a sub in ${clip(str(g.channel).trim() || 'a channel', 40)}!`);
    }
    const tile = $('tile-gifts-today');
    tile.classList.remove('celebrate');
    void tile.offsetWidth;  // Restart the animation
    tile.classList.add('celebrate');
    if (reduceMotion) return;
    for (let i = 0; i < 22; i++) {
      const bit = h('span', 'confetti');
      const angle = (i / 22) * Math.PI * 2, distance = 60 + Math.random() * 70;
      bit.style.setProperty('--dx', `${(Math.cos(angle) * distance).toFixed(1)}px`);
      bit.style.setProperty('--dy', `${(Math.sin(angle) * distance - 25).toFixed(1)}px`);
      bit.style.setProperty('--r', `${Math.round(Math.random() * 540 - 270)}deg`);
      bit.style.setProperty('--c', CONFETTI[i % CONFETTI.length]);
      tile.append(bit);
      setTimeout(() => bit.remove(), 1400);
    }
  }

  function toast(text) {
    const box = $('toasts');
    const node = h('div', 'toast', text);
    node.setAttribute('role', 'status');
    box.append(node);
    while (box.children.length > 3) box.removeChild(box.firstChild);
    setTimeout(() => {
      node.classList.add('out');
      setTimeout(() => node.remove(), 400);
    }, 8000);
  }

  // ── Top channels ──
  function renderTop(d) {
    const period = obj(obj(d.stats)[state.tab]);
    const rows = arr(period.top_channels).filter(isObj).slice(0, 10);
    const most = Math.max(1, ...rows.map((r) => whole(r.subs)));
    const items = rows.map((r, i) => {
      const subs = whole(r.subs), drops = whole(r.drops), channel = str(r.channel).trim();
      const li = h('li', 'top-row');
      const main = h('div', 'top-main');
      const line = h('div', 'top-line');
      line.append(twitchLink(channel, 'top-name', clip(channel || '?', 40)), h('span', 'top-num', plural(subs, 'sub')));
      const bar = h('div', 'bar'), fill = h('span');
      fill.style.width = ((subs / most) * 100).toFixed(1) + '%';
      bar.append(fill);
      main.append(line, bar, h('p', 'top-meta', `in ${plural(drops, 'drop')}`));
      li.append(h('span', 'rank', String(i + 1)), main);
      return li;
    });
    $('top-list').replaceChildren(...items);
    const empty = $('top-empty');
    setText(empty, state.tab === 'today' ? 'No gift drops seen yet today' : 'No gift drops seen yet');
    empty.hidden = items.length > 0;
  }

  // ── Footer ──
  function renderFooter(d) {
    const bot = obj(d.bot);
    const langs = arr(bot.languages).map((l) => str(l).trim()).filter(Boolean);
    const cats = arr(bot.categories).map((c) => str(c).trim()).filter(Boolean);
    setText($('f-langs'), !langs.length ? '—' : langs.includes('any') ? 'Any' : clip(langs.map((l) => l.toUpperCase()).join(', '), 200));
    setText($('f-cats'), cats.length ? clip(cats.join(', '), 300) : 'All');
    const discord = $('f-discord');
    setText(discord, bot.discord === true ? 'On' : 'Off');
    discord.dataset.tone = bot.discord === true ? 'ok' : 'off';
    setText($('f-max'), num(bot.max_channels) === null ? '—' : fmtInt(whole(bot.max_channels)));
    setText($('f-restarts'), num(bot.restarts) === null ? '—' : fmtInt(whole(bot.restarts)));
  }

  // ── Controls ──
  const sortButtons = () => [['viewers', $('sort-viewers')], ['name', $('sort-name')], ['lurk', $('sort-lurk')]];
  const chipButtons = () => [['all', $('src-all')], ['pinned', $('src-pinned')], ['followed', $('src-followed')], ['top', $('src-top')]];
  const tabButtons  = () => [['today', $('tab-today')], ['all_time', $('tab-all')]];

  function syncControls() {
    for (const [key, button] of sortButtons()) button.setAttribute('aria-pressed', String(key === state.sort));
    for (const [key, chip] of chipButtons()) chip.setAttribute('aria-pressed', String(key === state.source));
    for (const [key, tab] of tabButtons()) {
      tab.setAttribute('aria-selected', String(key === state.tab));
      tab.tabIndex = key === state.tab ? 0 : -1;
    }
  }

  function selectTab(key, focus) {
    state.tab = key;
    prefs.set('tab', key);
    syncControls();
    if (focus) tabButtons().forEach(([k, tab]) => { if (k === key) tab.focus(); });
    if (state.data) safely(renderTop, state.data);
  }

  // ── Refresh loop ──
  function safely(fn, d) {
    try {
      fn(d);
    } catch (err) {
      console.error('[lurkme] Rendering failed', err);
    }
  }

  function render(d) {
    const first = !state.data;
    state.data = d;
    safely(renderHeader, d);
    safely(renderChannels, d);
    safely(renderTiles, d);
    safely(renderGifts, d);
    safely(renderTop, d);
    safely(renderFooter, d);
    if (first) document.body.classList.remove('loading');
  }

  function tick() {
    const now = serverNow();
    if (state.data) {
      const bot = obj(state.data.bot);
      const started = num(bot.started_at);
      setText($('uptime'), started === null ? '—' : clock(now - started));
      renderToken(bot, now);
      for (const card of state.cards.values()) updateUptime(card, now);
      for (const [node, ts] of state.giftTimes) setText(node, ago(ts, now));
      renderGiftAge(now);
    }
    const age = state.lastOk ? (Date.now() - state.lastOk) / 1000 : null;
    setText($('updated'), age === null ? 'Waiting for data…' : age < 3 ? 'Updated just now' : `Updated ${clock(age)} ago`);
    renderBanner();
  }

  async function refresh() {
    clearTimeout(state.timer);
    state.timer = 0;
    if (state.busy || state.error === 'auth') return;  // A rejected password stays rejected until a reload
    if (document.hidden && state.data) return;  // Hidden tabs resume on visibilitychange
    state.busy = true;
    const abort = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = setTimeout(() => abort && abort.abort(), TIMEOUT_MS);
    try {
      const res = await fetch('api/status', {
        cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'},
        signal: abort ? abort.signal : undefined,
      });
      if (res.status === 401) throw new Error('auth');
      if (!res.ok) throw new Error('http ' + res.status);
      const data = await res.json();
      if (!isObj(data)) throw new Error('bad status data');
      const generated = num(data.generated_at);
      state.offset = generated === null ? 0 : generated - Date.now() / 1000;
      state.lastOk = Date.now();
      state.error = '';
      render(data);
    } catch (err) {
      state.error = err && err.message === 'auth' ? 'auth' : 'offline';  // Keep showing the last good data
    } finally {
      clearTimeout(timeout);
      state.busy = false;
      renderConnection();
      tick();
      // Polling on with the browser's cached (rejected) password would only rack up wrong-password strikes and
      // lock this address out, new password or not. The banner asks for a reload, which brings up the sign-in.
      if (state.error !== 'auth') state.timer = setTimeout(refresh, REFRESH_MS);
    }
  }

  function init() {
    const filter = $('filter');
    filter.addEventListener('input', () => {
      state.filter = String(filter.value || '');
      layoutChannels();
    });
    filter.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        filter.value = '';
        state.filter = '';
        layoutChannels();
      }
    });
    document.addEventListener('keydown', (e) => {
      const t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
      if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        filter.focus();
      }
    });
    for (const [key, button] of sortButtons()) {
      button.addEventListener('click', () => {
        state.sort = key;
        prefs.set('sort', key);
        syncControls();
        layoutChannels();
      });
    }
    $('more').addEventListener('click', () => {
      state.expanded = !state.expanded;
      prefs.set('expanded', state.expanded ? '1' : '0');
      layoutChannels();
    });
    for (const [key, chip] of chipButtons()) {
      chip.addEventListener('click', () => {
        state.source = key;
        syncControls();
        layoutChannels();
      });
    }
    const tabs = tabButtons();
    tabs.forEach(([key, tab], i) => {
      tab.addEventListener('click', () => selectTab(key, false));
      tab.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        selectTab(tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length][0], true);
      });
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh();
    });
    syncControls();
    setInterval(tick, 1000);
    refresh();
  }

  init();
})();
"""

if __name__ == "__main__":
    main()
