"""
Discord cards
-------------
Builds every Discord webhook payload lurkme posts: gift alerts, the test
alert, the "online" and "needs you" notices, and the daily digest.

Builders are pure (no I/O, no config read at call time) and return a dict
ready for requests.post(webhook, json=payload). Names from Twitch are
markdown-escaped, every embed is clipped to Discord's size limits, and the
only person who can ever be pinged is the user ID passed in.

Wording, where it differs from a plain f-string (copy it to keep other views in step):
  * Counts get thousands separators ("#1,500 this run") and singular forms ("1 drop · 1 sub").
  * Your odds: "1 in N drops" with N = round(drops / gifts) for N >= 2, "Almost every drop" for N == 1
    with more drops than wins, "Every drop you saw" for 0 < drops <= gifts. With no wins, or wins but no
    drops counted, a short note plus the all-time odds on a second line when those exist.
  * Lurk time is summed over every joined chat, so a day at 100 chats is about 2,400h: the field says
    "(all chats)" and anything from a day up is shown in hours.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import quote

TWITCH_PURPLE = 0x9146FF
GREEN         = 0x57F287
RED           = 0xED4245
GOLD          = 0xFEE75C
SUB_TIERS     = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3", "Prime": "Prime"}

USERNAME   = "lurkme"
TWITCH_URL = "https://www.twitch.tv/"
NAME_MAX   = 100  # Twitch names are at most 25 characters; anything longer is junk
ID_LIMIT   = 2**63  # Discord IDs are 64-bit snowflakes; real ones stay below 2**63 until 2084

# Discord's embed limits. Sizes are counted in UTF-16 units, which is never
# less than Discord's own count, so a clipped embed is always accepted.
TITLE_MAX       = 256
DESCRIPTION_MAX = 4096
FIELDS_MAX      = 25
FIELD_NAME_MAX  = 256
FIELD_VALUE_MAX = 1024
FOOTER_MAX      = 2048
AUTHOR_MAX      = 256
EMBED_MAX       = 6000

# ── Text helpers ──────────────────────────────────────────────────────────────

def md(text: str) -> str:
    """Escape Discord markdown so names like some_streamer_ render as typed."""
    return re.sub(r"([\\*_~`|>\[\]()])", r"\\\1", str(text))

def _md_lines(text: str) -> str:
    """md() for free text: also stops a line from turning into a heading, subtext or bullet."""
    return re.sub(r"(?m)^([ \t]*)([#-])", r"\1\\\2", md(text))

def human(seconds: float) -> str:
    """A short duration: "3d 4h", "5h 12m", "42m" or "under a minute"."""
    try:
        total = int(seconds)
    except (TypeError, ValueError, OverflowError):  # None, NaN, infinity
        return "under a minute"
    if total < 60:
        return "under a minute"
    days, rest  = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes     = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"

def _size(text: str) -> int:
    """Length in UTF-16 units: characters outside the BMP (most emoji) count twice."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)

def _clip(text: str, limit: int) -> str:
    """Shorten text to at most `limit` units, ending it with "…"."""
    if _size(text) <= limit:
        return text
    keep, room = 0, limit - 1  # Leave room for the "…"
    for ch in text:
        room -= 2 if ord(ch) > 0xFFFF else 1
        if room < 0:
            break
        keep += 1
    cut = text[:keep].rstrip()
    if (len(cut) - len(cut.rstrip("\\"))) % 2:  # A dangling escape would swallow the "…"
        cut = cut[:-1]
    return cut + "…"

def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default

def _count(n: int, word: str) -> str:
    """A pluralised count: "1 drop", "12 drops", "1,234 subs"."""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"

