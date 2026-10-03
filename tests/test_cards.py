"""Tests for cards.py — Discord webhook payload builders."""

from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cards  # noqa: E402

USER_ID = "123456789012345678"
NOW     = 1791028800.0  # 2026-10-03T12:00:00+00:00
NOW_ISO = "2026-10-03T12:00:00+00:00"

# lurker_bot.py's original _md(), kept here so cards.md() can be checked against it
ORIGINAL_MD = re.compile(r"([\\*_~`|>\[\]()])")

def gift(**overrides) -> dict:
    data = {"channel": "some_streamer", "room_id": "12345", "gifter": "Alice", "plan": "1000",
            "months": 1, "total": 3, "time": NOW_ISO}
    data.update(overrides)
    return data

def summary(gifts=0, drops=0, subs=0, lurk=0.0, top=None) -> dict:
    return {"gifts": gifts, "drops": drops, "subs_dropped": subs, "lurk_seconds": lurk, "top_channels": top or []}

def utf16(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2

def fields(payload: dict) -> dict[str, str]:
    return {f["name"]: f["value"] for f in payload["embeds"][0].get("fields", [])}

def all_cards(user_id: str | None = USER_ID, evil: str = "x") -> list[dict]:
    """One payload from every builder, with `evil` wherever user-controlled text can go."""
    top = [{"channel": evil, "drops": 3, "subs": 12}] * 6
    return [
        cards.gift_card(gift(gifter=evil, channel=evil), evil, None, 10, user_id),
        cards.test_card(user_id, now=NOW),
        cards.online_card(evil, 95, 100, 3600, evil, now=NOW),
        cards.attention_card(evil, evil, user_id, now=NOW),
        cards.digest_card(evil, summary(1, 40, 90, 7200, top), summary(5, 400, 900, 9e5, top),
                          95, 100, 86400, evil, now=NOW),
    ]


class PayloadChecks(unittest.TestCase):
    """Shared assertions: valid JSON, Discord's limits, and nobody but USER_ID ever pinged."""

    def assert_valid(self, payload: dict, pinged: str | None = None):
        json.loads(json.dumps(payload))
        self.assertEqual(payload["username"], "lurkme")
        self.assertLessEqual(set(payload), {"username", "embeds", "allowed_mentions", "content"})
        self.assertEqual(len(payload["embeds"]), 1)
        self.assert_within_limits(payload["embeds"][0])
        if pinged:
            self.assertEqual(payload["content"], f"<@{pinged}>")
            self.assertEqual(payload["allowed_mentions"], {"users": [pinged]})
        else:
            self.assertNotIn("content", payload)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def assert_within_limits(self, embed: dict):
        total = 0
        for key, limit in (("title", 256), ("description", 4096)):
            if key in embed:
                self.assertLessEqual(utf16(embed[key]), limit, key)
                total += utf16(embed[key])
        if "footer" in embed:
            self.assertLessEqual(utf16(embed["footer"]["text"]), 2048)
            total += utf16(embed["footer"]["text"])
        if "author" in embed:
            self.assertLessEqual(utf16(embed["author"]["name"]), 256)
            total += utf16(embed["author"]["name"])
        self.assertLessEqual(len(embed.get("fields", [])), 25)
        for field in embed.get("fields", []):
            self.assertTrue(field["name"].strip(), "Discord rejects blank field names")
            self.assertTrue(field["value"].strip(), "Discord rejects blank field values")
            self.assertLessEqual(utf16(field["name"]), 256)
            self.assertLessEqual(utf16(field["value"]), 1024)
            self.assertIsInstance(field["inline"], bool)
            total += utf16(field["name"]) + utf16(field["value"])
        self.assertLessEqual(total, 6000)
        self.assertIsInstance(embed["color"], int)
        datetime.fromisoformat(embed["timestamp"])


class TestMarkdown(unittest.TestCase):

    def test_escapes_every_markdown_character(self):
        for ch in "\\*_~`|>[]()":
            self.assertEqual(cards.md(f"a{ch}b"), f"a\\{ch}b")

    def test_plain_text_is_untouched(self):
        for text in ("Alice", "some streamer 123", "日本語の名前", "émoji 🎁", ""):
            self.assertEqual(cards.md(text), text)

    def test_matches_the_original_lurker_bot_md(self):
        for text in ("some_streamer_", "**bold** ~~x~~ ||spoiler|| > quote", "[link](https://evil)", "a\\_b`c`"):
            self.assertEqual(cards.md(text), ORIGINAL_MD.sub(r"\\\1", text))

    def test_masked_links_cannot_form(self):
        escaped = cards.md("[free nitro](https://evil.example)")
        self.assertEqual(escaped, "\\[free nitro\\]\\(https://evil.example\\)")
        self.assertIsNone(re.search(r"(?<!\\)[\[\]()]", escaped))  # No bracket left unescaped

    def test_md_lines_neutralises_headings_and_bullets(self):
        self.assertEqual(cards._md_lines("# big\n- item\n-# small\n  ## indented"),
                         "\\# big\n\\- item\n\\-# small\n  \\## indented")
        self.assertEqual(cards._md_lines("a-b # c"), "a-b # c")


class TestHuman(unittest.TestCase):

    def test_examples_from_the_spec(self):
        self.assertEqual(cards.human(3 * 86400 + 4 * 3600 + 59), "3d 4h")
        self.assertEqual(cards.human(5 * 3600 + 12 * 60 + 30), "5h 12m")
        self.assertEqual(cards.human(42 * 60), "42m")
        self.assertEqual(cards.human(30), "under a minute")

    def test_boundaries(self):
        self.assertEqual(cards.human(0), "under a minute")
        self.assertEqual(cards.human(59.99), "under a minute")
        self.assertEqual(cards.human(60), "1m")
        self.assertEqual(cards.human(3599), "59m")
        self.assertEqual(cards.human(3600), "1h")
        self.assertEqual(cards.human(3660), "1h 1m")
        self.assertEqual(cards.human(86399), "23h 59m")
        self.assertEqual(cards.human(86400), "1d")
        self.assertEqual(cards.human(90000), "1d 1h")
        self.assertEqual(cards.human(400 * 86400), "400d")

    def test_bad_input_never_raises(self):
        for value in (-5, None, float("nan"), float("inf"), float("-inf"), "junk"):
            self.assertEqual(cards.human(value), "under a minute")
        self.assertEqual(cards.human("120"), "2m")


class TestClip(unittest.TestCase):

    def test_short_text_is_unchanged(self):
        self.assertEqual(cards._clip("hello", 5), "hello")

    def test_long_text_ends_with_ellipsis(self):
        self.assertEqual(cards._clip("hello world", 6), "hello…")
        self.assertEqual(utf16(cards._clip("x" * 5000, 1024)), 1024)

    def test_emoji_count_as_two_units(self):
        clipped = cards._clip("🎁" * 300, 256)
        self.assertLessEqual(utf16(clipped), 256)
        self.assertTrue(clipped.endswith("…"))
        self.assertEqual(cards._size("🎁a"), 3)

    def test_never_leaves_a_dangling_escape(self):
        self.assertEqual(cards._clip(cards.md("ab_" * 100), 4), "ab…")  # Not "ab\…"
        self.assertEqual(cards._clip("a\\\\b" * 10, 4), "a\\\\…")       # An escaped backslash stays whole
        for limit in range(2, 60):
            body = cards._clip(cards.md("x_*`" * 50), limit)[:-1]
            self.assertEqual((len(body) - len(body.rstrip("\\"))) % 2, 0, limit)


class TestPing(PayloadChecks):

    def test_pings_only_the_given_user(self):
        payload = cards.ping({"username": "lurkme"}, USER_ID)
        self.assertEqual(payload["content"], f"<@{USER_ID}>")
        self.assertEqual(payload["allowed_mentions"], {"users": [USER_ID]})

    def test_no_user_means_no_mentions_at_all(self):
        payload = cards.ping({"username": "lurkme", "content": "@everyone"}, None)
        self.assertNotIn("content", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(cards.ping({}, "")["allowed_mentions"], {"parse": []})

    def test_malformed_ids_are_never_pinged(self):
        for bad in ("everyone", "@here", "12a45", "<@123>", "&123", "１２３", "²³", " "):
            payload = cards.ping({}, bad)
            self.assertNotIn("content", payload, bad)
            self.assertEqual(payload["allowed_mentions"], {"parse": []}, bad)

    def test_accepts_int_and_padded_ids(self):
        self.assertEqual(cards.ping({}, f" {USER_ID} ")["content"], f"<@{USER_ID}>")
        self.assertEqual(cards.ping({}, 42)["allowed_mentions"], {"users": ["42"]})

    def test_out_of_range_ids_are_never_pinged(self):
        # Discord answers 400 "is not snowflake" to these, which would sink the whole alert
        for bad in ("9" * 21, str(2**64), str(2**64 - 1), str(2**63), "0", "000", "1" * 5000):
            payload = cards.ping({}, bad)
            self.assertNotIn("content", payload, bad[:30])
            self.assertEqual(payload["allowed_mentions"], {"parse": []}, bad[:30])
        biggest = str(2**63 - 1)
        self.assertEqual(cards.ping({}, biggest)["allowed_mentions"], {"users": [biggest]})

    def test_leading_zeros_are_dropped(self):
        self.assertEqual(cards.ping({}, "0" + USER_ID), {"content": f"<@{USER_ID}>", "allowed_mentions": {"users": [USER_ID]}})
        self.assertEqual(cards.ping({}, "0" * 5 + "42")["allowed_mentions"], {"users": ["42"]})

    def test_out_of_range_id_still_sends_the_card(self):
        for payload in all_cards("9" * 21):
            self.assert_valid(payload, pinged=None)

    def test_returns_the_same_dict(self):
        payload = {}
        self.assertIs(cards.ping(payload, USER_ID), payload)


class TestGiftCard(PayloadChecks):

    def test_payload_shape(self):
        avatar  = "https://static-cdn.jtvnw.net/jtv_user_pictures/x-300x300.png"
        payload = cards.gift_card(gift(), "Some_Streamer", avatar, 57, USER_ID)
        self.assert_valid(payload, pinged=USER_ID)
        embed = payload["embeds"][0]
        url   = "https://www.twitch.tv/some_streamer"
        self.assertEqual(embed["title"], "🎁 You got a gifted sub!")
        self.assertEqual(embed["url"], url)
        self.assertEqual(embed["author"], {"name": "Some_Streamer on Twitch", "url": url, "icon_url": avatar})
        self.assertEqual(embed["thumbnail"], {"url": avatar})
        self.assertEqual(embed["description"], f"**Alice** gifted you a sub in **[Some\\_Streamer]({url})**")
        self.assertEqual(embed["color"], cards.TWITCH_PURPLE)
        self.assertEqual(embed["footer"], {"text": "lurkme"})
        self.assertEqual(embed["timestamp"], NOW_ISO)
        self.assertEqual(embed["fields"], [
            {"name": "Tier",     "value": "Tier 1",      "inline": True},
            {"name": "Length",   "value": "1 month",     "inline": True},
            {"name": "Total",    "value": "#3 this run", "inline": True},
            {"name": "All-time", "value": "#57",         "inline": True},
        ])

    def test_no_ping_without_user(self):
        self.assert_valid(cards.gift_card(gift(), "x", None, None, None), pinged=None)

    def test_all_time_field_only_when_known(self):
        self.assertNotIn("All-time", fields(cards.gift_card(gift(), "x", None, None, None)))
        self.assertEqual(fields(cards.gift_card(gift(), "x", None, 0, None))["All-time"], "#0")
        self.assertEqual(fields(cards.gift_card(gift(total=1500), "x", None, 12345, None)),
                         {"Tier": "Tier 1", "Length": "1 month", "Total": "#1,500 this run", "All-time": "#12,345"})

    def test_tiers(self):
        for plan, tier in (("1000", "Tier 1"), ("2000", "Tier 2"), ("3000", "Tier 3"), ("Prime", "Prime"),
                           ("9999", "Tier 1"), (None, "Tier 1")):
            self.assertEqual(fields(cards.gift_card(gift(plan=plan), "x", None, None, None))["Tier"], tier)

    def test_length_wording(self):
        for months, text in ((1, "1 month"), (3, "3 months"), (12, "12 months"), (0, "1 month"), ("6", "6 months"),
                             (None, "1 month"), ("junk", "1 month")):
            self.assertEqual(fields(cards.gift_card(gift(months=months), "x", None, None, None))["Length"], text)

    def test_names_are_escaped(self):
        payload = cards.gift_card(gift(gifter="**Bob**_"), "[Evil](https://x)", None, None, None)
        description = payload["embeds"][0]["description"]
        self.assertTrue(description.startswith("**\\*\\*Bob\\*\\*\\_** gifted you a sub in **[\\[Evil\\]\\(https://x\\)]("))
        self.assertEqual(payload["embeds"][0]["author"]["name"], "[Evil](https://x) on Twitch")  # Not markdown there

    def test_falls_back_to_login_without_display_name(self):
        payload = cards.gift_card(gift(), "", None, None, None)
        self.assertEqual(payload["embeds"][0]["author"]["name"], "some_streamer on Twitch")

    def test_avatar_must_be_https(self):
        for bad in ("http://example.com/a.png", "javascript:alert(1)", "", "https://a b.png", None):
            embed = cards.gift_card(gift(), "x", bad, None, None)["embeds"][0]
            self.assertNotIn("thumbnail", embed, bad)
            self.assertNotIn("icon_url", embed["author"], bad)

    def test_channel_url_is_encoded(self):
        payload = cards.gift_card(gift(channel="a)b c"), "x", None, None, None)
        self.assertEqual(payload["embeds"][0]["url"], "https://www.twitch.tv/a%29b%20c")
        self.assertIn("](https://www.twitch.tv/a%29b%20c)**", payload["embeds"][0]["description"])

    def test_unix_time_is_converted(self):
        self.assertEqual(cards.gift_card(gift(time=NOW), "x", None, None, None)["embeds"][0]["timestamp"], NOW_ISO)

    def test_huge_names_are_clipped(self):
        payload = cards.gift_card(gift(gifter="A" * 5000, channel="b" * 5000), "C_" * 3000, None, 1, USER_ID)
        self.assert_valid(payload, pinged=USER_ID)
        self.assertIn("…", payload["embeds"][0]["description"])

    def test_missing_channel_or_gift(self):
        data = gift()
        del data["channel"]
        payload = cards.gift_card(data, "", None, None, USER_ID)
        self.assert_valid(payload, pinged=USER_ID)
        self.assertEqual(payload["embeds"][0]["url"], "https://www.twitch.tv/")
        self.assertEqual(payload["embeds"][0]["author"]["name"], "? on Twitch")
        self.assertEqual(cards.gift_card(data, "Named", None, None, None)["embeds"][0]["author"]["name"], "Named on Twitch")
        for junk in (None, {}, "junk"):
            self.assert_valid(cards.gift_card(junk, "x", None, None, None))

    def test_bad_timestamps_fall_back_to_now(self):
        for bad in ("yesterday", "", "  ", "2026-13-45T99:00:00", None, float("nan"), True, [], 1e20):
            stamp = datetime.fromisoformat(cards.gift_card(gift(time=bad), "x", None, None, None)["embeds"][0]["timestamp"])
            self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 5, repr(bad))

    def test_iso_timestamps_are_normalised_to_utc(self):
        for good in ("2026-10-03T12:00:00Z", "2026-10-03T12:00:00", "2026-10-03T14:00:00+02:00",
                     "2026-10-03T12:00:00.123456+00:00", " 2026-10-03T12:00:00+00:00 "):
            self.assertEqual(cards.gift_card(gift(time=good), "x", None, None, None)["embeds"][0]["timestamp"],
                             NOW_ISO, good)

    def test_input_gift_is_not_modified(self):
        data = gift()
        before = dict(data)
        cards.gift_card(data, "x", "https://static-cdn.jtvnw.net/a.png", 5, USER_ID)
        self.assertEqual(data, before)


class TestTestCard(PayloadChecks):

    def test_sample_alert(self):
        payload = cards.test_card(USER_ID, now=NOW)
        self.assert_valid(payload, pinged=USER_ID)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "🧪 Test alert: gift alerts are working")
        self.assertEqual(embed["url"], "https://www.twitch.tv/twitch")
        self.assertEqual(embed["author"]["name"], "Twitch on Twitch")
        self.assertTrue(embed["description"].startswith(
            "**lurkme** gifted you a sub in **[Twitch](https://www.twitch.tv/twitch)**"))
        self.assertIn("sample", embed["description"])
        self.assertEqual(embed["timestamp"], NOW_ISO)
        self.assertEqual(fields(payload), {"Tier": "Tier 1", "Length": "1 month", "Total": "#1 this run"})

    def test_without_user(self):
        self.assert_valid(cards.test_card(None), pinged=None)

    def test_defaults_to_now(self):
        stamp = datetime.fromisoformat(cards.test_card(None)["embeds"][0]["timestamp"])
        self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 5)


