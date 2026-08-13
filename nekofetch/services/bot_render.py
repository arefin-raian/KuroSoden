"""Shared channel-post rendering — one home for the *look* of distribution cards.

Two rendering paths post the same content cards into channels:

  * the automated pipeline's distribution bot
    (:mod:`nekofetch.bots.distribution.app`), and
  * Senku's manual publisher (:mod:`kurosoden.shared.senku_publisher`).

Historically each grew its own copy of the keyboard-building logic, so they
drifted — the reference two-per-row quality layout only ever existed in the
preview script, while both live paths dumped every quality button into a single
row. This module is the single source of truth both paths call, so the layout
can never diverge again.

Everything here is driven by :class:`~nekofetch.core.config.PostFormatConfig`
so an operator can retune the look (button rows, resolution labels, language
order, premium emoji) from the Settings panel without a code change.
"""

from __future__ import annotations

import re

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nekofetch.core.config import PostFormatConfig
from nekofetch.localization.messages import M, t

# Reference quality set + ordering, mirrored from bot_content so both the card
# text and the buttons rank resolutions identically.
_RES_ORDER = {"360p": 360, "480p": 480, "540p": 540, "720p": 720, "1080p": 1080, "2160p": 2160}


def sort_qualities(quals) -> list[str]:
    """Sort resolutions low→high by the canonical ladder (unknowns sink last)."""
    return sorted({q for q in quals if q}, key=lambda r: _RES_ORDER.get(r, 9999))


def resolution_label(res: str, fmt: PostFormatConfig) -> str:
    """Render a quality button's visible label via the configurable template.

    ``fmt.resolution_label`` lets operators wrap the raw resolution with symbols
    front/back (e.g. ``「 {res} 」`` or ``⬢ {res}``). A template missing the
    ``{res}`` slot falls back to the bare resolution so a bad override can never
    produce identical, indistinguishable buttons.
    """
    tmpl = fmt.resolution_label or "{res}"
    if "{res}" not in tmpl:
        return res
    return tmpl.replace("{res}", res)


def _chunk_rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    """Split a flat button list into rows of at most ``per_row``.

    With ``per_row=2`` this yields the reference layout the channels use:
    2 buttons → one row ``[2]``; 3 → ``[2, 1]``; 4 → ``[2, 2]``. ``per_row`` is
    clamped to at least 1 so a misconfigured 0 can't wipe every button.
    """
    per_row = max(1, per_row)
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def build_quality_rows(
    qualities: list[str],
    links: dict[str, str],
    fmt: PostFormatConfig,
    *,
    link_key=lambda q: q,
) -> list[list[InlineKeyboardButton]]:
    """Rows of URL quality buttons, chunked ``fmt.buttons_per_row`` per row.

    ``links`` maps a link key → Fstore URL; ``link_key`` derives that key from a
    quality (the separate-audio path passes ``lang_quality``). Qualities without
    a resolved link are dropped — a URL button with an empty ``url`` is invalid.
    """
    row_buttons = [
        InlineKeyboardButton(resolution_label(q, fmt), url=links[link_key(q)])
        for q in qualities
        if links.get(link_key(q))
    ]
    return _chunk_rows(row_buttons, fmt.buttons_per_row)


def _lang_label(language: str, section_label: str, fmt: PostFormatConfig) -> str:
    """Resolve the header label for a language section.

    Prefers the per-language config override, then the label baked into the
    ``button_data`` section (built from ``en.json``), then the ``en.json`` key.
    """
    if language == "japanese":
        return fmt.language_label_japanese or section_label or t(M.BOT_LANG_JAPANESE)
    if language == "english":
        return fmt.language_label_english or section_label or t(M.BOT_LANG_ENGLISH)
    return section_label or language.title()