def _iso(when: float | str | None = None) -> str:
    """An embed timestamp in UTC from an ISO string or a Unix time. None, or anything unreadable, means now
    (Discord rejects the whole message over a bad timestamp)."""
    try:
        if isinstance(when, str):
            stamp = datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", when.strip()))  # 3.10 can't read "Z"
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
        if isinstance(when, (int, float)) and not isinstance(when, bool) and math.isfinite(when):
            return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _channel_url(login: str) -> str:
    return TWITCH_URL + quote(str(login)[:NAME_MAX], safe="", errors="replace")

def _is_https(url: str | None) -> bool:
    return isinstance(url, str) and url.startswith("https://") and not any(ch.isspace() for ch in url)

def _link_or_text(text: str) -> str:
    """A bare URL stays clickable; anything else is escaped like any other text."""
    text = text.strip()
    return text if re.fullmatch(r"https?://\S+", text) else md(text)

def _looks_like_command(text: str) -> bool:
    """A one-line fix that reads like a shell command, not a sentence ("Update your .env, then restart.")."""
    return (len(text.splitlines()) == 1 and not re.match(r"[A-Z][a-z]", text)
            and not re.search(r"\w[.!?]$", text))

def _command(text: str, command: bool | None = None) -> str:
    """Show a shell command as an inline code span that no backtick inside can break out of.
    command=None guesses from the text; True or False says which it is."""
    if command is None:
        command = _looks_like_command(text)
    if not command or len(text.splitlines()) > 1 or "``" in text:
        return _md_lines(text)  # Prose, several lines, or can't be fenced: plain, escaped text
    fence = "`` " if "`" in text else "`"  # Discord's double-backtick span allows a lone ` inside
    body  = _clip(text, FIELD_VALUE_MAX - 2 * len(fence))
    return f"{fence}{body}{fence[::-1]}"

# ── Embed plumbing ────────────────────────────────────────────────────────────

def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value if value.strip() else "—", "inline": inline}  # Discord rejects blanks

def _embed_size(embed: dict) -> int:
    parts  = [embed.get("title", ""), embed.get("description", ""),
              embed.get("footer", {}).get("text", ""), embed.get("author", {}).get("name", "")]
    parts += [field[key] for field in embed.get("fields", []) for key in ("name", "value")]
    return sum(_size(part) for part in parts)

def _fit(embed: dict) -> dict:
    """Clip an embed to Discord's limits, so one long name or error can't get the whole alert rejected."""
    for key, limit in (("title", TITLE_MAX), ("description", DESCRIPTION_MAX)):
        if key in embed:
            embed[key] = _clip(embed[key], limit)
    for key, sub, limit in (("author", "name", AUTHOR_MAX), ("footer", "text", FOOTER_MAX)):
        if key in embed:
            embed[key][sub] = _clip(embed[key][sub], limit)
    if "fields" in embed:
        embed["fields"] = embed["fields"][:FIELDS_MAX]
        for field in embed["fields"]:
            field["name"]  = _clip(field["name"], FIELD_NAME_MAX)
            field["value"] = _clip(field["value"], FIELD_VALUE_MAX)

    # Still over the total: shorten the description, then fields from the bottom up, then the rest
    fields  = embed.get("fields", [])[::-1]
    targets = ([(embed, "description")] + [(f, "value") for f in fields] + [(f, "name") for f in fields]
               + [(embed.get("footer", {}), "text"), (embed.get("author", {}), "name"), (embed, "title")])
    for obj, key in targets:
        excess = _embed_size(embed) - EMBED_MAX
        if excess <= 0:
            break
        if obj.get(key):
            obj[key] = _clip(obj[key], max(_size(obj[key]) - excess, 1))
    return embed

def ping(payload: dict, user_id: str | None) -> dict:
    """Mention user_id and nobody else, or nobody at all: names inside embeds can never ping anyone."""
    user_id = str(user_id).strip() if user_id else ""
    # Discord IDs are plain ASCII digits in snowflake range; it rejects the whole message over anything else
    if user_id.isascii() and user_id.isdigit() and len(user_id) <= 20 and 0 < int(user_id) < ID_LIMIT:
        user_id = str(int(user_id))  # No leading zeros
        payload["content"]          = f"<@{user_id}>"
        payload["allowed_mentions"] = {"users": [user_id]}
    else:
        payload.pop("content", None)
        payload["allowed_mentions"] = {"parse": []}
    return payload