class TestOnlineCard(PayloadChecks):

    def test_payload_shape(self):
        payload = cards.online_card("lurk_bot", 95, 100, None, None, now=NOW)
        self.assert_valid(payload, pinged=None)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "🟢 lurkme is online")
        self.assertEqual(embed["color"], cards.GREEN)
        self.assertEqual(embed["description"], "Lurking as **lurk\\_bot** in **95/100** channels.")
        self.assertNotIn("fields", embed)
        self.assertEqual(embed["timestamp"], NOW_ISO)

    def test_downtime_line(self):
        embed = cards.online_card("bot", 100, 100, 5 * 3600 + 12 * 60, None)["embeds"][0]
        self.assertEqual(embed["description"],
                         "Lurking as **bot** in **100/100** channels.\nBack after being down for 5h 12m.")
        for nothing in (None, 0, 0.0):
            self.assertNotIn("Back after", cards.online_card("bot", 1, 100, nothing, None)["embeds"][0]["description"])

    def test_dashboard_field(self):
        url = "http://127.0.0.1:8787/"
        self.assertEqual(fields(cards.online_card("bot", 1, 100, None, url)), {"Dashboard": url})
        hint = "ssh -L 8787:127.0.0.1:8787 my_vps"
        self.assertEqual(fields(cards.online_card("bot", 1, 100, None, hint))["Dashboard"],
                         "ssh -L 8787:127.0.0.1:8787 my\\_vps")
        self.assertNotIn("fields", cards.online_card("bot", 1, 100, None, "   ")["embeds"][0])

    def test_never_pings(self):
        self.assert_valid(cards.online_card("@everyone", 1, 100, 60, "@here"), pinged=None)

    def test_non_string_dashboard_hint(self):
        self.assertEqual(fields(cards.online_card("bot", 1, 100, None, 8787)), {"Dashboard": "8787"})
        self.assertNotIn("fields", cards.online_card("bot", 1, 100, None, 0)["embeds"][0])


