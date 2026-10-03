"""Tests for dashboard.py — the local web dashboard (routes, auth, headers, and the page's own script)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402
from aiohttp.http_exceptions import BadHttpMessage as BadRequest  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import dashboard  # noqa: E402

NODE     = shutil.which("node")
MIN_NODE = 16  # The page harness uses AbortController and performance as globals
PASSWORD = "correct horse battery staple"
CDN      = "https://static-cdn.jtvnw.net/"
TWITCH   = "https://www.twitch.tv/"
PATHS    = ["/", "/app.js", "/app.css", "/api/status", "/healthz"]

EXPECTED_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' https://static-cdn.jtvnw.net data:; connect-src 'self'; "
                                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"),
    "X-Content-Type-Options":  "nosniff",
    "Referrer-Policy":         "no-referrer",
    "Cache-Control":           "no-store",
}

def summary(gifts=0, drops=0, subs=0, lurk=0.0, top=None) -> dict:
    return {"gifts": gifts, "drops": drops, "subs_dropped": subs, "lurk_seconds": lurk, "top_channels": top or []}

def sample_status(connected: bool = True) -> dict:
    return {
        "generated_at": 1791028800.0,
        "bot": {"nick": "lurker", "started_at": 1791000000.0, "connected": connected, "last_data_age": 1.5,
                "token_expires_in": None, "token_auto_refresh": True, "max_channels": 100,
                "languages": ["en"], "categories": [], "discord": False, "restarts": 0},
        "channels": [{"login": "streamer", "display_name": "Streamer", "game": "Chess", "viewers": 1234,
                      "started_at": "2026-10-03T10:00:00Z", "title": "hi", "source": "top", "live": True,
                      "thumbnail_url": CDN + "previews-ttv/live_user_streamer-{width}x{height}.jpg",
                      "profile_image_url": None, "lurk_seconds_today": 600.0}],
        "stats": {"today": summary(1, 4, 40, 600.0, [{"channel": "streamer", "drops": 4, "subs": 40}]),
                  "all_time": summary(3, 90, 900, 9000.0)},
        "recent_gifts": [{"ts": 1791028000.0, "channel": "streamer", "gifter": "Alice", "plan": "1000", "months": 1}],
    }

def basic(password: str, user: str = "anyone") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")

def node_major(version: str | None) -> int:
    """The major version in `node --version` output ("v20.11.1" -> 20), or 0 if it isn't one."""
    match = re.match(r"\s*v?(\d+)\.", version or "")
    return int(match.group(1)) if match else 0

def installed_node_major(command: list[str] | None) -> int:
    """Major version of the node that `command --version` runs; 0 if there's none (or it won't say)."""
    if not command:
        return 0
    try:
        result = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    return node_major(result.stdout)

NODE_MAJOR = installed_node_major([NODE] if NODE else None)
NODE_OK    = NODE_MAJOR >= MIN_NODE
NODE_SKIP  = f"needs node {MIN_NODE}+ (found {f'node {NODE_MAJOR}' if NODE_MAJOR else 'none'})"