def _payload(embed: dict, user_id: str | None = None) -> dict:
    return ping({"username": USERNAME, "embeds": [_fit(embed)]}, user_id)

# ── Cards ─────────────────────────────────────────────────────────────────────

def gift_card(gift: dict, channel_name: str, avatar_url: str | None, lifetime_gifts: int | None,
              user_id: str | None) -> dict:
    """The alert for one sub gifted to you: who, where, which tier, and your running totals. Pings."""
    gift   = gift if isinstance(gift, dict) else {}
    login  = str(gift.get("channel") or "")
    url    = _channel_url(login)
    name   = _clip(str(channel_name or login or "?"), NAME_MAX)
    gifter = _clip(str(gift.get("gifter") or "Someone"), NAME_MAX)
    months = max(_int(gift.get("months"), 1), 1)
    embed  = {
        "author":      {"name": f"{name} on Twitch", "url": url},
        "title":       "🎁 You got a gifted sub!",
        "url":         url,
        "description": f"**{md(gifter)}** gifted you a sub in **[{md(name)}]({url})**",
        "color":       TWITCH_PURPLE,
        "fields": [
            _field("Tier",   SUB_TIERS.get(str(gift.get("plan")), "Tier 1")),
            _field("Length", f"{months} months" if months > 1 else "1 month"),
            _field("Total",  f"#{_int(gift.get('total')):,} this run"),
        ],
        "footer":      {"text": "lurkme"},
        "timestamp":   _iso(gift.get("time")),
    }
    if lifetime_gifts is not None:
        embed["fields"].append(_field("All-time", f"#{_int(lifetime_gifts):,}"))
    if _is_https(avatar_url):
        embed["author"]["icon_url"] = avatar_url
        embed["thumbnail"]          = {"url": avatar_url}
    return _payload(embed, user_id)

def test_card(user_id: str | None, *, now: float | None = None) -> dict:
    """A sample gift alert for `--test-discord`, to check the webhook and the @mention. Pings."""
    sample  = {"channel": "twitch", "room_id": "", "gifter": "lurkme", "plan": "1000", "months": 1, "total": 1,
               "time": _iso(now)}
    payload = gift_card(sample, "Twitch", None, None, user_id)
    embed   = payload["embeds"][0]
    embed["title"]        = "🧪 Test alert: gift alerts are working"
    embed["description"] += "\n*This is a sample. Real gift alerts look just like this.*"
    return payload

def online_card(nick: str, channels: int, max_channels: int, downtime_seconds: float | None,
                dashboard_hint: str | None, *, now: float | None = None) -> dict:
    """Posted when the bot comes up: who it lurks as, in how many chats, and how long it was down."""
    lines = [f"Lurking as **{md(_clip(str(nick or '?'), NAME_MAX))}** "
             f"in **{_int(channels):,}/{_int(max_channels):,}** channels."]
    if downtime_seconds:
        lines.append(f"Back after being down for {human(downtime_seconds)}.")
    embed = {
        "title":       "🟢 lurkme is online",
        "description": "\n".join(lines),
        "color":       GREEN,
        "footer":      {"text": "lurkme"},
        "timestamp":   _iso(now),
    }
    hint = str(dashboard_hint or "").strip()
    if hint:
        embed["fields"] = [_field("Dashboard", _link_or_text(hint), inline=False)]
    return _payload(embed)