class TestAttentionCard(PayloadChecks):

    def test_payload_shape(self):
        payload = cards.attention_card("Token expired", "python3 get_token.py", USER_ID, now=NOW)
        self.assert_valid(payload, pinged=USER_ID)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "🔴 lurkme stopped and needs you")
        self.assertEqual(embed["color"], cards.RED)
        self.assertEqual(embed["timestamp"], NOW_ISO)
        self.assertEqual(embed["fields"], [
            {"name": "What happened", "value": "Token expired",          "inline": False},
            {"name": "How to fix",    "value": "`python3 get_token.py`", "inline": False},
        ])

    def test_no_ping_without_user(self):
        self.assert_valid(cards.attention_card("x", "y", None), pinged=None)

    def test_reason_is_escaped(self):
        value = fields(cards.attention_card("bad_token **now** # yes\n# heading", "fix", None))["What happened"]
        self.assertEqual(value, "bad\\_token \\*\\*now\\*\\* # yes\n\\# heading")

    def test_backticks_cannot_break_the_code_span(self):
        self.assertEqual(fields(cards.attention_card("x", "echo `date`", None))["How to fix"], "`` echo `date` ``")
        self.assertEqual(fields(cards.attention_card("x", "`", None))["How to fix"], "`` ` ``")
        self.assertEqual(fields(cards.attention_card("x", "a ``b`` c", None))["How to fix"], "a \\`\\`b\\`\\` c")

    def test_prose_fix_is_not_shown_as_code(self):
        sentence = "Update your .env with new credentials, then start the bot again."  # lurker_bot.fix_hint()
        self.assertEqual(fields(cards.attention_card("x", sentence, None))["How to fix"], sentence)
        for prose in ("Restart the bot", "Run get_token.py again!", "systemctl restart lurkme.", "Is the token valid?"):
            self.assertNotIn("`", fields(cards.attention_card("x", prose, None))["How to fix"], prose)
        self.assertEqual(fields(cards.attention_card("x", "Run get_token.py", None))["How to fix"], "Run get\\_token.py")

    def test_commands_are_still_code(self):
        for command in ("sudo bash /opt/lurkme/deploy/install.sh --reconfigure", "python3 get_token.py", "cd .",
                        "TWITCH_TOKEN=x python3 lurker_bot.py", "./run.sh", "~/lurkme/start"):
            self.assertEqual(fields(cards.attention_card("x", command, None))["How to fix"], f"`{command}`")

    def test_command_override(self):
        self.assertEqual(fields(cards.attention_card("x", "Restart", None, command=True))["How to fix"], "`Restart`")
        self.assertEqual(fields(cards.attention_card("x", "my_cmd --go", None, command=False))["How to fix"],
                         "my\\_cmd --go")
        self.assertEqual(fields(cards.attention_card("x", "a\nb_c", None, command=True))["How to fix"], "a\nb\\_c")

    def test_multi_line_fix_is_escaped_text(self):
        value = fields(cards.attention_card("x", "1. Run get_token.py\n2. Restart", None))["How to fix"]
        self.assertEqual(value, "1. Run get\\_token.py\n2. Restart")

    def test_long_reason_is_truncated(self):
        payload = cards.attention_card("e" * 5000, "sudo systemctl restart lurkme", USER_ID)
        self.assert_valid(payload, pinged=USER_ID)
        value = fields(payload)["What happened"]
        self.assertEqual(len(value), 1024)
        self.assertTrue(value.endswith("…"))
        self.assertEqual(fields(payload)["How to fix"], "`sudo systemctl restart lurkme`")

    def test_long_fix_keeps_its_code_span_closed(self):
        for fix in ("x" * 5000, "a`b" * 2000):
            value = fields(cards.attention_card("r", fix, None))["How to fix"]
            self.assertLessEqual(len(value), 1024)
            self.assertTrue(value.endswith("…`") or value.endswith("… ``"), value[-10:])

    def test_blank_inputs(self):
        payload = cards.attention_card("", "  ", None)
        self.assert_valid(payload)
        self.assertEqual(fields(payload), {"What happened": "No details were given."})

    def test_mentions_in_reason_never_reach_content(self):
        payload = cards.attention_card("@everyone <@&1> @here", "@everyone", None)
        self.assert_valid(payload, pinged=None)


