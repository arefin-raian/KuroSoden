"""Index channel service — dynamic, shift-capable index maintenance.

Maintains stylized, per-letter index posts in a dedicated channel using the
new short-bar format with HTML bold. Supports **dynamic shifting**: when a
letter section overflows Telegram's 1024-char caption limit, the next section
is rebranded (e.g. B → A(2)), all subsequent sections shift down, the last
reserved post is consumed, and the poster button grid is rebuilt.

State is persisted in the ``index_sections`` PostgreSQL table so it survives
restarts. On first run the table must be seeded with the existing channel
message IDs (see ``seed_index_sections``).
"""

from __future__ import annotations

import asyncio
import html
from pathlib import Path
from typing import cast

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from sqlalchemy import distinct, select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import (
    DistributionBot, IndexSection, StoragePack,
)
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)

# Telegram photo caption character limit (leaving 24-char safety margin).
_CAPTION_LIMIT = 1000

# Number of reserved posts to add when they run out.
_RESERVED_BATCH = 10

# Minimum reserved slots before auto-adding more.
_RESERVED_MIN = 3

# Button labels (exact Unicode small caps from old channel).
_MAIN_BTN = "ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ"
_TOP_BTN = "ɢᴏ ᴛᴏ ᴛᴏᴘ"

# Image directory for letter graphics (absolute, CWD-independent).
_IMG_DIR = Path(__file__).resolve().parents[3] / "index_data" / "index-images"

# Poster caption (Unicode bold heading, HTML bold subtitle).
_POSTER_CAP = (
    "📍[ 𝗜𝗻𝗱𝗲𝘅 𝗼𝗳 𝗔𝗻𝗶𝗺𝗲 𝗪𝗲𝗲𝗯𝘀 ] ----\n\n"
    "<b>Use the buttons below to choose a letter and quickly find"
    " your favorite shows 🎬🔥</b>"
)

# Reserved post caption template.
_RESERVED_CAP = "█▓▒░<b> RESERVED FOR FUTURE </b>░▒▓█"
_RESERVED_IMG = "https://files.catbox.moe/cp9nkw.png"

# Substrings that mark a post as a genuine (untouched) reserved slot. The
# auto-indexer only ever consumes a slot whose LIVE caption still carries one of
# these — if an admin edited a reserved post into a real one (dropping the
# "RESERVED FOR FUTURE" banner / the "Slot N/N" line), it's no longer a slot and
# must never be overwritten. Matched case-insensitively against the plain text.
_RESERVED_MARKERS = ("RESERVED FOR FUTURE", "SLOT ")


def _is_reserved_caption(caption: str | None) -> bool:
    """True when ``caption`` still carries a reserved-slot marker.

    A reserved slot's caption reads ``RESERVED FOR FUTURE`` + ``Slot N/N``. An
    empty/None caption is NOT treated as a marker match here — the verify pass
    handles unreadable/empty messages separately (it never flags them), so this
    stays a pure "does the text still mark it reserved?" check."""
    if not caption:
        return False
    up = caption.upper()
    return any(m in up for m in _RESERVED_MARKERS)

# User-provided divider sticker.
_DIVIDER = (
    "CAACAgUAAxkBAAJAhmpLZLtVdyR7k9JYI3_iqUJVR_zT"
    "AAJOFwACoa8gVlT9gR8Fr550PAQ"
)

# Main channel link for letter buttons.
_MAIN_LINK = "https://t.me/AniXWeebs"

# ── Seed ────────────────────────────────────────────────────────────────────

_INITIAL_LETTERS = [
    "#", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L",
    "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
]

# Message IDs from the rebuilt channel (must match the actual channel).
# fmt: off
_INITIAL_MSG_IDS = [
    174,  # #
    176, 178, 180, 182, 184, 186, 188, 190, 192, 194,  # A-J
    196, 198, 200, 202, 204, 206, 208, 210, 212, 214,  # K-T
    216, 218, 220, 222, 224, 226,                        # U-Z
]
# Reserved message IDs (label=None initially).
_INITIAL_RESERVED = [
    228, 230, 232, 234, 236, 238, 240, 242, 244, 246,
]
# fmt: on

_POSTER_MSG_ID = 171