def build_audio_keyboard(
    button_data: dict | None,
    fmt: PostFormatConfig,
) -> InlineKeyboardMarkup | None:
    """Build the full quality keyboard from a ``button_data`` payload.

    ``flat`` (dual-audio or single track): one wrapped block of quality buttons.

    ``separate_audio`` (sub-only titles with distinct sub & dub packs): a
    language header row followed by that language's wrapped quality buttons, for
    each language. Japanese leads by default (``fmt.japanese_first``) to match
    the reference channels, where original audio is offered first.

    Returns ``None`` when there are no resolved links — a keyboard of dead
    buttons is worse than none, and the caption already lists the qualities.
    """
    if not button_data:
        return None
    # Editors may intentionally replace a generated quality keyboard with a
    # small, explicit set of URL buttons (for example a corrected watch-guide
    # link). Keep this payload deliberately simple and let the shared renderer
    # own the Telegram markup shape.
    if button_data.get("type") == "custom":
        custom_rows = []
        for item in button_data.get("buttons", []):
            if not isinstance(item, dict):
                continue
            label = str(item.get("text") or "").strip()
            url = str(item.get("url") or "").strip()
            if label and url.startswith(("http://", "https://", "tg://")):
                custom_rows.append([InlineKeyboardButton(label[:64], url=url)])
        return InlineKeyboardMarkup(custom_rows) if custom_rows else None

    links: dict[str, str] = button_data.get("links", {})
    if not links:
        return None

    rows: list[list[InlineKeyboardButton]] = []

    if button_data.get("type") == "flat":
        rows.extend(build_quality_rows(button_data.get("qualities", []), links, fmt))

    elif button_data.get("type") == "separate_audio":
        sections = list(button_data.get("sections", []))
        if fmt.japanese_first:
            # Stable sort: Japanese sections bubble to the front, order otherwise kept.
            sections.sort(key=lambda s: 0 if s.get("language") == "japanese" else 1)
        for sec in sections:
            lang = sec.get("language", "")
            qrows = build_quality_rows(
                sec.get("qualities", []), links, fmt,
                link_key=lambda q, _l=lang: f"{_l}_{q}",
            )
            if not qrows:
                continue
            # Language header (visual label). URL buttons can't be inert, so the
            # header carries no link — callers that need a real target override.
            rows.append([InlineKeyboardButton(
                _lang_label(lang, sec.get("label", ""), fmt), callback_data="noop",
            )])
            rows.extend(qrows)

    return InlineKeyboardMarkup(rows) if rows else None


def format_duration(minutes: int | None, fmt: PostFormatConfig) -> str:
    """Human-readable runtime from AniList per-episode minutes.

    ``95`` → ``"1h 35m"``; ``24`` → ``"24m"``. Returns ``"—"`` when unknown so a
    movie card never renders the old ``1h {episode_count}m`` bug (episode counts
    were being fed in as minutes).
    """
    if not minutes or minutes <= 0:
        return "—"
    h, m = divmod(int(minutes), 60)
    if h >= 1:
        return fmt.duration_format_hm.format(h=h, m=m)
    return fmt.duration_format_m.format(m=m)


def resolve_premium_emoji(text: str, fmt: PostFormatConfig) -> str:
    """Expand ``:name:`` tokens to premium (custom) emoji spans.

    ``fmt.premium_emoji`` maps ``name`` → ``custom_emoji_id`` → a
    ``<tg-emoji emoji-id="…">…</tg-emoji>`` span. When a name isn't mapped (or
    the map is empty), the token is left untouched — so templates authored with
    ``:sparkle:`` degrade to plain text on a non-premium account rather than
    breaking. The fallback glyph shown inside the span is the token name itself;
    operators pair a premium id with a unicode fallback by writing the unicode
    directly and mapping it, e.g. ``premium_emoji={"🎬": "5375…"}``.
    """
    if not fmt.premium_emoji:
        return text

    def _sub(match: re.Match) -> str:
        token = match.group(1)
        emoji_id = fmt.premium_emoji.get(token)
        if not emoji_id:
            return match.group(0)
        return f'<tg-emoji emoji-id="{emoji_id}">{token}</tg-emoji>'

    # Also allow mapping a raw unicode glyph directly (no colon syntax needed).
    for glyph, emoji_id in fmt.premium_emoji.items():
        if glyph.startswith(":") or not emoji_id:
            continue
        text = text.replace(glyph, f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>')

    return re.sub(r":([a-zA-Z0-9_]+):", _sub, text)