class DashboardCase(unittest.IsolatedAsyncioTestCase):
    """Starts create_app(...) behind an aiohttp TestClient; tests call self.serve() first."""

    async def serve(self, get_status=sample_status, password: str | None = None, **kwargs) -> TestClient:
        self.logged: list[str] = []
        kwargs.setdefault("log", self.logged.append)
        self.client = TestClient(TestServer(dashboard.create_app(get_status, password, **kwargs)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        return self.client

    def assert_security_headers(self, resp, where: str):
        for name, value in EXPECTED_HEADERS.items():
            self.assertEqual(resp.headers.get(name), value, f"{name} on {where}")
        self.assertNotIn("aiohttp", resp.headers.get("Server", "").lower(), where)
        self.assertNotIn("python", resp.headers.get("Server", "").lower(), where)

# ── Routes ────────────────────────────────────────────────────────────────────

class RoutesTest(DashboardCase):

    async def test_page_and_assets_have_the_right_content_types(self):
        client = await self.serve()
        for path, ctype in (("/", "text/html"), ("/app.js", "application/javascript"), ("/app.css", "text/css"),
                            ("/api/status", "application/json")):
            resp = await client.get(path)
            self.assertEqual(resp.status, 200, path)
            self.assertEqual(resp.content_type, ctype, path)
            self.assertEqual(resp.charset, "utf-8", path)
        resp = await client.get("/")
        self.assertEqual(resp.headers["Content-Type"], "text/html; charset=utf-8")
        page = await resp.text()
        self.assertIn('<script src="app.js" defer></script>', page)
        self.assertIn('<link rel="stylesheet" href="app.css">', page)
        self.assertEqual(await (await client.get("/app.js")).text(), dashboard.APP_JS)
        self.assertEqual(await (await client.get("/app.css")).text(), dashboard.APP_CSS)

    async def test_status_is_passed_through_unchanged(self):
        client = await self.serve()
        resp = await client.get("/api/status")
        self.assertEqual(await resp.json(), sample_status())  # Nothing added (no secrets), nothing lost

    async def test_status_is_strict_json_even_with_odd_values(self):
        class Secret:
            def __repr__(self):
                return "token=hunter2"

        def odd():
            return {"nan": float("nan"), "inf": float("inf"), "when": datetime(2026, 10, 3, tzinfo=timezone.utc),
                    "tags": {"b", "a"}, "pair": (1, 2), 5: "int key", "obj": Secret(), "nested": [{"x": -math.inf}]}

        client = await self.serve(odd)
        resp = await client.get("/api/status")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertNotIn("hunter2", text)

        def reject(name):
            raise ValueError(f"non-standard JSON constant {name}")

        data = json.loads(text, parse_constant=reject)
        self.assertEqual(data, {"nan": None, "inf": None, "when": "2026-10-03T00:00:00+00:00", "tags": ["a", "b"],
                                "pair": [1, 2], "5": "int key", "obj": None, "nested": [{"x": None}]})

    async def test_get_status_errors_return_500_without_details(self):
        def broken():
            raise RuntimeError("sqlite exploded at /secret/path token=abc123")

        client = await self.serve(broken)
        for _ in range(3):
            resp = await client.get("/api/status")
            self.assertEqual(resp.status, 500)
            self.assertEqual(resp.content_type, "application/json")
            body = await resp.text()
            self.assertEqual(json.loads(body), {"error": "status unavailable"})
            self.assertNotIn("exploded", body)
            self.assertNotIn("abc123", body)
            self.assert_security_headers(resp, "500")
        self.assertEqual(len(self.logged), 1, "the same failure is logged once")  # Server-side only
        self.assertIn("RuntimeError", self.logged[0])

    async def test_get_status_returning_a_non_dict_is_an_error(self):
        for value in (None, [], "status", 42):
            client = await self.serve(lambda value=value: value)
            resp = await client.get("/api/status")
            self.assertEqual(resp.status, 500, repr(value))
            self.assertEqual(await resp.json(), {"error": "status unavailable"})
            await client.close()

    async def test_async_get_status_is_supported(self):
        async def status():
            return sample_status()

        client = await self.serve(status)
        self.assertEqual(await (await client.get("/api/status")).json(), sample_status())
        self.assertEqual((await client.get("/healthz")).status, 200)

    async def test_healthz(self):
        cases = [
            (lambda: sample_status(connected=True), 200, "ok"),
            (lambda: sample_status(connected=False), 503, "disconnected"),
            (lambda: {"bot": {"connected": "yes"}}, 503, "disconnected"),  # Only a real True counts
            (lambda: {"bot": None}, 503, "disconnected"),
            (lambda: {}, 503, "disconnected"),
            (lambda: 1 / 0, 503, "disconnected"),
        ]
        for get_status, status, text in cases:
            client = await self.serve(get_status)
            resp = await client.get("/healthz")
            self.assertEqual((resp.status, await resp.text()), (status, text))
            self.assert_security_headers(resp, "/healthz")
            await client.close()

    async def test_healthz_uses_the_cheap_health_check_when_given(self):
        status_calls = []
        def get_status():
            status_calls.append(1)
            return sample_status(connected=True)
        for health, code in ((lambda: True, 200), (lambda: False, 503), (lambda: "yes", 503), (lambda: 1 / 0, 503)):
            client = await self.serve(get_status, get_health=health)
            self.assertEqual((await client.get("/healthz")).status, code)
            await client.close()
        self.assertEqual(status_calls, [], "the unauthenticated /healthz must not build the full status")

    async def test_security_headers_on_every_response(self):
        client = await self.serve()
        for path in PATHS + ["/missing", "/api/status/../../etc/passwd"]:
            resp = await client.get(path)
            self.assert_security_headers(resp, path)
        for method in ("POST", "PUT", "DELETE"):
            resp = await client.request(method, "/api/status")
            self.assertEqual(resp.status, 405)
            self.assert_security_headers(resp, method)
        resp = await client.head("/")
        self.assertEqual(resp.status, 200)
        self.assert_security_headers(resp, "HEAD /")

    async def test_unknown_routes_404(self):
        client = await self.serve()
        for path in ("/missing", "/api", "/app.js.map", "/.env", "/favicon.ico"):
            self.assertEqual((await client.get(path)).status, 404, path)

    async def test_responses_are_compressed_when_asked(self):
        client = await self.serve()
        resp = await client.get("/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(await resp.text(), dashboard.APP_JS)

# ── Auth ──────────────────────────────────────────────────────────────────────

class AuthTest(DashboardCase):

    async def test_every_route_but_healthz_needs_the_password(self):
        client = await self.serve(password=PASSWORD)
        for path in ["/", "/app.js", "/app.css", "/api/status", "/missing"]:
            resp = await client.get(path)
            self.assertEqual(resp.status, 401, path)
            self.assertTrue(resp.headers["WWW-Authenticate"].startswith('Basic realm="lurkme"'), path)
            self.assert_security_headers(resp, f"401 {path}")
            self.assertNotIn(PASSWORD, await resp.text())
        resp = await client.get("/healthz")
        self.assertEqual((resp.status, await resp.text()), (200, "ok"))

    async def test_correct_password_with_any_username(self):
        client = await self.serve(password=PASSWORD)
        for user in ("anyone", "", "admin", "名前"):
            for path in PATHS:
                resp = await client.get(path, headers={"Authorization": basic(PASSWORD, user)})
                self.assertEqual(resp.status, 200, (user, path))
                self.assert_security_headers(resp, path)
        resp = await client.get("/api/status", headers={"Authorization": basic(PASSWORD)})
        self.assertEqual(await resp.json(), sample_status())

    async def test_scheme_is_case_insensitive(self):
        client = await self.serve(password=PASSWORD)
        token = basic(PASSWORD).split(" ", 1)[1]
        for scheme in ("basic", "BASIC", "bAsIc"):
            resp = await client.get("/", headers={"Authorization": f"{scheme} {token}"})
            self.assertEqual(resp.status, 200, scheme)

    async def test_unusual_passwords(self):
        for password in ("pä55wörd✓", "with:colons:inside", "x" * 300, " spaced "):
            client = await self.serve(password=password)
            self.assertEqual((await client.get("/", headers={"Authorization": basic(password)})).status, 200, password)
            self.assertEqual((await client.get("/", headers={"Authorization": basic(password[:-1])})).status, 401)
            await client.close()

    async def test_wrong_and_malformed_credentials_are_rejected(self):
        b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
        headers = [
            basic("wrong"), basic(PASSWORD + " "), basic(PASSWORD.upper()), basic(""),
            "Basic", "Basic ", "Basic !!!not-base64!!!", "Basic " + b64(PASSWORD.encode()),  # No colon at all
            "Basic " + b64(PASSWORD.encode())[:-2], "Bearer " + b64(b"x:" + PASSWORD.encode()),
            b64(b"x:" + PASSWORD.encode()), "Basic " + b64(b"x:\xff\xfe" + PASSWORD.encode()),
            "Basic \u00e9\u00e9\u00e9", "Digest username=x",
        ]
        for header in headers:  # A fresh app each time, so the lockout doesn't kick in
            client = await self.serve(password=PASSWORD)
            resp = await client.get("/api/status", headers={"Authorization": header})
            self.assertEqual(resp.status, 401, header)
            self.assertNotIn("Traceback", await resp.text())
            await client.close()

    async def test_empty_password_means_no_auth(self):
        client = await self.serve(password="")
        self.assertEqual((await client.get("/")).status, 200)

    async def test_repeated_wrong_passwords_lock_the_address_out(self):
        client = await self.serve(password=PASSWORD)
        for i in range(dashboard.AUTH_MAX_FAILURES):
            self.assertEqual((await client.get("/", headers={"Authorization": basic(f"guess {i}")})).status, 401)
        for headers in ({"Authorization": basic("guess")}, {"Authorization": basic(PASSWORD)}, {}):
            resp = await client.get("/", headers=headers)
            self.assertEqual(resp.status, 429)  # Even the right password waits out the lockout
            self.assertGreater(int(resp.headers["Retry-After"]), 0)
            self.assert_security_headers(resp, "429")
        self.assertEqual((await client.get("/healthz")).status, 200)

    async def test_a_stale_tab_with_the_old_password_doesnt_lock_the_owner_out(self):
        # The password changed while a dashboard tab stayed open: that tab keeps sending the old one
        client = await self.serve(password=PASSWORD)
        for _ in range(dashboard.AUTH_MAX_FAILURES * 3):
            resp = await client.get("/api/status", headers={"Authorization": basic("the old password")})
            self.assertEqual(resp.status, 401)
        self.assertEqual((await client.get("/", headers={"Authorization": basic(PASSWORD)})).status, 200)
        for i in range(dashboard.AUTH_MAX_FAILURES):  # Real guessing still locks out
            await client.get("/", headers={"Authorization": basic(f"guess {i}")})
        self.assertEqual((await client.get("/", headers={"Authorization": basic(PASSWORD)})).status, 429)

    async def test_requests_without_credentials_dont_count_as_failures(self):
        client = await self.serve(password=PASSWORD)
        for _ in range(dashboard.AUTH_MAX_FAILURES * 3):
            self.assertEqual((await client.get("/")).status, 401)
        self.assertEqual((await client.get("/", headers={"Authorization": basic(PASSWORD)})).status, 200)

    def test_lockout_window(self):
        now = [1000.0]
        lockout = dashboard._Lockout(max_failures=3, window=60, clock=lambda: now[0])
        for _ in range(2):
            lockout.failed("1.2.3.4")
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)
        lockout.failed("1.2.3.4")
        self.assertAlmostEqual(lockout.retry_after("1.2.3.4"), 60)
        self.assertEqual(lockout.retry_after("5.6.7.8"), 0)  # Per address
        now[0] += 59
        self.assertAlmostEqual(lockout.retry_after("1.2.3.4"), 1)
        now[0] += 1
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)  # Window over: forgiven
        for _ in range(2):
            lockout.failed("1.2.3.4")
        lockout.succeeded("1.2.3.4")
        lockout.failed("1.2.3.4")
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)  # A success resets the count

    def test_the_same_wrong_attempt_counts_once_per_window(self):
        now = [1000.0]
        lockout = dashboard._Lockout(max_failures=3, window=60, clock=lambda: now[0])
        for _ in range(50):
            lockout.failed("1.2.3.4", basic("old password"))
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)
        self.assertNotIn("old password", repr(lockout._failures))  # Kept as a keyed hash only
        self.assertNotIn(basic("old password"), repr(lockout._failures))
        lockout.failed("1.2.3.4", basic("old password", user="someone else"))  # A different attempt
        lockout.failed("5.6.7.8", basic("old password"))  # Per address
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)
        lockout.failed("1.2.3.4", basic("guess"))
        self.assertAlmostEqual(lockout.retry_after("1.2.3.4"), 60)
        now[0] += 60
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)
        for _ in range(2):  # A new window: the old password counts once again
            lockout.failed("1.2.3.4", basic("old password"))
        lockout.failed("1.2.3.4", basic("guess"))
        self.assertEqual(lockout.retry_after("1.2.3.4"), 0)
        lockout.failed("1.2.3.4")  # No attempt given: always counts
        self.assertAlmostEqual(lockout.retry_after("1.2.3.4"), 60)

    def test_lockout_memory_is_bounded(self):
        now = [0.0]
        lockout = dashboard._Lockout(clock=lambda: now[0])
        for i in range(dashboard.AUTH_MAX_TRACKED * 3):
            now[0] += 0.001
            lockout.failed(f"10.0.{i // 256}.{i % 256}")
        self.assertLessEqual(len(lockout._failures), dashboard.AUTH_MAX_TRACKED + 1)

    def test_password_parsing(self):
        parse = dashboard._password_from
        self.assertEqual(parse(basic("pw")), b"pw")
        self.assertEqual(parse(basic("a:b:c")), b"a:b:c")
        self.assertEqual(parse(basic("", "")), b"")
        self.assertEqual(parse("  Basic   " + basic("pw").split()[1] + "  "), b"pw")
        for bad in ("", "Basic", "Basic ===", "Basic e30", "Token abc", "Basic Zm9v", "Basic é"):
            self.assertIsNone(parse(bad), bad)

