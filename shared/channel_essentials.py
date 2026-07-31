"""The channel-creation essentials Senku hands the admin, verbatim from NekoFetch.

NekoFetch's auto-pipeline builds a distribution channel's **title**, **username**,
and **description** from one place: ``BotFactory`` (via ``_gather`` +
``format_bot_name`` / ``format_bot_username``) and its ``_BRANDING_DESCRIPTION``
block. Kuro Sōden is the *manual* version of that same pipeline, so Senku must
surface the identical values for the admin to paste — not a re-derivation that can
drift. This module is the single adapter: it calls the exact NekoFetch functions
and returns their output as a small dataclass.

Nothing here talks to Telegram; it only reads Postgres (storage packs) + config.
Every field is best-effort — a title with no stored packs still yields a usable
name from the franchise's English/romaji, just without the audio/quality suffix.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

_TMDB_SEARCH = "https://www.themoviedb.org/search?query={q}"


@dataclass(slots=True)
class ChannelEssentials:
    """The paste-ready pieces for creating one distribution channel."""

    title: str                 # FINAL channel title the bot sets itself (EN + JP + tags)
    channel_name: str          # plain name suggestion for the admin to create with
    username: str              # best @username candidate (no leading @)
    username_candidates: list[str]  # menu of valid @username options to pick from
    description: str           # channel bio / description block
    poster_search_url: str     # TMDB poster page to open (never auto-copied)


async def build_channel_essentials(
    container: Container, *, anime_doc_id: str | None, franchise: dict | None,
) -> ChannelEssentials:
    """Assemble the channel-creation essentials for a title.

    Reuses :class:`BotFactory` so the manual output matches what the auto-pipeline
    would have produced: ``_gather`` pulls the real audio/language/quality from the
    storage packs, ``format_bot_name`` composes the display name, and
    ``format_bot_username(is_channel=True)`` yields the ``…_axw`` channel handle.
    The description is NekoFetch's configured branding block (operator override or
    the built-in AniXWeebs network block).
    """
    from nekofetch.services.bot_factory import BotFactory
    from nekofetch.services.bot_naming import (
        format_channel_title,
        format_channel_username_candidates,
        format_bot_username,
    )

    franchise = franchise or {}
    english = (franchise.get("english") or franchise.get("title") or "").strip()
    romaji = (franchise.get("romaji") or "").strip()
    native = (franchise.get("native") or franchise.get("title_native") or "").strip()
    synonyms = [s for s in (franchise.get("synonyms") or []) if isinstance(s, str)]

    meta: dict = {}
    if anime_doc_id:
        try:
            # The one place NekoFetch resolves name ingredients from real packs.
            meta = await BotFactory(container)._gather(anime_doc_id)
        except Exception as exc:  # noqa: BLE001 — packs may be absent pre-store
            log.warning("channel_essentials.gather_failed",
                        anime=anime_doc_id, error=str(exc))

    # Prefer the franchise's titles when _gather couldn't resolve them.
    english = (meta.get("english") or "").strip() or english
    romaji = (meta.get("romaji") or "").strip() or romaji
    native = (meta.get("native") or "").strip() or native
    base_title = english or romaji or (anime_doc_id or "Anime")

    # The FINAL title the bot sets itself once it's an admin (EN + JP + tags).
    title = format_channel_title(
        english or base_title, native,
        audios=meta.get("audios") or set(),
        languages=meta.get("languages"),
        qualities=meta.get("qualities"),
    )
    # A menu of valid @username options built from the title's own names; the
    # admin picks whichever Telegram lets them claim (exact handles rarely free).
    candidates = format_channel_username_candidates(
        english=english or base_title, romaji=romaji, synonyms=synonyms,
    )
    if not candidates:
        candidates = [format_bot_username(base_title, anime_doc_id or "",
                                          is_channel=True)]
    username = candidates[0]
    description = _description(container, title=english or base_title)
    poster_url = _TMDB_SEARCH.format(q=quote_plus(base_title))

    return ChannelEssentials(
        title=title,
        channel_name=english or base_title,
        username=username,
        username_candidates=candidates,
        description=description,
        poster_search_url=poster_url,
    )


# Telegram counts a channel description in UTF-16 code units and hard-caps it at
# 255 (setChatDescription rejects anything longer rather than truncating). The
# 𝗯𝗼𝗹𝗱 sans-serif glyphs and emojis in the template are each 2 UTF-16 units, so
# the block is measured in UTF-16 — not len() — and links are dropped in a fixed
# priority order until it fits.
_DESC_LIMIT = 255


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — how Telegram measures a description."""
    return len(text.encode("utf-16-le")) // 2


