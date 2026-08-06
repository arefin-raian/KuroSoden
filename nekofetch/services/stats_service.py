"""Stats service — dynamic dataset statistics.

Actually visits the index channel to extract real published titles,
cross-references against the 148 canonical names, and maintains a
pinned stats message with structured breakdown.

Auto-refreshes on publish and at startup.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from nekofetch.core.constants import RULE_HEAVY
from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import safe_redis_get, safe_redis_mget
from nekofetch.infrastructure.database.postgres.models import StoragePack
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.localization.messages import M, t

log = get_logger(__name__)

_STATS_MSG_KEY = "nf:storage:stats_msg_id"
_CANONICAL_PATH = Path("resources") / "canonical_names.json"

_CANONICAL_CACHE: dict[str, dict] | None = None
_CANONICAL_MTIME: float = 0.0


def _load_canonical_map() -> dict[str, dict]:
    """Load the canonical name map with mtime-based caching."""
    global _CANONICAL_CACHE, _CANONICAL_MTIME
    try:
        if not _CANONICAL_PATH.exists():
            return {}
        mtime = _CANONICAL_PATH.stat().st_mtime
        if _CANONICAL_CACHE is not None and mtime <= _CANONICAL_MTIME:
            return _CANONICAL_CACHE
        _CANONICAL_CACHE = json.loads(_CANONICAL_PATH.read_text(encoding="utf-8"))
        _CANONICAL_MTIME = mtime
        return _CANONICAL_CACHE
    except Exception as exc:
        log.warning("stats.canonical_map.load_failed", error=str(exc))
        return {}


class StatsService:
    def __init__(self, container: Container) -> None:
        self._c = container

    # ── channel scraping ──────────────────────────────────────────────────────

    async def _fetch_index_channel_titles(self) -> set[str]:
        """Visit the index channel and extract ALL published titles from letter posts.

        Each letter has a single message that looks like::

            ------- A -------
            ⦿ Attack on Titan
            ⦿ Another

        Message IDs are tracked in Redis (``nf:index:letter:{letter}``) by
        :class:`IndexChannelService`). All letter messages are fetched in a
        single ``get_messages()`` batch call (1 API call, not 27).

        Returns empty set if unreachable or no letter messages exist yet.
        """
        cfg = self._c.config.index_channel
        client = getattr(self._c, "admin_client", None)
        if not (cfg.enabled and cfg.channel_id != 0 and client is not None):
            return set()

        letters = [chr(i) for i in range(ord("A"), ord("Z") + 1)] + ["#"]

        # Collect all existing letter message IDs in ONE Postgres SELECT.
        # The index-channel layout is the single source of truth — letter
        # posts live in the ``index_sections`` table with ``message_id`` and
        # ``base_letter`` columns, seeded by ``IndexChannelService.seed_index_sections``
        # on first startup. The previous ``nf:index:letter:*`` Redis keys were
        # NEVER WRITTEN by any code path (only read by this method), so every
        # stats refresh tripped a 27-key timeout storm on Upstash blips;
        # routing through Postgres eliminates the Redis round-trip entirely
        # while keeping ``get_messages`` (the one API call that actually
        # fetches titles) unchanged. The query is bounded by 27 rows (one
        # per active letter) and runs ``<5ms`` on the local Postgres — orders
        # of magnitude faster than the per-key timeout path it replaced.
        from nekofetch.infrastructure.database.postgres.models import (
            IndexSection,
        )

        letter_ids: list[int] = []
        if self._c.pg_sessionmaker:
            async with session_scope(self._c.pg_sessionmaker) as session:
                rows = (
                    await session.execute(
                        select(IndexSection.message_id).where(
                            IndexSection.label.isnot(None)
                        )
                    )
                ).scalars().all()
            letter_ids = [int(mid) for mid in rows if mid]
        if not letter_ids:
            log.info("stats.index_channel.no_letters_yet")
            return set()

        # Single API call for all letter messages
        titles: set[str] = set()
        try:
            msgs = await client.get_messages(cfg.channel_id, letter_ids)
            for msg in msgs:
                if not msg or not msg.text:
                    continue
                for line in msg.text.split("\n"):
                    line = line.strip()
                    if line.startswith("⦿"):
                        title = line.removeprefix("⦿").strip()
                        if title:
                            titles.add(title)
        except Exception as exc:
            log.warning("stats.fetch_index.failed", error=str(exc))

        log.info("stats.index_channel.titles", count=len(titles))
        return titles

    # ── data queries ──────────────────────────────────────────────────────────

    async def _all_series(self) -> dict[str, str]:
        """Return {anime_doc_id: anime_title} for all series in storage."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(StoragePack.anime_doc_id, StoragePack.anime_title)
                )
            ).all()
        seen: dict[str, str] = {}
        for doc_id, title in rows:
            if doc_id not in seen:
                seen[doc_id] = title
        return seen

    # ── title matching ────────────────────────────────────────────────────────

    @staticmethod
    def _match_canonical_name(title: str, cm: dict[str, dict]) -> str | None:
        """Match a title against the canonical names map.

        Resolution order:
        1. Case-insensitive match on canonical_name values
        2. Case-insensitive match on map keys
        3. Normalized match (alphanumeric only)
        """
        tl = title.lower().strip()

        # 1. Match by canonical_name values
        for info in cm.values():
            cn = info.get("canonical_name", "")
            if cn.lower().strip() == tl:
                return cn

        # 2. Match by original keys (old PACK_TREE keys)
        for our_key, info in cm.items():
            if our_key.lower().strip() == tl:
                return info.get("canonical_name") or our_key

        # 3. Normalized match — strip all non-alphanumeric
        def norm(s: str) -> str:
            return "".join(c for c in s if c.isalnum()).lower()

        nt = norm(title)
        for info in cm.values():
            cn = info.get("canonical_name", "")
            if norm(cn) == nt:
                return cn
        for our_key, info in cm.items():
            if norm(our_key) == nt:
                return info.get("canonical_name") or our_key

        return None

    # ── compute ───────────────────────────────────────────────────────────────

    async def compute(self) -> dict:
        """Compute the full stats snapshot from authoritative DB signals.

        **Total series**: distinct `anime_doc_id` in `StoragePack`.
        **Published**: series with a `ChannelPost.main_message_id` (live in the
        main channel).
        **Indexed**: series whose title-letter has a *posted* ``IndexSection``.
        The index channel lists every ``StoragePack`` title per letter (see
        ``IndexChannelService._titles_for_letter``), so a series is in the index
        as soon as its letter section is live — independent of whether it has a
        main-channel card. This is why published (6) and indexed (7) can differ.
        **Not indexed**: Total - Indexed (NOT total - published; the owner reads
        this as "how many are missing from the index").

        The old `compute()` scraped the index channel and fuzzy-matched titles
        against a canonical-names map — fragile and returned published=0 when
        the scrape failed. This DB-driven version reads the authoritative signals
        that the publish/index flows write, eliminating the scrape entirely.
        """
        from nekofetch.infrastructure.database.postgres.models import (
            ChannelPost,
            IndexSection,
        )
        from nekofetch.services.index_channel_service import IndexChannelService

        async with session_scope(self._c.pg_sessionmaker) as session:
            # Total series = distinct anime_doc_id in StoragePack. We dedup in
            # Python (dict keyed by doc_id) rather than SQL DISTINCT because one
            # anime_doc_id can carry inconsistent titles across packs (e.g.
            # "The Case Study of Vanitas" vs "Vanitas no Carte") — a
            # SELECT DISTINCT on (doc_id, title) would return one row per title
            # variant and over-count the series.
            all_series_rows = (
                await session.execute(
                    select(StoragePack.anime_doc_id, StoragePack.anime_title)
                )
            ).all()
            all_series = {doc_id: title for doc_id, title in all_series_rows}

            # Published = has a main-channel post.
            published_set = set((
                await session.execute(
                    select(ChannelPost.anime_doc_id)
                    .where(ChannelPost.main_message_id.isnot(None))
                )
            ).scalars().all())
            # Indexed = the series' letter has a *posted* index section. The index
            # lists StoragePack titles per letter, so membership is decided by the
            # letter, not a per-series row.
            posted_letters = set((
                await session.execute(
                    select(IndexSection.base_letter).where(
                        IndexSection.message_id.isnot(None),
                        IndexSection.base_letter.isnot(None),
                    )
                )
            ).scalars().all())

        catalog_ids = set(all_series)
        total = len(all_series)
        # Intersect with the catalog so published can never exceed total (a stray
        # ChannelPost without a StoragePack would otherwise skew it).
        published_count = len(published_set & catalog_ids)

        indexed_titles = [
            title for title in all_series.values()
            if IndexChannelService.letter_of(title) in posted_letters
        ]
        indexed_count = len(indexed_titles)
        not_indexed_count = total - indexed_count

        # "Not indexed" titles = series in storage whose letter isn't posted yet.
        not_indexed_titles = [
            title for title in all_series.values()
            if IndexChannelService.letter_of(title) not in posted_letters
        ]

        return {
            "total_series": total,
            "published_series": published_count,
            "indexed_series": indexed_count,
            "not_indexed_series": not_indexed_count,
            "not_indexed_titles": sorted(set(not_indexed_titles)),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }

    # ── Gojo dashboard (compute + operational counts) ──────────────────────────

    async def gojo_dashboard(self) -> dict:
        """Compute the catalog stats **plus** operational counts for Gojo's Stats
        screen: durable backup coverage, scheduled publishes in flight, and the
        timestamps of the last update/ban-check sweeps.

        Best-effort per source — a missing table or a bad row degrades one line,
        never the whole screen. Returns the ``compute()`` dict augmented with a
        ``dashboard`` sub-dict.
        """
        from sqlalchemy import func

        from nekofetch.infrastructure.database.postgres.models import (
            ChannelContentBackup,
            PublishedPostBackup,
            ScheduledPost,
        )

        base = await self.compute()
        d: dict = {
            # index_items mirrors compute()'s catalog-consistent indexed count so
            # the dashboard sub-dict never disagrees with the Overview section.
            "main_backups": 0, "dist_backups": 0,
            "index_items": base.get("indexed_series", 0),
            "pending_scheduled": 0, "next_scheduled_utc": None,
            "last_update_check": None, "last_ban_check": None,
        }
        try:
            async with session_scope(self._c.pg_sessionmaker) as session:
                d["main_backups"] = int(
                    (await session.execute(
                        select(func.count()).select_from(PublishedPostBackup)
                    )).scalar() or 0
                )
                d["dist_backups"] = int(
                    (await session.execute(
                        select(func.count()).select_from(ChannelContentBackup)
                        .where(ChannelContentBackup.scope == "distribution")
                    )).scalar() or 0
                )
                d["pending_scheduled"] = int(
                    (await session.execute(
                        select(func.count()).select_from(ScheduledPost)
                        .where(ScheduledPost.status == "pending")
                    )).scalar() or 0
                )
                nxt = (await session.execute(
                    select(ScheduledPost.scheduled_at)
                    .where(ScheduledPost.status == "pending")
                    .order_by(ScheduledPost.scheduled_at.asc()).limit(1)
                )).scalar()
                if nxt is not None:
                    d["next_scheduled_utc"] = nxt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception as exc:  # noqa: BLE001 — counts are best-effort
            log.warning("stats.dashboard.counts_failed", error=str(exc))

        # Last-sweep timestamps are stamped in Redis by the scheduled jobs.
        for key, field in (("nf:maint:last_update_check", "last_update_check"),
                           ("nf:maint:last_ban_check", "last_ban_check")):
            try:
                val = await safe_redis_get(self._c.redis, key)
                if val:
                    d[field] = val
            except Exception:  # noqa: BLE001
                pass

        base["dashboard"] = d
        return base

    # ── message display ───────────────────────────────────────────────────────

    @staticmethod
    def _format_message(stats: dict) -> str:
        """Build a clean, structured HTML stats message."""
        parts: list[str] = []

        parts.append(t(M.STATS_TITLE))
        parts.append(RULE_HEAVY)
        parts.append("")

        parts.append(t(M.STATS_OVERVIEW))
        parts.append(t(M.STATS_ROW, label=t(M.STATS_TOTAL), value=stats["total_series"]))
        parts.append(t(M.STATS_ROW, label=t(M.STATS_PUBLISHED), value=stats["published_series"]))
        parts.append(t(M.STATS_ROW, label=t(M.STATS_NOT_INDEXED), value=stats["not_indexed_series"]))
        parts.append("")

        titles = stats.get("not_indexed_titles", [])
        parts.append(t(M.STATS_PENDING_TITLE))
        if titles:
            for title in titles:
                safe = html.escape(title, quote=False)
                parts.append(t(M.STATS_ENTRY, title=safe))
        else:
            parts.append(t(M.STATS_NONE_PENDING))
        parts.append("")

        parts.append(RULE_HEAVY)
        parts.append(t(M.STATS_UPDATED, ts=stats["ts"]))

        return "\n".join(parts)

    @staticmethod
    def dashboard_message(stats: dict) -> str:
        """Gojo's Stats screen: catalog overview + the operational counts from
        :meth:`gojo_dashboard` (backup coverage, scheduled publishes, last sweeps).

        Reuses the catalog block from :meth:`_format_message` (minus the long
        pending-titles list, which would bury the operational numbers) and appends
        a durable-recovery section. Plain HTML — no localization keys, since this
        surface is Gojo-specific."""
        d = stats.get("dashboard", {})
        parts: list[str] = [
            "<b>🔮 Gojo — Dashboard</b>",
            RULE_HEAVY,
            "",
            "<b>Catalog</b>",
            f"  ⦿ Total series: <b>{stats.get('total_series', 0)}</b>",
            f"  ⦿ Published: <b>{stats.get('published_series', 0)}</b>",
            f"  ⦿ Not indexed: <b>{stats.get('not_indexed_series', 0)}</b>",
            "",
            "<b>Durable backups</b>",
            f"  ⦿ Main-channel posts: <b>{d.get('main_backups', 0)}</b>",
            f"  ⦿ Distribution channels: <b>{d.get('dist_backups', 0)}</b>",
            f"  ⦿ Indexed items: <b>{stats.get('indexed_series', d.get('index_items', 0))}</b>",
            "",
            "<b>Scheduling</b>",
            f"  ⦿ Publishes in flight: <b>{d.get('pending_scheduled', 0)}</b>",
        ]
        nxt = d.get("next_scheduled_utc")
        if nxt:
            parts.append(f"  ⦿ Next: <b>{html.escape(nxt)}</b>")
        parts.append("")
        parts.append("<b>Last maintenance sweeps</b>")
        parts.append(f"  ⦿ Update check: <b>{html.escape(d.get('last_update_check') or '—')}</b>")
        parts.append(f"  ⦿ Ban check: <b>{html.escape(d.get('last_ban_check') or '—')}</b>")
        parts.append("")
        parts.append(RULE_HEAVY)
        parts.append(t(M.STATS_UPDATED, ts=stats["ts"]))
        return "\n".join(parts)

    # ── refresh (post/edit/pin) ───────────────────────────────────────────────

    async def refresh(self) -> int | None:
        """Compute current stats and refresh the pinned stats message.

        Visits the index channel to get real published titles, then posts/pins
        a stats message showing total vs published vs not-indexed with official
        English names.
        """
        cfg = self._c.config.storage_channel
        client = getattr(self._c, "admin_client", None)
        if not (cfg.enabled and cfg.channel_id != 0 and client is not None):
            return None

        stats = await self.compute()
        text = self._format_message(stats)
        channel_id = cfg.channel_id

        # Guard: skip creating a new message if no series published yet
        if stats["total_series"] > 0 and stats["published_series"] == 0:
            log.info("stats.refresh.skipped_no_published_yet")
            raw = await safe_redis_get(self._c.redis, _STATS_MSG_KEY,
                                        label="stats.refresh.guard.get")
            if raw:
                try:
                    await client.edit_message_text(channel_id, int(raw), text)
                except Exception:
                    pass
            return None

        raw = await safe_redis_get(self._c.redis, _STATS_MSG_KEY,
                                    label="stats.refresh.existing.get")
        existing_id = int(raw) if raw else None

        try:
            if existing_id:
                await client.edit_message_text(channel_id, existing_id, text)
                log.info("stats.msg.updated", message_id=existing_id, total=stats["total_series"])
                return existing_id

            msg = await client.send_message(channel_id, text)
            await client.pin_chat_message(channel_id, msg.id, disable_notification=True)
            # Delete the "pinned this message" service notice.
            for candidate in range(msg.id + 1, msg.id + 4):
                try:
                    sm = await client.get_messages(channel_id, candidate)
                    if sm and getattr(sm, "pinned_message", None) is not None:
                        await client.delete_messages(channel_id, candidate)
                except Exception:
                    pass
            if self._c.redis:
                await safe_redis_set(self._c.redis, _STATS_MSG_KEY,
                                      str(msg.id),
                                      label="stats.refresh.created.set")
            log.info("stats.msg.created", message_id=msg.id)
            return msg.id
        except Exception as exc:
            log.warning("stats.refresh.failed", error=str(exc))
            return None