# ── DNS rebinding guard ───────────────────────────────────────────────────────

class HostTest(DashboardCase):

    async def test_without_a_password_only_local_hosts_are_served(self):
        client = await self.serve()
        for host in ("localhost", "localhost:8787", "127.0.0.1:8787", "[::1]:8787", "lurkme.localhost:80",
                     "LOCALHOST.", "10.0.0.5:8787", "::1"):
            self.assertEqual((await client.get("/api/status", headers={"Host": host})).status, 200, host)
        for host in ("evil.example", "evil.example:8787", "127.0.0.1.nip.io:8787", "localhost.evil.example",
                     "attacker-rebind.com"):
            resp = await client.get("/api/status", headers={"Host": host})
            self.assertEqual(resp.status, 403, host)
            self.assert_security_headers(resp, f"403 {host}")
        self.assertEqual((await client.get("/healthz", headers={"Host": "monitor.example"})).status, 200)

    async def test_with_a_password_any_host_is_fine(self):
        client = await self.serve(password=PASSWORD)
        resp = await client.get("/", headers={"Host": "my-vps.example:8787", "Authorization": basic(PASSWORD)})
        self.assertEqual(resp.status, 200)
        self.assertEqual((await client.get("/", headers={"Host": "my-vps.example"})).status, 401)

    def test_is_local_host(self):
        for host in ("localhost", "LocalHost:1", "a.b.localhost", "127.0.0.1", "127.0.0.1:80", "[::1]", "[::1]:9",
                     "::1", "192.168.1.20:8787", "[fe80::1]:80"):
            self.assertTrue(dashboard._is_local_host(host), host)
        for host in ("", None, "example.com", "localhost.com", "127.0.0.1.example", "[evil]:80", "local host"):
            self.assertFalse(dashboard._is_local_host(host), host)

# ── Start / stop ──────────────────────────────────────────────────────────────