async def seed_index_sections(session_maker) -> None:
    """Populate index_sections from the hardcoded channel layout.

    Idempotent — only runs when the table is empty.
    """
    async with session_scope(session_maker) as session:
        existing = (await session.execute(select(IndexSection))).scalars().first()
        if existing:
            return

        order = 1
        for i, letter in enumerate(_INITIAL_LETTERS):
            session.add(IndexSection(
                sort_order=order, label=letter, base_letter=letter,
                message_id=_INITIAL_MSG_IDS[i],
            ))
            order += 1

        for mid in _INITIAL_RESERVED:
            session.add(IndexSection(
                sort_order=order, label=None, base_letter=None,
                message_id=mid,
            ))
            order += 1

        log.info("index.sections.seeded", letters=len(_INITIAL_LETTERS),
                 reserved=len(_INITIAL_RESERVED))


# ── Caption rendering ────────────────────────────────────────────────────────

def _strip_bullet(title: str) -> str:
    """Drop any leading ``⦿`` (and surrounding space) a title already carries.

    Empty index cards are seeded with a bare ``⦿`` bullet, and some legacy
    titles arrive with the bullet baked in. Without this, prepending the
    template bullet yields a doubled ``⦿ ⦿ Name``. Strip first, then format.
    """
    return title.lstrip().removeprefix("⦿").strip()


def _entry_html(title: str, links: dict[str, str] | None) -> str:
    """Render one index line, hyperlinking the title to its distribution link.

    When ``links`` has an entry for the title, the name becomes an ``<a href>``
    to the channel's (private, bot-minted) invite link — so the index doubles as
    a clickable directory. Titles with no backing entity render as plain bold text.
    """
    clean = _strip_bullet(title)
    url = (links or {}).get(title)
    if url:
        return f"<b>⦿ <a href=\"{html.escape(url, quote=True)}\">{html.escape(clean)}</a></b>"
    return f"<b>⦿ {clean}</b>"


def _letter_caption(label: str, titles: list[str],
                    links: dict[str, str] | None = None) -> str:
    """Build a bold-HTML caption for a letter section.

    Format::

        <b>•────────•°• A •°•────────•</b>

        <b>⦿ Title 1</b>
        <b>⦿ Title 2</b>

        <b>•─────────────────────•</b>

    When ``links`` is provided, each title is hyperlinked to its distribution link.
    """
    header = f"<b>•────────•°• {label} •°•────────•</b>"
    body = ("\n".join(_entry_html(t, links) for t in titles)
            if titles else "<b>⦿</b>")
    footer = "<b>•─────────────────────•</b>"
    return f"{header}\n\n{body}\n\n{footer}"


def _chunk_titles(titles: list[str], label: str) -> list[list[str]]:
    """Split titles into chunks that fit within Telegram's caption limit."""
    chunks: list[list[str]] = []
    current: list[str] = []
    # Use a worst-case label like "A(99)" to size the header accurately.
    header_len = len(_letter_caption(label + "(99)", []))
    current_len = header_len

    for title in titles:
        entry = f"<b>⦿ {_strip_bullet(title)}</b>"
        entry_len = len(entry) + 1  # +1 for newline
        if current_len + entry_len > _CAPTION_LIMIT:
            chunks.append(current)
            current = [title]
            current_len = header_len + entry_len
        else:
            current.append(title)
            current_len += entry_len

    if current:
        chunks.append(current)
    return chunks


# ── Service ──────────────────────────────────────────────────────────────────