class TestDigestCard(PayloadChecks):

    def digest(self, period: dict, all_time: dict | None = None, **kw) -> dict:
        args = {"nick": "lurk_bot", "channels_now": 97, "max_channels": 100, "uptime_seconds": 3 * 86400 + 4 * 3600,
                "tz_label": "Europe/Berlin", "now": NOW}
        args.update(kw)
        return cards.digest_card(period=period, all_time=all_time or summary(), **args)

    def test_payload_shape(self):
        top = [{"channel": "big_streamer", "drops": 3, "subs": 120}, {"channel": "small", "drops": 1, "subs": 1}]
        payload = self.digest(summary(2, 40, 300, 5 * 3600 + 12 * 60, top), summary(9, 400, 3000, 1e6, top))
        self.assert_valid(payload, pinged=None)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "📊 Daily lurkme digest")
        self.assertEqual(embed["color"], cards.GOLD)
        self.assertEqual(embed["footer"], {"text": "lurkme · Europe/Berlin"})
        self.assertEqual(embed["timestamp"], NOW_ISO)
        self.assertIn("**lurk\\_bot**", embed["description"])
        self.assertEqual(fields(payload), {
            "Gifts won":       "2 today · 9 all-time",
            "Gift drops seen": "40 drops · 300 subs given out",
            "Your odds":       "1 in 20 drops",
            "Lurk time (all chats)": "5h 12m",
            "Channels":        "97/100",
            "Uptime":          "3d 4h",
            "Top channels for gift drops": "1. **big\\_streamer** — 120 subs in 3 drops\n2. **small** — 1 sub in 1 drop",
        })
        inline = {f["name"]: f["inline"] for f in embed["fields"]}
        self.assertFalse(inline.pop("Top channels for gift drops"))
        self.assertTrue(all(inline.values()))

    def test_odds_math(self):
        cases = [
            (summary(gifts=1, drops=57), "1 in 57 drops"),
            (summary(gifts=3, drops=100), "1 in 33 drops"),
            (summary(gifts=2, drops=5), "1 in 2 drops"),   # round(2.5) == 2
            (summary(gifts=2, drops=7), "1 in 4 drops"),   # round(3.5) == 4
            (summary(gifts=4, drops=5), "Almost every drop"),
            (summary(gifts=2, drops=2), "Every drop you saw"),
            (summary(gifts=3, drops=1), "Every drop you saw"),
            (summary(gifts=1, drops=0), "Not enough drops seen yet"),   # Joined after the drop was announced
            (summary(gifts=1, drops=12345), "1 in 12,345 drops"),
        ]
        for period, text in cases:
            self.assertEqual(fields(self.digest(period))["Your odds"], text, period)

    def test_odds_with_no_wins(self):
        self.assertEqual(fields(self.digest(summary(drops=50)))["Your odds"], "No wins yet — keep lurking")
        self.assertEqual(fields(self.digest(summary()))["Your odds"], "No wins yet — keep lurking")
        self.assertEqual(fields(self.digest(summary(drops=50), summary(gifts=2, drops=300)))["Your odds"],
                         "No wins yet — keep lurking\nAll-time: 1 in 150 drops")

    def test_wins_without_drops_never_claim_every_drop(self):
        payload = self.digest(summary(gifts=1, drops=0), summary(gifts=2, drops=300))
        self.assertEqual(fields(payload)["Gift drops seen"], "0 drops · 0 subs given out")
        self.assertEqual(fields(payload)["Your odds"], "Not enough drops seen yet\nAll-time: 1 in 150 drops")
        payload = self.digest(summary(gifts=1, drops=0), summary(gifts=1, drops=0))
        self.assertEqual(fields(payload)["Your odds"], "Not enough drops seen yet")

    def test_empty_day(self):
        payload = self.digest(summary(), uptime_seconds=30)
        self.assert_valid(payload)
        self.assertEqual(fields(payload), {
            "Gifts won":       "0 today · 0 all-time",
            "Gift drops seen": "0 drops · 0 subs given out",
            "Your odds":       "No wins yet — keep lurking",
            "Lurk time (all chats)": "under a minute",
            "Channels":        "97/100",
            "Uptime":          "under a minute",
            "Top channels for gift drops": "No gift drops seen yet",
        })

    def test_lurk_time_is_summed_over_chats(self):
        for seconds, text in ((100 * 86400, "2,400h"), (86400, "24h"), (86399, "23h 59m"), (42 * 60, "42m"),
                              (float("nan"), "under a minute"), (None, "under a minute")):
            self.assertEqual(fields(self.digest(summary(lurk=seconds)))["Lurk time (all chats)"], text, seconds)
        self.assertNotIn("d", fields(self.digest(summary(lurk=100 * 86400)))["Lurk time (all chats)"])

    def test_singular_wording(self):
        self.assertEqual(fields(self.digest(summary(drops=1, subs=1)))["Gift drops seen"], "1 drop · 1 sub given out")

    def test_big_numbers_get_separators(self):
        payload = self.digest(summary(gifts=1, drops=2000, subs=15000), summary(gifts=1234))
        self.assertEqual(fields(payload)["Gift drops seen"], "2,000 drops · 15,000 subs given out")
        self.assertEqual(fields(payload)["Gifts won"], "1 today · 1,234 all-time")

    def test_top_channels_capped_at_five(self):
        top   = [{"channel": f"ch{i}", "drops": 10 - i, "subs": 100 - i} for i in range(8)]
        value = fields(self.digest(summary(top=top)))["Top channels for gift drops"]
        self.assertEqual(value.count("\n"), 4)
        self.assertTrue(value.startswith("1. **ch0** — 100 subs in 10 drops"))
        self.assertTrue(value.endswith("5. **ch4** — 96 subs in 6 drops"))

    def test_missing_and_null_fields_are_tolerated(self):
        payload = cards.digest_card("bot", {}, None, 0, 100, None, "", now=NOW)
        self.assert_valid(payload)
        self.assertEqual(payload["embeds"][0]["footer"], {"text": "lurkme"})
        payload = self.digest({"gifts": None, "top_channels": [None, {"channel": None}, "junk"]})
        self.assertEqual(fields(payload)["Top channels for gift drops"], "1. **?** — 0 subs in 0 drops")

    def test_wrong_types_are_tolerated(self):
        for period, all_time in (({"top_channels": 5}, {}), ({"top_channels": {"a": 1}}, []), ("junk", 7),
                                 ([1, 2], {"gifts": 2, "drops": 10}), ({"top_channels": "abc"}, None)):
            payload = cards.digest_card("bot", period, all_time, 1, 100, 60, "UTC", now=NOW)
            self.assert_valid(payload)
            self.assertEqual(fields(payload)["Top channels for gift drops"], "No gift drops seen yet")

    def test_hostile_names(self):
        top     = [{"channel": "*_[x](https://evil)_*", "drops": 1, "subs": 1}]
        payload = self.digest(summary(top=top), nick="@everyone", tz_label="Z" * 5000)
        self.assert_valid(payload, pinged=None)
        self.assertIn("**\\*\\_\\[x\\]\\(https://evil\\)\\_\\***", fields(payload)["Top channels for gift drops"])
        self.assertLessEqual(len(payload["embeds"][0]["footer"]["text"]), 2048)