class StartStopTest(unittest.IsolatedAsyncioTestCase):

    async def test_start_on_an_ephemeral_port_then_stop(self):
        app    = dashboard.create_app(sample_status)
        runner = await dashboard.start_dashboard(app, "127.0.0.1", 0)
        port   = runner.addresses[0][1]
        self.assertGreater(port, 0)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/healthz") as resp:
                    self.assertEqual((resp.status, await resp.text()), (200, "ok"))
                async with session.get(f"http://127.0.0.1:{port}/api/status") as resp:
                    self.assertEqual(await resp.json(), sample_status())
        finally:
            await dashboard.stop_dashboard(runner)
        async with aiohttp.ClientSession() as session:
            with self.assertRaises((aiohttp.ClientError, OSError)):
                async with session.get(f"http://127.0.0.1:{port}/healthz"):
                    pass

        # The bot restarts in-process: the same port must be free again right away
        runner = await dashboard.start_dashboard(dashboard.create_app(sample_status), "127.0.0.1", port)
        await dashboard.stop_dashboard(runner)

    async def test_busy_port_raises_oserror(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            with self.assertRaises(OSError):
                await dashboard.start_dashboard(dashboard.create_app(sample_status), "127.0.0.1", port)

    async def test_bad_host_raises_oserror(self):
        with self.assertRaises(OSError):
            await dashboard.start_dashboard(dashboard.create_app(sample_status), "256.0.0.1", 0)

# ── Requests aiohttp can't parse ──────────────────────────────────────────────

async def raw_request(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 10)  # aiohttp closes the connection after a 400
    finally:
        writer.close()

class UnparseableRequestTest(unittest.IsolatedAsyncioTestCase):

    async def test_garbage_is_refused_without_logging_a_traceback(self):
        runner = await dashboard.start_dashboard(dashboard.create_app(sample_status, PASSWORD), "127.0.0.1", 0)
        self.addAsyncCleanup(dashboard.stop_dashboard, runner)
        port = runner.addresses[0][1]
        garbage = [
            b"GET /<script>alert(1)</script>\x01 HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /\x00 HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: localhost\r\nBad Header: <b>x</b>\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Long: " + b"a" * 20000 + b"\r\n\r\n",
            b"\x16\x03\x01\x00\xa5\x01\x00\x00\xa1\x03\x03" + bytes(64),  # TLS on the HTTP port
        ]
        # aiohttp's parser answers these itself, before the app sees them, so the reply is aiohttp's plain-text
        # 400 without the app's security headers (see _add_security_headers). What matters: it's refused, the
        # connection is closed, and a scanner can't fill the bot's log with one traceback per request.
        with self.assertNoLogs(level="WARNING"):
            for data in garbage:
                reply = await raw_request(port, data)
                self.assertTrue(reply.startswith(b"HTTP/1.0 400 ") or reply.startswith(b"HTTP/1.1 400 "), reply[:200])
                self.assertNotIn(b"Traceback", reply)
        async with aiohttp.ClientSession() as session:  # Still serving normally afterwards
            async with session.get(f"http://127.0.0.1:{port}/", headers={"Authorization": basic(PASSWORD)}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Security-Policy"], EXPECTED_HEADERS["Content-Security-Policy"])

    def test_real_errors_are_still_logged(self):
        def record(exc: Exception | None) -> logging.LogRecord:
            exc_info = (type(exc), exc, None) if exc else None
            return logging.LogRecord("x", logging.ERROR, __file__, 1, "Error handling request", (), exc_info)

        self.assertFalse(dashboard._not_a_bad_request(record(BadRequest("Invalid char in url path"))))
        self.assertTrue(dashboard._not_a_bad_request(record(RuntimeError("a bug in a handler"))))
        self.assertTrue(dashboard._not_a_bad_request(record(None)))

# ── The page itself (static checks) ───────────────────────────────────────────

class PageSourceTest(unittest.TestCase):

    def test_script_never_parses_html_or_evaluates_strings(self):
        js = dashboard.APP_JS
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                       "createContextualFragment", "DOMParser", "srcdoc", "javascript:", ".srcset",
                       "setAttribute('style'", "setAttribute('on", "setAttribute('href'", "setAttribute('src'"):
            self.assertNotIn(banned, js, banned)
        self.assertIsNone(re.search(r"setTimeout\(\s*['\"`]", js), "setTimeout with a string")

    def test_urls_from_the_data_only_go_through_the_guards(self):
        js = dashboard.APP_JS
        self.assertIn("const CDN        = 'https://static-cdn.jtvnw.net/';", js)
        self.assertIn("const TWITCH     = 'https://www.twitch.tv/';", js)
        self.assertIn("TWITCH + encodeURIComponent(login)", js)
        # Every .src / .href assignment, by the variable it uses
        assigned = sorted(set(re.findall(r"\.(?:src|href) = ([\w.]+);", js)))
        self.assertEqual(assigned, ["c.avatar", "url"])

    def test_page_has_no_inline_code_for_the_csp_to_block(self):
        html = dashboard.PAGE_HTML
        for tag in re.findall(r"<script\b[^>]*>", html):
            self.assertIn(' src="', tag)
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r"\sstyle\s*=", html), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), "inline event handler")
        self.assertIsNone(re.search(r"(?:src|href)=\"(?:https?:)?//", html), "external resource")
        for url in re.findall(r"url\(([^)]*)\)", dashboard.APP_CSS):
            self.assertTrue(url.startswith(('"data:', "'data:")), url)
        self.assertNotIn("@import", dashboard.APP_CSS)

    def test_every_element_the_script_uses_exists(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', dashboard.PAGE_HTML))
        used = set(re.findall(r"\$\('([\w-]+)'\)", dashboard.APP_JS))
        self.assertGreater(len(used), 30)
        self.assertEqual(used - ids, set())

    def test_page_is_responsive_and_dark(self):
        self.assertIn('name="viewport"', dashboard.PAGE_HTML)
        self.assertIn("@media (max-width: 560px)", dashboard.APP_CSS)
        self.assertIn("prefers-reduced-motion", dashboard.APP_CSS)
        self.assertIn("#9146ff", dashboard.APP_CSS.lower())

    @unittest.skipUnless(NODE_OK, NODE_SKIP)
    def test_script_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "app.js")
            path.write_text(dashboard.APP_JS, encoding="utf-8")
            result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

# ── The page's script, run against a fake DOM ─────────────────────────────────

# A tiny DOM for node: enough for app.js, and it records anything unsafe the script does
# (HTML sinks, URLs that aren't Twitch's CDN / twitch.tv, dangerous attributes) plus every console.error.
HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const [appPath, stepsPath] = process.argv.slice(2);
const CDN = 'https://static-cdn.jtvnw.net/', TWITCH = 'https://www.twitch.tv/';
const violations = [], errors = [], imgSrcs = [], hrefs = [], fetches = [];

