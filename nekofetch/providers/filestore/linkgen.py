"""Fstore link generation — encode message ranges as base64 bot deep links.

Usage::

    from nekofetch.providers.filestore import build_fstore_link, pick_fstore_bot_rr

    bot = await pick_fstore_bot_rr(redis, ["KiloxBot", "MarkySayBot"])
    link = build_fstore_link(
        bot_username=bot,
        channel_id=-1001234567890,
        start_msg_id=42,
        end_msg_id=99,
    )
    # -> "https://t.me/KiloxBot?start=Z2V0LTQyMDEyMzQ1Njc4OTAtOTkxMjM0NTY3ODkw"
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis  # noqa: F401 — runtime import via lazy

_RR_KEY_PREFIX = "nf:fstore:rr:"
_RR_KEY_TTL = 86400 * 7  # 7 days — stale keys clean themselves


def _encode(string: str) -> str:
    """URL-safe base64 encode (no padding)."""
    return base64.urlsafe_b64encode(string.encode("ascii")).decode("ascii").strip("=")


def build_fstore_link(
    *,
    bot_username: str,
    channel_id: int,
    start_msg_id: int,
    end_msg_id: int | None = None,
) -> str:
    """Build an Fstore-style deep link for a file or batch of files.

    Parameters
    ----------
    bot_username : str
        The Telegram bot username (e.g. ``"KiloxBot"``).
    channel_id : int
        The storage channel ID (e.g. ``-1001234567890``).
    start_msg_id : int
        First (or only) message ID in the pack.
    end_msg_id : int | None
        Last message ID for a batch range. ``None`` for a single file.

    Returns
    -------
    str
        Full ``https://t.me/...`` deep link.
    """
    cid = abs(channel_id)
    if end_msg_id is not None and end_msg_id != start_msg_id:
        payload = f"get-{start_msg_id * cid}-{end_msg_id * cid}"
    else:
        payload = f"get-{start_msg_id * cid}"
    encoded = _encode(payload)
    return f"https://t.me/{_clean_username(bot_username)}?start={encoded}"


def _clean_username(username: str) -> str:
    """Normalise a bot username for a ``t.me/<username>`` deep link.

    Telegram rejects ``t.me/@name`` (leading ``@``) and any URL with spaces or
    stray commas as ``BUTTON_URL_INVALID``. Config values are entered by hand and
    have shown up as ``"@Name"`` or even a whole comma-joined list stuffed into
    one field, so we defensively strip a leading ``@``, surrounding whitespace,
    and take only the first token before a comma/space — a malformed value can
    still make a *wrong* link, but never an *invalid* one that kills the post."""
    if not username:
        return username
    first = username.strip().lstrip("@").split(",")[0].split()[0]
    return first.lstrip("@")

async def pick_fstore_bot_rr(redis, usernames: list[str]) -> str | None:
    """Pick the next bot from the list using **round-robin** cycling.

    A counter stored in Redis (keyed by a hash of the list contents) is
    atomically incremented on every call, so bots are used in strict rotation:

        A → B → C → A → B → C → ...

    When the bot list changes (add/remove) the hash changes, which implicitly
    resets the counter — the first bot in the new list is picked next.

    Falls back to **random** selection when Redis is unavailable so link
    generation never blocks on a cache failure.

    Parameters
    ----------
    redis : Redis | None
        Redis async client. ``None`` falls back to random.
    usernames : list[str]
        Configured Fstore bot usernames.

    Returns
    -------
    str | None
        Selected bot username, or ``None`` when the list is empty.
    """
    if not usernames:
        return None
    if len(usernames) == 1:
        return usernames[0]
    if redis is None:
        return secrets.choice(usernames)

    # Deterministic key: hash the sorted list so order changes still reset.
    key = _RR_KEY_PREFIX + hashlib.md5(
        ":".join(sorted(usernames)).encode("utf-8")
    ).hexdigest()[:12]

    try:
        idx = await redis.incr(key) - 1  # 0-based
        idx %= len(usernames)
        await redis.expire(key, _RR_KEY_TTL)
        return usernames[idx]
    except Exception:
        return secrets.choice(usernames)