class IndexChannelService:
    # ── Per-letter lock to prevent concurrent shifts from corrupting
    # the label-to-message mapping (two publishes racing on the same letter).
    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, container: Container, client=None) -> None:
        self._c = container
        self._client_override = client
        self.cfg = container.config.index_channel

    # ── Client selection: Gojo (Publisher) is the PRIMARY actor for the index
    # channel — it edits letter cards, reserved posts, inline buttons, and the
    # poster grid. But the legacy index posts were authored by the NekoFetch
    # admin bot, and Telegram only lets a bot edit its OWN messages, so any Gojo
    # edit that lands on one of those fails with MESSAGE_AUTHOR_REQUIRED. When it
    # does, we transparently fall back to the admin client (NekoFetch), which
    # authored them and thus can edit them — NekoFetch acts as Gojo's substitute
    # for anything Gojo lacks permission to touch. A userbot is deliberately NOT
    # used: user accounts cannot attach inline keyboards, which the index needs.
    _FALLBACK_ERRORS = (
        "MESSAGE_AUTHOR_REQUIRED",
        "CHAT_ADMIN_REQUIRED",
        "CHAT_WRITE_FORBIDDEN",
        "MESSAGE_ID_INVALID",
        "FORBIDDEN",
    )

    def _primary_client(self):
        """Use an explicitly supplied actor, otherwise Gojo/admin fallback."""
        if self._client_override is not None:
            return self._client_override
        pm = getattr(self._c, "pipeline_manager", None)
        gojo = getattr(pm, "gojo", None) if pm else None
        return gojo or getattr(self._c, "admin_client", None)

    def _admin_client(self):
        return getattr(self._c, "admin_client", None)

    async def _client_call(self, method: str, *args, **kwargs):
        """Invoke a Pyrogram method on Gojo, falling back to NekoFetch when Gojo
        lacks permission to act on the target message (e.g. a legacy admin-authored
        post → MESSAGE_AUTHOR_REQUIRED).

        MESSAGE_NOT_MODIFIED is re-raised unchanged so callers keep their idempotent
        handling; only permission/authorship failures trigger the single fallback.
        """
        primary = self._primary_client()
        admin = self._admin_client()
        try:
            return await getattr(primary, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).upper()
            if "MESSAGE_NOT_MODIFIED" in text:
                raise
            if admin is None or admin is primary:
                raise
            if not any(code in text for code in self._FALLBACK_ERRORS):
                raise
            log.info("index.client.fallback", method=method, reason=str(exc)[:80])
            return await getattr(admin, method)(*args, **kwargs)

    def _active(self) -> bool:
        return bool(
            self.cfg.enabled and self.cfg.channel_id != 0
            and self._primary_client() is not None
        )

    # ── config-driven channel identity (was hardcoded to AniXWeebs_Index) ───────
    # These let the index survive a move to a fresh channel: on restore we rewrite
    # ``channel_username`` / ``poster_message_id`` in config and every t.me link,
    # poster button, and "go to top" target follows automatically.

    def _username(self) -> str:
        """Public username used to build t.me/<user>/<mid> deep links."""
        return (getattr(self.cfg, "username", "") or "AniXWeebs_Index").lstrip("@")

    def _poster_id(self) -> int:
        return int(getattr(self.cfg, "poster_message_id", 0) or _POSTER_MSG_ID)

    def _main_link(self) -> str:
        return getattr(self.cfg, "main_channel_link", "") or _MAIN_LINK

    @staticmethod
    def letter_of(title: str) -> str:
        for ch in title:
            if ch.isalpha():
                return ch.upper()
            if ch.isdigit():
                return "#"
        return "#"

    async def _titles_for_letter(self, base_letter: str) -> list[str]:
        # DB-level filter — avoids loading all titles into Python memory.
        # For '#' (digit-starting titles), use PostgreSQL regex ^[0-9];
        # for A-Z, use ILIKE for case-insensitive letter matching.
        if base_letter == "#":
            cond = StoragePack.anime_title.op("~")(r"^[0-9]")
        else:
            cond = StoragePack.anime_title.ilike(f"{base_letter}%")
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(distinct(StoragePack.anime_title)).where(cond)
                )
            ).scalars().all()
        return sorted(rows)

    async def _links_for_titles(self, titles: list[str]) -> dict[str, str]:
        """Map each title to the distribution link its index entry should point at.

        Prefers the channel's private bot-minted invite link (the same link the
        main-channel Download button uses), falling back to the public
        ``t.me/<username>`` link and, for bots, the ``?start`` deep link. Titles
        with no enabled distribution entity are omitted (rendered as plain text).
        """
        if not titles:
            return {}
        links: dict[str, str] = {}
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.enabled.is_(True),
                        DistributionBot.anime_doc_id.is_not(None),
                    )
                )
            ).scalars().all()
        # anime_doc_id → the DistributionBot row (last enabled wins, matching the
        # main-channel service's ``.first()`` on the same enabled filter).
        by_doc = {r.anime_doc_id: r for r in rows}
        # StoragePack.anime_title is what appears in the index; map it back to a
        # doc id to find the backing entity. Build title→doc once.
        async with session_scope(self._c.pg_sessionmaker) as session:
            pack_rows = (
                await session.execute(
                    select(StoragePack.anime_title, StoragePack.anime_doc_id)
                    .where(StoragePack.anime_title.in_(titles))
                    .distinct()
                )
            ).all()
        for title, doc_id in pack_rows:
            row = by_doc.get(doc_id)
            if row is None or not (row.username or row.invite_link):
                continue
            if row.is_channel and row.invite_link:
                links[title] = row.invite_link
            elif row.is_channel and row.username:
                links[title] = f"https://t.me/{row.username}"
            elif row.username:
                links[title] = f"https://t.me/{row.username}?start=anime_{doc_id}"
        return links

    async def _all_active_sections(self) -> list[IndexSection]:
        """Return all sections with a label, ordered by sort_order."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            result = await session.execute(
                select(IndexSection)
                .where(IndexSection.label.isnot(None))
                .order_by(IndexSection.sort_order)
            )
            return list(result.scalars().all())

    async def _count_reserved(self) -> int:
        async with session_scope(self._c.pg_sessionmaker) as session:
            return await self._count_reserved_in_session(session)

    async def _count_reserved_in_session(self, session) -> int:
        # A slot counts as reserved only when it has no label AND hasn't been
        # repurposed by an admin into a normal post (see _verify_reserved_slots).
        result = await session.execute(
            select(IndexSection).where(
                IndexSection.label.is_(None),
                IndexSection.repurposed.is_(False),
            )
        )
        return len(list(result.scalars().all()))

    async def _verify_reserved_slots(self) -> int:
        """Re-check every tracked reserved slot against its live caption.

        The admin can edit a reserved post into a real one (a promo, a notice,
        an out-of-band letter). Once its caption loses the RESERVED marker
        (``RESERVED FOR FUTURE`` / ``Slot N/N``) it is no longer ours to consume
        or overwrite, so we flag it ``repurposed`` — auto-indexing then skips it
        forever and works with the remaining genuine slots. Best-effort: an
        unreadable message is left as-is (flagged only on a definite marker loss).

        Returns the number of slots newly flagged. Cheap enough to run at the
        top of a shift (only reserved, unflagged rows are fetched)."""
        client = self._primary_client()
        if client is None:
            return 0
        async with session_scope(self._c.pg_sessionmaker) as session:
            reserved = (
                await session.execute(
                    select(IndexSection).where(
                        IndexSection.label.is_(None),
                        IndexSection.repurposed.is_(False),
                    ).order_by(IndexSection.sort_order)
                )
            ).scalars().all()
            flagged = 0
            for slot in reserved:
                if not slot.message_id:
                    continue
                try:
                    msg = await client.get_messages(self.cfg.channel_id, slot.message_id)
                except Exception as exc:  # noqa: BLE001 — unreadable → leave as-is
                    log.debug("index.reserved.verify_unreadable",
                              mid=slot.message_id, error=str(exc))
                    continue
                # A deleted/empty message can't be a repurposed post; skip it so a
                # transient fetch miss doesn't strand the slot.
                if not msg or getattr(msg, "empty", False):
                    continue
                caption = getattr(msg, "caption", None) or getattr(msg, "text", None) or ""
                if not _is_reserved_caption(caption):
                    slot.repurposed = True
                    flagged += 1
                    log.info("index.reserved.repurposed",
                             mid=slot.message_id, order=slot.sort_order)
        return flagged

    async def _get_poster_id(self) -> int:
        """Return the poster message ID (config-driven; falls back to seed default)."""
        return self._poster_id()

    # ── Refresh ─────────────────────────────────────────────────────────

    async def refresh_letter(self, base_letter: str, _retry: int = 0) -> int | None:
        """Rebuild all chunks for ``base_letter``; shift if needed.

        Returns the first chunk's message ID (for ``entry_link``).
        The ``_retry`` parameter prevents infinite recursion when
        reserved-post creation keeps failing.
        """
        if not self._active():
            return None

        # Acquire per-letter lock so two concurrent publishes for the same
        # letter can't double-shift and corrupt the section mapping.
        lock = IndexChannelService._locks.setdefault(base_letter, asyncio.Lock())
        async with lock:
            return await self._refresh_letter_locked(base_letter, _retry)

    async def _refresh_letter_locked(self, base_letter: str, _retry: int = 0) -> int | None:
        """Core refresh logic — called from ``refresh_letter`` under the per-letter lock."""
        titles = await self._titles_for_letter(base_letter)
        if not titles:
            return None

        # Before any shift, confirm the trailing reserved slots are still reserved:
        # an admin may have repurposed one into a normal post (dropping its
        # "RESERVED FOR FUTURE" / "Slot N/N" marker). Such a slot is flagged
        # ``repurposed`` and excluded from the reserved pool so auto-indexing
        # never overwrites a real post — it just uses the remaining real slots.
        await self._verify_reserved_slots()

        chunks = _chunk_titles(titles, base_letter)
        links = await self._links_for_titles(titles)
        first_mid: int | None = None

        async with session_scope(self._c.pg_sessionmaker) as session:
            # Get existing sections for this base letter
            existing = (
                await session.execute(
                    select(IndexSection)
                    .where(IndexSection.base_letter == base_letter)
                    .order_by(IndexSection.sort_order)
                )
            ).scalars().all()
            existing = list(existing)

            # If more chunks than slots, shift down starting from the
            # section *after* the last existing chunk for this letter.
            if len(chunks) > len(existing):
                if not existing:
                    log.error("index.refresh.no_slot", base_letter=base_letter,
                              hint="letter has no pre-allocated section")
                    return None

                needed = len(chunks) - len(existing)
                reserved_count = await self._count_reserved_in_session(session)
                if needed > reserved_count:
                    if _retry >= 3:
                        log.error("index.refresh.max_retries", base_letter=base_letter)
                        return None
                    log.warning("index.refresh.out_of_reserved",
                                needed=needed, available=reserved_count,
                                base_letter=base_letter)
                    await session.commit()
                    await self._add_reserved_batch()
                    # Retry with fresh session so the new reserved posts
                    # are visible to the next refresh_letter call.
                    return await self._refresh_letter_locked(base_letter, _retry + 1)

                # from_order targets the next section (e.g. B becomes A(2)).
                from_order = existing[-1].sort_order + 1
                for _ in range(needed):
                    await self._shift_down_in_session(session, from_order)
                    from_order += 1  # next shift targets the slot that was just vacated
                # Re-fetch after shift
                existing = (
                    await session.execute(
                        select(IndexSection)
                        .where(IndexSection.base_letter == base_letter)
                        .order_by(IndexSection.sort_order)
                    )
                ).scalars().all()
                existing = list(existing)

            # Edit or clear each section
            for idx in range(len(existing)):
                if idx < len(chunks):
                    chunk = chunks[idx]
                    section = existing[idx]
                    label = base_letter if idx == 0 else f"{base_letter}({idx + 1})"
                    caption = _letter_caption(label, chunk, links)

                    # Check if image needs changing
                    old_label = section.label or ""
                    needs_image = not old_label.startswith(base_letter) or section.base_letter != base_letter

                    section.label = label
                    section.base_letter = base_letter

                    if idx == 0:
                        first_mid = cast(int, section.message_id)

                    try:
                        if needs_image:
                            img_path = _IMG_DIR / f"{base_letter}.jpg"
                            if img_path.exists():
                                await self._client_call(
                                    "edit_message_media",
                                    self.cfg.channel_id, cast(int, section.message_id),
                                    media=InputMediaPhoto(
                                        media=str(img_path), caption=caption,
                                        parse_mode=ParseMode.HTML,
                                    ),
                                    reply_markup=self._letter_buttons(),
                                )
                                continue
                        await self._client_call(
                            "edit_message_caption",
                            self.cfg.channel_id, cast(int, section.message_id),
                            caption=caption, parse_mode=ParseMode.HTML,
                            reply_markup=self._letter_buttons(),
                        )
                    except Exception as exc:
                        if "MESSAGE_NOT_MODIFIED" not in str(exc):
                            log.warning("index.refresh.failed", label=label, error=str(exc))
                else:
                    # Shrinking chunks — clear the caption but keep the
                    # label/base_letter so the section remains in the
                    # poster grid (just with no titles showing).
                    section = existing[idx]
                    empty_cap = _letter_caption(section.label or "?", [])
                    try:
                        await self._client_call(
                            "edit_message_caption",
                            self.cfg.channel_id, cast(int, section.message_id),
                            caption=empty_cap, parse_mode=ParseMode.HTML,
                            reply_markup=self._letter_buttons(),
                        )
                    except Exception as exc:
                        if "MESSAGE_NOT_MODIFIED" not in str(exc):
                            log.warning("index.refresh.clear_failed",
                                        section_id=section.message_id, error=str(exc))

            await session.commit()

        # ── After commit: check reserved count & rebuild poster ─────────
        # Must happen here (outside the session) so we don't nest commits.
        if await self._count_reserved() < _RESERVED_MIN:
            if not await self._add_reserved_batch():
                log.warning("index.refresh.reserved_add_failed", base_letter=base_letter)

        await self._rebuild_poster()
        return first_mid

    async def _shift_down_in_session(self, session, from_order: int) -> None:
        """Shift sections starting at ``from_order`` down by one.

        MUST be called inside an active session_scope.
        """
        # Exclude repurposed slots: an admin turned them into normal posts, so
        # they're no longer part of our managed sequence — never shifted into and
        # never consumed as reserved (that would overwrite a real post). Skipping
        # them here means the shift flows over them to the next genuine slot.
        result = await session.execute(
            select(IndexSection)
            .where(
                IndexSection.sort_order >= from_order,
                IndexSection.repurposed.is_(False),
            )
            .order_by(IndexSection.sort_order)
        )
        sections = list(result.scalars().all())
        if not sections:
            return

        first = sections[0]

        # ── Save old labels BEFORE we mutate anything ──────────────────
        old_labels: list[tuple[str | None, str | None]] = [
            (s.label, s.base_letter) for s in sections
        ]

        # Find the previous section's base letter
        prev_result = await session.execute(
            select(IndexSection)
            .where(IndexSection.sort_order == from_order - 1)
        )
        prev = prev_result.scalars().first()
        prev_base = prev.base_letter if prev and prev.base_letter else "?"

        # Count existing chunks for the overflow letter
        count_result = await session.execute(
            select(IndexSection).where(IndexSection.base_letter == prev_base)
        )
        existing_count = len(list(count_result.scalars().all()))

        # Store values we need after the shift for image change
        new_first_label = f"{prev_base}({existing_count + 1})"
        first_msg_id = first.message_id

        # Rebrand the first section
        first.label = new_first_label
        first.base_letter = prev_base

        # Shift remaining sections down using the SAVED old labels
        for i in range(1, len(sections)):
            current = sections[i]
            prev_old_label, prev_old_base = old_labels[i - 1]
            if current.label is None:
                # Consume reserved — inherit the label that was in the
                # previous slot *before* we started shifting.
                current.label = prev_old_label
                current.base_letter = prev_old_base
                break  # Only one reserved gets consumed per shift
            current.label = prev_old_label
            current.base_letter = prev_old_base

        # Change image for rebranded section
        if first_msg_id:
            img_path = _IMG_DIR / f"{prev_base}.jpg"
            if img_path.exists():
                try:
                    temp_cap = _letter_caption(new_first_label, [])
                    await self._client_call(
                        "edit_message_media",
                        self.cfg.channel_id, first_msg_id,
                        media=InputMediaPhoto(
                            media=str(img_path), caption=temp_cap,
                            parse_mode=ParseMode.HTML,
                        ),
                        reply_markup=self._letter_buttons(),
                    )
                except Exception as exc:
                    log.warning("index.shift.image_failed", label=new_first_label, error=str(exc))

        log.info("index.shifted", from_order=from_order, new_label=new_first_label)

    async def _add_reserved_batch(self) -> bool:
        """Post _RESERVED_BATCH new reserved posts at the end of the channel.

        Returns True if all posts were sent successfully, False otherwise.
        """
        if not self._active():
            return False

        async with session_scope(self._c.pg_sessionmaker) as session:
            # Find max sort_order
            result = await session.execute(
                select(IndexSection.sort_order).order_by(IndexSection.sort_order.desc()).limit(1)
            )
            max_order = result.scalars().first() or 0

            all_ok = True
            for i in range(_RESERVED_BATCH):
                # Divider sticker
                try:
                    await self._client_call("send_sticker", self.cfg.channel_id, _DIVIDER)
                except Exception as exc:
                    log.warning("index.reserved.divider_failed", error=str(exc))

                # Reserved post with image
                try:
                    sent = await self._client_call(
                        "send_photo",
                        self.cfg.channel_id, _RESERVED_IMG,
                        caption=(f"{_RESERVED_CAP}\n\n"
                                 f"<i>Slot {i + 1}/{_RESERVED_BATCH}</i>"),
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._letter_buttons(),
                    )
                    max_order += 1
                    session.add(IndexSection(
                        sort_order=max_order, label=None, base_letter=None,
                        message_id=sent.id,
                    ))
                    log.info("index.reserved.added", msg_id=sent.id, order=max_order)
                except Exception as exc:
                    log.warning("index.reserved.add_failed", error=str(exc))
                    all_ok = False
                    await asyncio.sleep(2)  # brief pause before next attempt

            await session.commit()
            return all_ok

    # ── Poster ──────────────────────────────────────────────────────────

    async def _rebuild_poster(self) -> None:
        """Rebuild the poster's 3-column letter button grid."""
        if not self._active():
            return

        sections = await self._all_active_sections()
        if not sections:
            return

        poster_id = await self._get_poster_id()
        username = self._username()

        rows = []
        row = []
        for sec in sections:
            if sec.label and sec.message_id:
                row.append(InlineKeyboardButton(
                    sec.label, url=f"https://t.me/{username}/{sec.message_id}"
                ))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        try:
            await self._client_call(
                "edit_message_caption",
                self.cfg.channel_id, poster_id,
                caption=_POSTER_CAP, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            log.info("index.poster.rebuilt", buttons=sum(len(r) for r in rows))
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" not in str(exc):
                log.warning("index.poster.rebuild_failed", error=str(exc))

    # ── Buttons / links ──────────────────────────────────────────────────

    def _letter_buttons(self) -> InlineKeyboardMarkup:
        poster_id = self._poster_id()
        username = self._username()
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                _MAIN_BTN, url=self._main_link(),
            ),
            InlineKeyboardButton(
                _TOP_BTN, url=f"https://t.me/{username}/{poster_id}",
            ),
        ]])

    async def channel_invite(self) -> str | None:
        """Return a private invite link to the INDEX CHANNEL itself.

        The main-channel post's Index button points here (per spec: "the index
        button, which is the index channel's link"). Minted once by the Gojo bot
        (bots can create invite links where they're admin) and cached in Redis so
        we don't mint a fresh link on every publish. Best-effort → None on failure,
        and the caller falls back to a deep-link to the letter section."""
        if not self._active() or not self.cfg.channel_id:
            return None
        key = f"nf:index:invite:{self.cfg.channel_id}"

        # Cached link (best-effort — a Redis miss/blip just re-mints).
        try:
            from nekofetch.core.redis_safe import safe_redis_get

            cached = await safe_redis_get(self._c.redis, key,
                                          label="index.invite.get")
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
        except Exception:  # noqa: BLE001
            pass

        from nekofetch.services.invite_link_service import InviteLinkService

        link = await InviteLinkService(self._c).mint_for_chat(self.cfg.channel_id)
        if link:
            try:
                from nekofetch.core.redis_safe import safe_redis_set

                await safe_redis_set(self._c.redis, key, link,
                                     label="index.invite.set")
            except Exception:  # noqa: BLE001
                pass
        return link

    async def entry_link(self, title: str) -> str | None:
        """Return a t.me link to the index post containing ``title``."""
        if not self._active():
            return None

        base = self.letter_of(title)
        titles = await self._titles_for_letter(base)
        if not titles:
            return None

        chunks = _chunk_titles(titles, base)
        async with session_scope(self._c.pg_sessionmaker) as session:
            sections = (
                await session.execute(
                    select(IndexSection)
                    .where(IndexSection.base_letter == base)
                    .order_by(IndexSection.sort_order)
                )
            ).scalars().all()
            sections = list(sections)

        username = self._username()
        for idx, chunk in enumerate(chunks):
            if title in chunk and idx < len(sections) and sections[idx].message_id:
                return f"https://t.me/{username}/{sections[idx].message_id}"

        # Fallback: first section
        if sections and sections[0].message_id:
            return f"https://t.me/{username}/{sections[0].message_id}"

        return None

    # ── Manual slot management (Gojo index-management UI) ─────────────────────

    async def list_slots(self) -> list[dict]:
        """Return every tracked index slot for the management UI, in order.

        Each dict is ``{order, message_id, label, base_letter, repurposed,
        kind}`` where ``kind`` is ``"letter"`` (a live A–Z section), ``"reserved"``
        (an empty slot we may consume), or ``"repurposed"`` (an admin turned it
        into a normal post — auto-indexing leaves it alone). The poster is tracked
        in config, not here, so it isn't listed."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(IndexSection).order_by(IndexSection.sort_order)
                )
            ).scalars().all()
        out: list[dict] = []
        for r in rows:
            if r.repurposed:
                kind = "repurposed"
            elif r.label is None:
                kind = "reserved"
            else:
                kind = "letter"
            out.append({
                "order": r.sort_order, "message_id": r.message_id,
                "label": r.label, "base_letter": r.base_letter,
                "repurposed": bool(r.repurposed), "kind": kind,
            })
        return out

    async def _slot_by_order(self, order: int) -> IndexSection | None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            return (
                await session.execute(
                    select(IndexSection).where(IndexSection.sort_order == order)
                )
            ).scalar_one_or_none()

    async def edit_slot_caption(self, order: int, caption_html: str) -> bool:
        """Overwrite one slot's caption (raw HTML) by ``sort_order``.

        Manual override for the management UI — edits the live message in place.
        Does not touch the label/base_letter mapping, so a *letter* slot keeps its
        auto-index identity; editing a letter slot's text by hand is at the
        operator's own risk (a later auto-refresh may rewrite it). Returns True on
        a confirmed edit."""
        if not self._active():
            return False
        slot = await self._slot_by_order(order)
        if slot is None or not slot.message_id:
            return False
        try:
            await self._client_call(
                "edit_message_caption",
                self.cfg.channel_id, cast(int, slot.message_id),
                caption=caption_html, parse_mode=ParseMode.HTML,
                reply_markup=self._letter_buttons(),
            )
        except Exception as exc:  # noqa: BLE001
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return True
            log.warning("index.slot.edit_caption_failed", order=order, error=str(exc))
            return False
        log.info("index.slot.caption_edited", order=order)
        return True

    async def replace_slot_image(self, order: int, image: str,
                                 caption_html: str | None = None) -> bool:
        """Replace one slot's photo (URL or file_id) by ``sort_order``.

        Keeps the existing caption unless ``caption_html`` is given. Edits the
        live message media in place. Returns True on a confirmed edit."""
        if not self._active():
            return False
        slot = await self._slot_by_order(order)
        if slot is None or not slot.message_id:
            return False
        media = InputMediaPhoto(media=image, caption=caption_html or "",
                                parse_mode=ParseMode.HTML) if caption_html \
            else InputMediaPhoto(media=image)
        try:
            await self._client_call(
                "edit_message_media",
                self.cfg.channel_id, cast(int, slot.message_id), media=media,
                reply_markup=self._letter_buttons(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("index.slot.replace_image_failed", order=order, error=str(exc))
            return False
        log.info("index.slot.image_replaced", order=order)
        return True

    async def set_slot_buttons(self, order: int,
                               buttons: list[tuple[str, str]]) -> bool:
        """Set one slot's inline buttons to ``[(text, url), …]`` (one per row).

        Passing an empty list clears the buttons. Edits the live message's reply
        markup in place. Returns True on a confirmed edit."""
        if not self._active():
            return False
        slot = await self._slot_by_order(order)
        if slot is None or not slot.message_id:
            return False
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text, url=url)] for text, url in buttons]
        ) if buttons else None
        try:
            await self._client_call(
                "edit_message_reply_markup",
                self.cfg.channel_id, cast(int, slot.message_id),
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return True
            log.warning("index.slot.set_buttons_failed", order=order, error=str(exc))
            return False
        log.info("index.slot.buttons_set", order=order, count=len(buttons))
        return True

    async def mark_slot_repurposed(self, order: int, repurposed: bool = True) -> bool:
        """Manually flag/unflag a slot as repurposed (management-UI override).

        A repurposed slot is excluded from the reserved pool and never shifted or
        overwritten by auto-indexing. This is the explicit counterpart to the
        automatic caption-marker detection in :meth:`_verify_reserved_slots`."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            slot = (
                await session.execute(
                    select(IndexSection).where(IndexSection.sort_order == order)
                )
            ).scalar_one_or_none()
            if slot is None:
                return False
            slot.repurposed = repurposed
        log.info("index.slot.repurposed_set", order=order, repurposed=repurposed)
        return True