class FakeText {
  constructor(text) { this.data = String(text); this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}
class ClassList {
  constructor() { this.items = new Set(); }
  add(...c) { c.forEach((x) => this.items.add(String(x))); }
  remove(...c) { c.forEach((x) => this.items.delete(String(x))); }
  toggle(c, force) { const on = force === undefined ? !this.items.has(c) : !!force; on ? this.items.add(c) : this.items.delete(c); return on; }
  contains(c) { return this.items.has(c); }
}
class Style {
  setProperty(k, v) { if (typeof v !== 'string') violations.push('style.setProperty(' + k + ') with ' + typeof v); this[k] = v; }
}
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null; this.id = '';
    this.classList = new ClassList(); this.style = new Style(); this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.title = ''; this.value = ''; this._src = ''; this._href = '';
  }
  get className() { return Array.from(this.classList.items).join(' '); }
  set className(v) { this.classList.items = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get children() { return this.childNodes.filter((n) => n instanceof FakeElement); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(v) { this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = []; if (v !== null && v !== undefined && String(v)) this.appendChild(new FakeText(v)); }
  _node(n) { if (!(n instanceof FakeElement || n instanceof FakeText)) throw new TypeError('not a node: ' + String(n)); return n; }
  appendChild(n) { this._node(n); if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === 'string' ? new FakeText(n) : n)); }
  insertBefore(n, ref) {
    if (ref === null || ref === undefined) return this.appendChild(n);
    this._node(n);
    if (n === ref) return n;
    if (n.parentNode) n.parentNode.removeChild(n);
    const i = this.childNodes.indexOf(ref);
    if (i < 0) throw new Error('insertBefore: reference is not a child');
    n.parentNode = this; this.childNodes.splice(i, 0, n); return n;
  }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i < 0) throw new Error('removeChild: not a child'); this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) { this.textContent = ''; this.append(...nodes); }
  setAttribute(name, value) {
    const k = String(name).toLowerCase();
    if (k.startsWith('on') || ['style', 'src', 'srcset', 'href', 'xlink:href', 'action', 'formaction', 'srcdoc'].includes(k)) violations.push('setAttribute(' + k + ')');
    this.attributes[k] = String(value);
  }
  getAttribute(name) { const k = String(name).toLowerCase(); return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(name) { const k = String(name).toLowerCase(); delete this.attributes[k]; if (k === 'src') this._src = ''; if (k === 'href') this._href = ''; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, extra) { (this.listeners[type] || []).forEach((fn) => fn(Object.assign({type, target: this, preventDefault() {}}, extra || {}))); }
  focus() { document.activeElement = this; }
  get offsetWidth() { return 0; }
  get src() { return this._src; }
  set src(v) { v = String(v); imgSrcs.push(v); if (!v.startsWith(CDN)) violations.push('img.src = ' + v); this._src = v; }
  get href() { return this._href; }
  set href(v) { v = String(v); hrefs.push(v); if (!v.startsWith(TWITCH)) violations.push('a.href = ' + v); this._href = v; }
  get innerHTML() { violations.push('read innerHTML'); return ''; }
  set innerHTML(v) { violations.push('innerHTML'); }
  get outerHTML() { violations.push('read outerHTML'); return ''; }
  set outerHTML(v) { violations.push('outerHTML'); }
  insertAdjacentHTML() { violations.push('insertAdjacentHTML'); }
}

const elements = new Map(), docListeners = {};
const document = {
  hidden: false, title: '', activeElement: null, body: new FakeElement('body'),
  getElementById(id) { if (!elements.has(id)) { const el = new FakeElement(id === 'filter' ? 'input' : 'div'); el.id = id; elements.set(id, el); } return elements.get(id); },
  createElement(tag) { return new FakeElement(tag); },
  createTextNode(text) { return new FakeText(text); },
  addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  write() { violations.push('document.write'); },
  writeln() { violations.push('document.writeln'); },
};
document.body.classList.add('loading');

let timers = [], nextTimer = 1;
const addTimer = (interval) => (fn, ms) => { const id = nextTimer++; timers.push({id, fn, ms: ms || 0, interval}); return id; };
const clearTimer = (id) => { timers = timers.filter((t) => t.id !== id); };
function runTimers(match) {
  const due = timers.filter(match);
  timers = timers.filter((t) => t.interval || !due.includes(t));
  due.forEach((t) => t.fn());
}
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