def attention_card(reason: str, fix: str, user_id: str | None, *, now: float | None = None,
                   command: bool | None = None) -> dict:
    """Posted when the bot has stopped for good: what went wrong and how to fix it. Pings.
    A fix that is a shell command shows as code; pass command=True/False when the guess would be wrong."""
    reason = str(reason or "").strip()
    fix    = str(fix or "").strip()
    embed  = {
        "title":       "🔴 lurkme stopped and needs you",
        "description": "It won't start again on its own until this is fixed.",
        "color":       RED,
        "fields":      [_field("What happened", _md_lines(reason) if reason else "No details were given.", inline=False)],
        "footer":      {"text": "lurkme"},
        "timestamp":   _iso(now),
    }
    if fix:
        embed["fields"].append(_field("How to fix", _command(fix, command), inline=False))
    return _payload(embed, user_id)

def _ratio(drops: int, gifts: int) -> str | None:
    """How many gift drops it takes, on average, for one to land on you. None when there's nothing to go on."""
    if gifts <= 0 or drops <= 0:
        return None
    if drops <= gifts:
        return "Every drop you saw"
    n = max(round(drops / gifts), 1)
    return f"1 in {n:,} drops" if n > 1 else "Almost every drop"

def _odds(period: dict, all_time: dict) -> str:
    today = _ratio(_int(period.get("drops")), _int(period.get("gifts")))
    if today:
        return today
    overall = _ratio(_int(all_time.get("drops")), _int(all_time.get("gifts")))
    note    = "Not enough drops seen yet" if _int(period.get("gifts")) > 0 else "No wins yet — keep lurking"
    return note + (f"\nAll-time: {overall}" if overall else "")

def _top_channels(rows: list[dict] | None, limit: int = 5) -> str:
    rows  = rows if isinstance(rows, (list, tuple)) else []
    lines = []
    for row in [r for r in rows if isinstance(r, dict)][:limit]:
        name = md(_clip(str(row.get("channel") or "?"), NAME_MAX))
        subs, drops = _int(row.get("subs")), _int(row.get("drops"))
        lines.append(f"{len(lines) + 1}. **{name}** — {_count(subs, 'sub')} in {_count(drops, 'drop')}")
    return "\n".join(lines) or "No gift drops seen yet"

def _lurk_time(seconds: float) -> str:
    """Lurk time summed over every chat: a day or more is shown in hours ("2,400h"), since "100d" in a
    daily card reads like a bug."""
    total = _int(seconds)  # NaN and infinity count as 0
    return f"{total // 3600:,}h" if total >= 86400 else human(seconds)

def digest_card(nick: str, period: dict, all_time: dict, channels_now: int, max_channels: int,
                uptime_seconds: float, tz_label: str, *, now: float | None = None) -> dict:
    """The daily summary: gifts won, drops seen, your odds, lurk time and the best channels for drops."""
    period      = period if isinstance(period, dict) else {}
    all_time    = all_time if isinstance(all_time, dict) else {}
    drops, subs = _int(period.get("drops")), _int(period.get("subs_dropped"))
    footer      = f"lurkme · {_clip(str(tz_label), NAME_MAX)}" if tz_label else "lurkme"
    embed = {
        "title":       "📊 Daily lurkme digest",
        "description": f"Here's how **{md(_clip(str(nick or '?'), NAME_MAX))}** did today.",
        "color":       GOLD,
        "fields": [
            _field("Gifts won",             f"{_int(period.get('gifts')):,} today · "
                                            f"{_int(all_time.get('gifts')):,} all-time"),
            _field("Gift drops seen",       f"{_count(drops, 'drop')} · {_count(subs, 'sub')} given out"),
            _field("Your odds",             _odds(period, all_time)),
            _field("Lurk time (all chats)", _lurk_time(period.get("lurk_seconds"))),
            _field("Channels",              f"{_int(channels_now):,}/{_int(max_channels):,}"),
            _field("Uptime",                human(uptime_seconds)),
            _field("Top channels for gift drops", _top_channels(period.get("top_channels")), inline=False),
        ],
        "footer":      {"text": footer},
        "timestamp":   _iso(now),
    }
    return _payload(embed)