class TestEveryBuilder(PayloadChecks):

    def test_ping_rules(self):
        expected = [USER_ID, USER_ID, None, USER_ID, None]  # gift, test, online, attention, digest
        for payload, pinged in zip(all_cards(USER_ID), expected):
            self.assert_valid(payload, pinged=pinged)
        for payload in all_cards(None):
            self.assert_valid(payload, pinged=None)

    def test_hostile_text_stays_inside_limits_and_out_of_content(self):
        for evil in ("@everyone @here <@&123> <@456>", "_*`|>[]()\\" * 700, "🎁" * 6000, "\n# x\n" * 1000,
                     "x" * 20000, "\ud800 lone surrogate"):
            for payload in all_cards(USER_ID, evil):
                self.assert_valid(payload, pinged=payload.get("content", "")[2:-1] or None)
                self.assertIn(payload.get("content"), (None, f"<@{USER_ID}>"))

    def test_builders_are_deterministic(self):
        self.assertEqual(json.dumps(all_cards()), json.dumps(all_cards()))


class TestFit(PayloadChecks):

    def test_oversized_embed_is_cut_to_every_limit(self):
        embed = {
            "title":       "T" * 1000,
            "description": "D" * 9000,
            "color":       cards.GOLD,
            "fields":      [{"name": "N" * 500, "value": "V" * 2000, "inline": True} for _ in range(40)],
            "footer":      {"text": "F" * 3000},
            "author":      {"name": "A" * 300},
            "timestamp":   NOW_ISO,
        }
        fitted = cards._fit(embed)
        self.assert_within_limits(fitted)
        self.assertEqual(len(fitted["fields"]), 25)
        self.assertEqual(cards._embed_size(fitted), 6000)

    def test_only_the_description_shrinks_when_that_is_enough(self):
        embed  = {"title": "t", "description": "d" * 4096, "fields": [{"name": "n", "value": "v" * 1024}] * 2,
                  "color": 0, "timestamp": NOW_ISO}
        embed["fields"] = [dict(f) for f in embed["fields"]]
        fitted = cards._fit(embed)
        self.assertEqual(cards._embed_size(fitted), 6000)
        self.assertEqual([f["value"] for f in fitted["fields"]], ["v" * 1024] * 2)
        self.assertTrue(fitted["description"].endswith("…"))

    def test_small_embed_is_untouched(self):
        embed = {"title": "hi", "description": "there", "fields": [{"name": "a", "value": "b", "inline": True}]}
        self.assertEqual(cards._fit(json.loads(json.dumps(embed))), embed)

    def test_blank_values_are_replaced(self):
        self.assertEqual(cards._field("Name", "  ")["value"], "—")


if __name__ == "__main__":
    unittest.main()