let step = null;
const sandbox = {
  document, URL, AbortController, performance, queueMicrotask,
  console: {error: (...a) => errors.push(a.map((x) => (x && x.stack) || String(x)).join(' ')), warn() {}, log() {}},
  setTimeout: addTimer(false), setInterval: addTimer(true), clearTimeout: clearTimer, clearInterval: clearTimer,
  requestAnimationFrame: (fn) => { queueMicrotask(() => fn(performance.now() + 60000)); return 1; },
  fetch: async (url) => {
    fetches.push(String(url));
    if (step.kind === 'neterr') throw new TypeError('Failed to fetch');
    const status = step.status || 200;
    const text = typeof step.raw === 'string' ? step.raw : JSON.stringify(step.body);
    return {status, ok: status >= 200 && status < 300, json: async () => sandboxJSON.parse(text)};
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
const sandboxJSON = vm.runInContext('JSON', sandbox);

function find(el, cls, out) {
  out = out || [];
  if (el instanceof FakeElement) {
    if (el.classList.contains(cls)) out.push(el);
    el.childNodes.forEach((n) => find(n, cls, out));
  }
  return out;
}
function info(id) {
  const el = elements.get(id);
  return el ? {text: el.textContent, hidden: !!el.hidden, n: el.children.length, data: Object.assign({}, el.dataset),
               attrs: Object.assign({}, el.attributes), title: el.title} : null;
}
const IDS = ['nick', 'conn', 'conn-label', 'uptime', 'token', 'updated', 'banner', 'banner-text', 'gifts-today',
             'gifts-today-sub', 'gifts-all', 'gifts-all-sub', 'drops-today', 'drops-today-sub', 'odds', 'odds-sub',
             'chan-count', 'chan-max', 'chan-sub', 'lurk-today', 'lurk-sub', 'channels', 'channels-sub',
             'channels-empty', 'more', 'gifts', 'gifts-empty', 'gifts-count', 'top-list', 'top-empty', 'toasts',
             'f-langs', 'f-cats', 'f-discord', 'f-max', 'f-restarts', 'src-all-n', 'src-pinned', 'src-followed',
             'src-top', 'sort-name', 'tab-all', 'tile-gifts-today'];
function snapshot(name) {
  const ids = {};
  IDS.forEach((id) => { ids[id] = info(id); });
  const grid = elements.get('channels');
  return {
    name, ids, title: document.title, loading: document.body.classList.contains('loading'),
    cards: grid ? grid.children.map((card) => ({
      name: find(card, 'ch-name').map((e) => e.textContent).join(''),
      text: card.textContent, offline: card.classList.contains('offline'),
      src: find(card, 'ch-img').map((e) => e.src).join(''),
      hot: find(card, 'ch-hot').map((e) => (e.hidden ? '' : e.textContent)).join(''),
    })) : [],
    celebrating: !!(elements.get('tile-gifts-today') && elements.get('tile-gifts-today').classList.contains('celebrate')),
  };
}
function act(action) {
  const [kind, id, value] = action;
  if (kind === 'input') { const el = elements.get(id); el.value = value; el.fire('input'); }
  else if (kind === 'click') elements.get(id).fire('click');
  else if (kind === 'key') elements.get(id).fire('keydown', {key: value});
  else if (kind === 'dockey') (docListeners.keydown || []).forEach((fn) => fn({key: value, target: document.body, preventDefault() {}}));
  else if (kind === 'hide') { document.hidden = value; (docListeners.visibilitychange || []).forEach((fn) => fn()); }
  else if (kind === 'tick') runTimers((t) => t.interval);
  else throw new Error('unknown action ' + kind);
}

(async () => {
  const steps = JSON.parse(fs.readFileSync(stepsPath, 'utf8'));
  const snapshots = [];
  for (let i = 0; i < steps.length; i++) {
    step = steps[i];
    if (i === 0) vm.runInContext(fs.readFileSync(appPath, 'utf8'), sandbox, {filename: 'app.js'});
    else runTimers((t) => !t.interval && t.ms === 10000);  // The next scheduled refresh
    await flush();
    runTimers((t) => t.interval);  // One clock tick
    await flush();
    snapshots.push(snapshot(step.name));
    for (const action of step.actions || []) {
      act(action);
      await flush();
      snapshots.push(snapshot(step.name + ' > ' + action.join(' ')));
    }
  }
  runTimers((t) => !t.interval && t.ms !== 10000);  // Toast and confetti clean-up
  await flush();
  runTimers((t) => !t.interval && t.ms !== 10000);
  await flush();
  snapshots.push(snapshot('cleanup'));
  const pending = timers.filter((t) => !t.interval && t.ms === 10000).length;
  process.stdout.write(JSON.stringify({snapshots, violations, errors, imgSrcs, hrefs, fetches, pending,
                                       requestedIds: Array.from(elements.keys())}));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

HOSTILE = '<img src=x onerror=alert(1)>'

def hostile_status() -> dict:
    status = sample_status()
    status["bot"].update({"nick": "<script>alert(1)</script>", "languages": ["<b>en</b>"],
                          "categories": ["</dd><img src=x>"]})
    bad_urls = ["javascript:alert(1)", "http://static-cdn.jtvnw.net/x.jpg", "//static-cdn.jtvnw.net/x.jpg",
                "https://static-cdn.jtvnw.net.evil.example/x.jpg", "https://evil.example/https://static-cdn.jtvnw.net/",
                "data:image/svg+xml,<svg onload=alert(1)>", " https://static-cdn.jtvnw.net/x.jpg", "https://static-cdn.jtvnw.net"]
    status["channels"] = [
        {"login": f"../../evil?x={i}#frag", "display_name": HOSTILE, "game": "<script>alert(2)</script>",
         "viewers": 10 - i, "started_at": "not a date", "title": '"><svg onload=alert(3)>', "source": "__proto__",
         "live": True, "thumbnail_url": url, "profile_image_url": url, "lurk_seconds_today": 1e308}
        for i, url in enumerate(bad_urls)
    ] + [{"login": "constructor", "display_name": "constructor", "source": "constructor", "viewers": None,
          "live": True, "lurk_seconds_today": -5},
         {"login": "lone\ud800login", "display_name": "Lone \udc00 name", "source": "followed", "viewers": 3,
          "live": True, "lurk_seconds_today": 60}]  # Lone surrogates: valid JSON, but encodeURIComponent throws
    status["recent_gifts"] = [{"ts": 1791028700.0, "channel": "javascript:alert(1)", "gifter": HOSTILE,
                               "plan": "__proto__", "months": 1e9},
                              {"ts": 1e300, "channel": "x", "gifter": "far future", "plan": "constructor", "months": -1},
                              {"ts": 1791028600.0, "channel": "gift\ud800channel", "gifter": "\udfff", "plan": "1000",
                               "months": 1}]
    status["stats"]["today"]["top_channels"] = [{"channel": "<i>x</i>", "drops": "many", "subs": 1e18},
                                                {"channel": "top\udbffchannel", "drops": 1, "subs": 2}]
    return status

def garbage_channels_status() -> dict:
    status = sample_status()
    status["channels"] = [None, 5, "str", [], {}, {"login": 123, "display_name": None, "viewers": "12",
                                                     "live": "yes", "started_at": 5, "lurk_seconds_today": "1h"},
                          {"login": "dup"}, {"login": "dup"}, {"login": "DUP"}]
    status["stats"] = {"today": [], "all_time": None}
    status["recent_gifts"] = [None, {"ts": "yesterday"}, {}, 7]
    return status

class NodeVersionTest(unittest.TestCase):
    """The page tests need node 16+; older ones (Ubuntu 22.04's apt nodejs is 12) must skip them, not fail."""

    def test_node_major(self):
        self.assertEqual(node_major("v22.22.0\n"), 22)
        self.assertEqual(node_major("v12.22.9"), 12)
        for junk in ("", None, "node", "vX.1", "16"):
            self.assertEqual(node_major(junk), 0, junk)

    def test_an_old_or_missing_node_is_skipped(self):
        old_node = [sys.executable, "-c", "print('v12.22.9')"]
        self.assertEqual(installed_node_major(old_node), 12)
        self.assertLess(installed_node_major(old_node), MIN_NODE)
        self.assertEqual(installed_node_major(None), 0)
        self.assertEqual(installed_node_major([str(Path(tempfile.gettempdir(), "no-such-node-binary"))]), 0)
        self.assertEqual(NODE_OK, NODE_MAJOR >= MIN_NODE)

@unittest.skipUnless(NODE_OK, NODE_SKIP)
class PageScriptTest(unittest.TestCase):
    """Runs the real app.js under node, against a fake DOM that flags anything unsafe."""

    def run_page(self, steps: list[dict], pending: int = 1) -> dict:
        """Run the steps; afterwards `pending` refreshes must be scheduled (1, or 0 once a password was rejected)."""
        with tempfile.TemporaryDirectory() as tmp:
            app, harness, data = Path(tmp, "app.js"), Path(tmp, "harness.js"), Path(tmp, "steps.json")
            app.write_text(dashboard.APP_JS, encoding="utf-8")
            harness.write_text(HARNESS_JS, encoding="utf-8")
            data.write_text(json.dumps(steps), encoding="utf-8")
            result = subprocess.run([NODE, str(harness), str(app), str(data)], capture_output=True, text=True,
                                    timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["violations"], [], "unsafe DOM use")
        self.assertEqual(out["errors"], [], "console.error (a render step threw)")
        for src in out["imgSrcs"]:
            self.assertTrue(src.startswith(CDN), src)
        for href in out["hrefs"]:
            self.assertTrue(href.startswith(TWITCH), href)
            self.assertNotRegex(href[len(TWITCH):], r"[/?#:]")  # One encoded path segment, nothing else
        self.assertEqual(set(out["requestedIds"]) - set(re.findall(r'\bid="([\w-]+)"', dashboard.PAGE_HTML)), set())
        self.assertTrue(all(url == "api/status" for url in out["fetches"]), out["fetches"])
        self.assertEqual(out["pending"], pending, "refreshes still scheduled at the end")
        self.snaps = {snap["name"]: snap for snap in out["snapshots"]}
        return out

    def snap(self, name: str) -> dict:
        return self.snaps[name]

    def toasts(self, name: str) -> int:
        node = self.snap(name)["ids"]["toasts"]
        return node["n"] if node else 0  # Not even looked up until the first toast

    def test_full_demo_data_renders(self):
        demo = dashboard.demo_status()
        self.run_page([{"name": "demo", "body": demo, "actions": [
            ["input", "filter", "aurora"], ["input", "filter", "zzz-no-such-channel"], ["key", "filter", "Escape"],
            ["click", "sort-name"], ["click", "more"], ["click", "src-pinned"], ["click", "src-all"],
            ["click", "tab-all"], ["dockey", "/"],
        ]}])
        snap, ids = self.snap("demo"), self.snap("demo")["ids"]
        self.assertFalse(snap["loading"])
        self.assertEqual(snap["title"], "🎁 2 · lurkme · lurkme_demo")
        self.assertEqual(ids["conn"]["data"]["tone"], "ok")
        self.assertEqual(ids["conn-label"]["text"], "Connected")
        self.assertTrue(ids["banner"]["hidden"])
        self.assertEqual(ids["nick"]["text"], "Lurking as lurkme_demo")
        self.assertEqual(ids["token"]["text"], "Token auto-refresh on")
        self.assertEqual(ids["uptime"]["text"], "3d 4h")
        self.assertEqual(ids["gifts-today"]["text"], "2")
        self.assertEqual(ids["gifts-all"]["text"], "37")
        self.assertEqual(ids["drops-today"]["text"], "57")
        self.assertEqual(ids["drops-today-sub"]["text"], "913 subs given out")
        self.assertEqual(ids["odds"]["text"], "1 in 65")  # round(2412 / 37)
        self.assertEqual(ids["odds-sub"]["text"], "today: 1 in 29")  # round(57 / 2)
        self.assertEqual(ids["chan-count"]["text"], str(len(demo["channels"])))
        self.assertEqual(ids["chan-max"]["text"], "/100")
        self.assertEqual(ids["f-max"]["text"], "100")
        self.assertEqual(ids["f-discord"]["text"], "On")
        self.assertEqual(ids["f-langs"]["text"], "EN")
        self.assertEqual(ids["f-cats"]["text"], "All")

        # 24 cards until "Show all"; live channels first, by viewers; offline ones dimmed
        self.assertEqual(len(snap["cards"]), 24)
        self.assertFalse(ids["more"]["hidden"])
        self.assertEqual(ids["more"]["text"], f"Show all {len(demo['channels'])} channels")
        self.assertEqual(snap["cards"][0]["name"], "AuroraPlays")
        self.assertIn("240 subs dropped today", snap["cards"][0]["hot"])
        self.assertTrue(any(HOSTILE in card["text"] for card in snap["cards"]), "hostile name shown as plain text")
        self.assertEqual(ids["gifts"]["n"], 6)
        self.assertIn("<b>Mallory</b>", ids["gifts"]["text"])
        self.assertEqual(ids["top-list"]["n"], 5)

        self.assertEqual([c["name"] for c in self.snap("demo > input filter aurora")["cards"]], ["AuroraPlays"])
        none = self.snap("demo > input filter zzz-no-such-channel")
        self.assertEqual(none["cards"], [])
        self.assertFalse(none["ids"]["channels-empty"]["hidden"])
        self.assertIn("zzz-no-such-channel", none["ids"]["channels-empty"]["text"])
        self.assertEqual(len(self.snap("demo > key filter Escape")["cards"]), 24)
        names = [c["name"] for c in self.snap("demo > click sort-name")["cards"]]
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertEqual(self.snap("demo > click sort-name")["ids"]["sort-name"]["attrs"]["aria-pressed"], "true")
        expanded = self.snap("demo > click more")
        self.assertEqual(len(expanded["cards"]), len(demo["channels"]))
        self.assertEqual(expanded["ids"]["more"]["text"], "Show fewer")
        pinned = self.snap("demo > click src-pinned")["cards"]
        self.assertEqual(len(pinned), 3)
        self.assertEqual(sum(card["offline"] for card in pinned), 1)
        self.assertEqual(len(self.snap("demo > click src-all")["cards"]), len(demo["channels"]))
        all_time = self.snap("demo > click tab-all")["ids"]
        self.assertIn("8,120 subs", all_time["top-list"]["text"])
        self.assertEqual(all_time["tab-all"]["attrs"]["aria-selected"], "true")

    def test_hostile_strings_stay_text_and_bad_urls_are_dropped(self):
        status = hostile_status()
        self.run_page([{"name": "hostile", "body": status}])  # run_page: no render step threw
        snap = self.snap("hostile")
        self.assertEqual(len(snap["cards"]), len(status["channels"]))
        self.assertEqual(snap["ids"]["gifts"]["n"], len(status["recent_gifts"]))
        self.assertEqual(snap["ids"]["top-list"]["n"], 2)
        self.assertIn("Lone \udc00 name", [card["name"] for card in snap["cards"]])  # Shown, just not linked
        self.assertIn("gift\ud800channel", snap["ids"]["gifts"]["text"])
        self.assertIn("top\udbffchannel", snap["ids"]["top-list"]["text"])
        self.assertTrue(all(card["src"] == "" for card in snap["cards"]), "no image from a non-CDN URL")
        self.assertIn(HOSTILE, " ".join(card["text"] for card in snap["cards"]))
        self.assertIn("<script>alert(1)</script>", snap["ids"]["nick"]["text"])
        self.assertIn(HOSTILE, snap["ids"]["gifts"]["text"])
        self.assertIn("</dd><img src=x>", snap["ids"]["f-cats"]["text"])
        self.assertEqual(snap["ids"]["src-top"]["hidden"], False)  # Unknown sources count as "top"

    def test_missing_and_mistyped_fields_are_tolerated(self):
        out = self.run_page([
            {"name": "empty", "body": {}},
            {"name": "nulls", "body": {"generated_at": None, "bot": None, "channels": None, "stats": None,
                                       "recent_gifts": None}},
            {"name": "wrong types", "body": {"generated_at": "now", "bot": [], "channels": {"a": 1}, "stats": "x",
                                             "recent_gifts": "y"}},
            {"name": "garbage channels", "body": garbage_channels_status()},
            {"name": "good", "body": sample_status()},
        ])
        empty = self.snap("empty")["ids"]
        self.assertEqual(empty["gifts-today"]["text"], "0")
        self.assertEqual(empty["odds"]["text"], "—")
        self.assertEqual(empty["odds-sub"]["text"], "No gift drops seen yet")
        self.assertEqual(empty["conn"]["data"]["tone"], "bad")  # "connected" missing: not connected
        self.assertFalse(empty["channels-empty"]["hidden"])
        self.assertFalse(empty["gifts-empty"]["hidden"])
        self.assertFalse(empty["top-empty"]["hidden"])
        self.assertEqual(empty["nick"]["text"], "Not logged in yet")
        garbage = self.snap("garbage channels")
        self.assertEqual(len(garbage["cards"]), 5)  # Five objects, duplicates kept apart
        good = self.snap("good")
        self.assertEqual([c["name"] for c in good["cards"]], ["Streamer"])
        self.assertEqual(good["cards"][0]["src"],
                         CDN + "previews-ttv/live_user_streamer-320x180.jpg?t=" + str(int(1791028800 // 300)))
        self.assertEqual(good["ids"]["conn"]["data"]["tone"], "ok")
        self.assertEqual(out["errors"], [])

    def test_failed_refresh_keeps_the_last_good_data(self):
        self.run_page([
            {"name": "first fails", "kind": "neterr"},
            {"name": "ok", "body": sample_status()},
            {"name": "server error", "status": 500, "body": {"error": "status unavailable"}},
            {"name": "network down", "kind": "neterr"},
            {"name": "not json", "raw": "<html>proxy error</html>"},
            {"name": "json array", "body": [1, 2, 3]},
            {"name": "recovered", "body": sample_status(connected=False)},
            {"name": "auth", "status": 401, "body": {}},
        ], pending=0)
        first = self.snap("first fails")["ids"]
        self.assertFalse(first["banner"]["hidden"])
        self.assertIn("Can't reach lurkme", first["banner-text"]["text"])
        self.assertTrue(self.snap("first fails")["loading"])
        ok = self.snap("ok")
        self.assertTrue(ok["ids"]["banner"]["hidden"])
        for name in ("server error", "network down", "not json", "json array"):
            snap = self.snap(name)
            self.assertFalse(snap["ids"]["banner"]["hidden"], name)
            self.assertIn("Couldn't refresh", snap["ids"]["banner-text"]["text"], name)
            self.assertEqual(snap["ids"]["conn"]["data"]["tone"], "bad", name)
            self.assertEqual([c["name"] for c in snap["cards"]], ["Streamer"], name)  # Last good data stays
            self.assertEqual(snap["ids"]["gifts-today"]["text"], "1", name)
        recovered = self.snap("recovered")["ids"]
        self.assertTrue(recovered["banner"]["hidden"])
        self.assertEqual(recovered["conn-label"]["text"], "Disconnected from Twitch")
        auth = self.snap("auth")["ids"]
        self.assertIn("password", auth["banner-text"]["text"])
        self.assertEqual(auth["banner"]["data"]["tone"], "bad")

    def test_a_rejected_password_stops_polling(self):
        # Every poll would resend the browser's cached (now wrong) password and count against this address
        out = self.run_page([
            {"name": "ok", "body": sample_status()},
            {"name": "auth", "status": 401, "body": {}, "actions": [["hide", "", True], ["hide", "", False]]},
            {"name": "later", "status": 401, "body": {}},
            {"name": "much later", "body": sample_status()},
        ], pending=0)
        self.assertEqual(len(out["fetches"]), 2)  # The first load and the rejected refresh, then nothing
        later = self.snap("much later")
        self.assertFalse(later["ids"]["banner"]["hidden"])
        self.assertIn("Reload the page", later["ids"]["banner-text"]["text"])
        self.assertEqual(later["ids"]["conn-label"]["text"], "Signed out")
        self.assertEqual([c["name"] for c in later["cards"]], ["Streamer"])  # The last good data stays

    def test_a_new_gift_is_celebrated_once(self):
        before = sample_status()
        after = sample_status()
        after["recent_gifts"].insert(0, {"ts": 1791028790.0, "channel": "streamer", "gifter": HOSTILE,
                                         "plan": "3000", "months": 3})
        after["stats"]["today"]["gifts"] = 2
        self.run_page([{"name": "before", "body": before}, {"name": "after", "body": after},
                       {"name": "again", "body": after}])
        self.assertEqual(self.toasts("before"), 0)  # Gifts already there on load: no toast
        snap = self.snap("after")
        self.assertEqual(self.toasts("after"), 1)
        self.assertIn(HOSTILE, snap["ids"]["toasts"]["text"])
        self.assertTrue(snap["celebrating"])
        self.assertIn("Tier 3 · 3 months", snap["ids"]["gifts"]["text"])
        self.assertEqual(self.toasts("again"), 1)  # Not celebrated twice
        self.assertEqual(self.toasts("cleanup"), 0)  # Toasts go away

    def test_token_countdown(self):
        status = sample_status()
        status["bot"].update({"token_auto_refresh": False, "token_expires_in": 3 * 3600 + 600})
        expiring = sample_status()
        expiring["bot"].update({"token_auto_refresh": False, "token_expires_in": 120})
        forever = sample_status()
        forever["bot"].update({"token_auto_refresh": False, "token_expires_in": None})
        self.run_page([{"name": "hours", "body": status}, {"name": "minutes", "body": expiring},
                       {"name": "forever", "body": forever}])
        self.assertRegex(self.snap("hours")["ids"]["token"]["text"], r"^Token expires in 3h( 9m| 10m)$")
        self.assertEqual(self.snap("hours")["ids"]["token"]["data"]["tone"], "warn")
        self.assertEqual(self.snap("minutes")["ids"]["token"]["data"]["tone"], "bad")
        self.assertEqual(self.snap("forever")["ids"]["token"]["text"], "Token never expires")

    def test_hidden_tab_stops_polling_until_visible(self):
        out = self.run_page([{"name": "ok", "body": sample_status(),
                              "actions": [["hide", "", True], ["hide", "", False]]}])
        self.assertEqual(len(out["fetches"]), 2)  # The initial load, then once on becoming visible again

# ── Demo ──────────────────────────────────────────────────────────────────────

class DemoTest(DashboardCase):

    def test_demo_status_matches_the_contract(self):
        status = dashboard.demo_status()
        json.dumps(status, allow_nan=False)
        self.assertEqual(set(status), {"generated_at", "bot", "channels", "stats", "recent_gifts"})
        self.assertEqual(set(status["bot"]), {"nick", "started_at", "connected", "last_data_age", "token_expires_in",
                                              "token_auto_refresh", "max_channels", "languages", "categories",
                                              "discord", "restarts"})
        channel_keys = {"login", "display_name", "game", "viewers", "started_at", "title", "thumbnail_url",
                        "profile_image_url", "source", "live", "lurk_seconds_today"}
        for channel in status["channels"]:
            self.assertEqual(set(channel), channel_keys)
            self.assertIn(channel["source"], ("pinned", "followed", "top"))
        for period in status["stats"].values():
            self.assertEqual(set(period), {"gifts", "drops", "subs_dropped", "lurk_seconds", "top_channels"})
        self.assertLessEqual(len(status["channels"]), status["bot"]["max_channels"])

    async def test_demo_serves(self):
        client = await self.serve(dashboard.demo_status)
        self.assertEqual((await client.get("/healthz")).status, 200)
        self.assertEqual((await client.get("/api/status")).status, 200)

    def test_main_without_demo_prints_usage(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            dashboard.main([])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("--demo", out.getvalue())

if __name__ == "__main__":
    unittest.main()