# The network link block, in REMOVAL order (last entry dropped first when the
# description overflows). Main channel + the closing community line always stay;
# only these four are droppable, in this exact order: Index → Movies → Ongoing →
# Network. Each tuple is (fancy-bold label, @handle).
_DESC_LINKS = [
    ("𝗠𝗮𝗶𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹", "@AniXWeebs"),      # never removed (index 0)
    ("𝗜𝗻𝗱𝗲𝘅", "@AniXWeebs_Index"),
    ("𝗠𝗼𝘃𝗶𝗲𝘀", "@AniMovieXWeebs"),
    ("𝗢𝗻𝗴𝗼𝗶𝗻𝗴", "@Ongoing_AniXWeebs"),
    ("𝗡𝗲𝘁𝘄𝗼𝗿𝗸", "@WeebsXServer"),
]
# Removal sequence by index: Network(4) → Ongoing(3) → Movies(2) → Index(1).
_DESC_DROP_ORDER = [4, 3, 2, 1]

_DESC_HEADER = "𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 {title} 🎐"
_DESC_FOOTER = "ᴊᴏɪɴ ᴏᴜʀ ᴄᴏᴍᴍᴜɴɪᴛʏ ꜰᴏʀ ᴍᴏʀᴇ ᴄᴏɴᴛᴇɴᴛ 💌"


def _render_description(title: str, drop: set[int]) -> str:
    """Assemble the description with the links whose index isn't in ``drop``."""
    lines = [_DESC_HEADER.format(title=title.strip()), ""]
    for i, (label, handle) in enumerate(_DESC_LINKS):
        if i in drop:
            continue
        lines.append(f"➥ {label}: {handle}")
    lines += ["", _DESC_FOOTER]
    return "\n".join(lines)


def _description(container: Container, *, title: str = "") -> str:
    """The per-channel description: the AniXWeebs template with this title baked in.

    An operator override (``bot.description_text``) still wins verbatim. Otherwise
    we build the network-links block from the template and, if the result exceeds
    Telegram's 255-UTF-16-unit cap, drop links in the fixed priority order
    (Index → Movies → Ongoing → Network) until it fits — the anime title and the
    main-channel link are never sacrificed.
    """
    try:
        override = (getattr(container.config.bot, "description_text", "") or "").strip()
    except Exception:  # noqa: BLE001 — config shape guard
        override = ""
    if override:
        # Honour an explicit override but still respect Telegram's hard cap.
        return _fit_utf16(override.format(title=title.strip()) if "{title}" in override
                          else override)

    drop: set[int] = set()
    desc = _render_description(title, drop)
    if _utf16_len(desc) <= _DESC_LIMIT:
        return desc
    # Overflow — drop links one at a time in priority order until it fits.
    for idx in _DESC_DROP_ORDER:
        drop.add(idx)
        desc = _render_description(title, drop)
        if _utf16_len(desc) <= _DESC_LIMIT:
            log.info("channel_essentials.desc_trimmed", dropped=sorted(drop))
            return desc
    # Even with every droppable link gone it's still too long (a very long title).
    # Truncate the title itself as the last resort so the call never gets rejected.
    log.warning("channel_essentials.desc_title_truncated", title=title)
    return _fit_utf16(desc)


def _fit_utf16(text: str) -> str:
    """Hard-trim ``text`` to ``_DESC_LIMIT`` UTF-16 units without splitting a
    surrogate pair (which would corrupt an emoji / fancy-bold glyph)."""
    if _utf16_len(text) <= _DESC_LIMIT:
        return text
    units = text.encode("utf-16-le")
    clipped = units[: _DESC_LIMIT * 2]
    # Never end on a lone high surrogate (0xD800–0xDBFF) — drop it if we did.
    if len(clipped) >= 2:
        last = int.from_bytes(clipped[-2:], "little")
        if 0xD800 <= last <= 0xDBFF:
            clipped = clipped[:-2]
    return clipped.decode("utf-16-le", errors="ignore")
