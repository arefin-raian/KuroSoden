"""Per-request distribution data cache — fetch once, read everywhere, clear on publish.

The Senku distribution wizard needs a title's full franchise map and per-entry
TMDB/AniList data at several steps (franchise map, thumbnail loop, watch-order
confirm). Re-hitting AniList/TMDB at every button tap would burn rate limits, so
this caches the whole thing in Redis on first touch, keyed by request code, and
clears it once the info card is posted.

Layout (all keyed by request ``code``):

    nf:dist:{code}:franchise   — the canonical franchise dict (durable-ish, TTL'd)
    nf:dist:{code}:entries     — the ordered, canonical entry list (season/movie/OVA)
    nf:dist:{code}:selections  — per-entry asset picks (logo/poster/bg/thumbnail)
    nf:dist:{code}:channel     — the verified distribution channel handle/id

Everything is best-effort and TTL-guarded: a stale cache self-expires so an
abandoned wizard can't wedge Redis forever. The durable franchise map still lives
on the request row (``franchise_data``); this cache is the volatile working set
the wizard reads and mutates, then discards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import (
    safe_redis_delete,
    safe_redis_get,
    safe_redis_set,
)

log = get_logger(__name__)

# Seven days: long enough for an admin to finish a multi-entry franchise across
# sessions, short enough that an abandoned wizard evaporates on its own.
_DEFAULT_TTL = 7 * 24 * 3600

_K_FRANCHISE = "nf:dist:{code}:franchise"
_K_ENTRIES = "nf:dist:{code}:entries"
_K_SELECTIONS = "nf:dist:{code}:selections"
_K_CHANNEL = "nf:dist:{code}:channel"

_ALL_KEYS = (_K_FRANCHISE, _K_ENTRIES, _K_SELECTIONS, _K_CHANNEL)


@dataclass
class EntryData:
    """One canonical franchise entry the wizard walks through, in watch order."""
    index: int
    label: str                       # e.g. "Season 3 Part 2" / "Movie: Stampede"
    kind: str = "season"             # season | movie | special
    season_number: int = 1
    season_part: int | None = None
    title: str = ""                  # the AniList/TMDB English title
    episodes: int | None = None
    anilist_id: int | None = None
    tmdb_id: int | None = None
    media_type: str = "tv"           # tv | movie (for TMDB asset fetch)
    format: str = "tv"


@dataclass
class Selection:
    """Per-entry asset picks and the rendered thumbnail, keyed by entry index."""
    logo_url: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None
    thumbnail_url: str | None = None
    done: bool = False


class DistributionCache:
    """Read/mutate the volatile distribution working set for one request code."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._redis = container.redis

    # ── Seeding ─────────────────────────────────────────────────────────────

    async def ensure(self, code: str) -> dict | None:
        """Populate the cache for ``code`` on first touch; return the franchise dict.

        Idempotent: if the franchise is already cached, returns it untouched.
        Otherwise resolves the franchise from the request row (which already
        persists ``franchise_data`` from intake) and expands its canonical entry
        list via :class:`FranchiseFlowService`, then stores both.
        """
        existing = await self.get_franchise(code)
        if existing is not None:
            return existing

        franchise = await self._resolve_franchise(code)
        if not franchise:
            log.warning("dist_cache.ensure.no_franchise", code=code)
            return None

        entries = await self._expand_entries(code, franchise)

        await safe_redis_set(
            self._redis, _K_FRANCHISE.format(code=code),
            json.dumps(franchise), ex=_DEFAULT_TTL, label="dist_cache.franchise.set",
        )
        await safe_redis_set(
            self._redis, _K_ENTRIES.format(code=code),
            json.dumps([asdict(e) for e in entries]), ex=_DEFAULT_TTL,
            label="dist_cache.entries.set",
        )
        log.info("dist_cache.ensure.seeded", code=code, entries=len(entries))
        return franchise

    async def _resolve_franchise(self, code: str) -> dict | None:
        """Pull the franchise dict off the request row (best-effort)."""
        try:
            from nekofetch.services.request_service import RequestService

            req = await RequestService(self._c).get(code)
        except Exception as exc:  # noqa: BLE001
            log.warning("dist_cache.resolve.request_failed", code=code, error=str(exc))
            return None

        franchise = dict(getattr(req, "franchise_data", None) or {})
        if not franchise:
            # Intake stored no franchise data (e.g. Telegram-source or a provider
            # miss) — fall back to a live resolve so the wizard still has a map.
            # NEVER resolve with the request CODE (``REQ-1058``): it isn't a
            # searchable title and would leak straight into the @acutebot /anime
            # query (and then surface as the channel/AcuteBot "title"). Only
            # resolve when we have a real anime title.
            title = getattr(req, "anime_title", None)
            if title:
                try:
                    from kurosoden.shared.franchise_resolver import resolve_franchise

                    franchise = await resolve_franchise(self._c, title) or {}
                except Exception as exc:  # noqa: BLE001
                    log.debug("dist_cache.resolve.live_failed", code=code, error=str(exc))
                    franchise = {}
            else:
                log.warning("dist_cache.resolve.no_title", code=code,
                            hint="request has no anime_title; skipping live resolve "
                                 "so the code is never sent to metadata providers")
                franchise = {}

        if franchise:
            # Stamp identity so downstream steps don't need the request row again.
            franchise.setdefault("_code", code)
            franchise.setdefault("anime_doc_id", getattr(req, "anime_doc_id", None))
            franchise.setdefault("anime_title", getattr(req, "anime_title", None))
        return franchise or None

    async def _expand_entries(self, code: str, franchise: dict) -> list[EntryData]:
        """Expand the franchise into an ordered, canonical entry list.

        Reuses :class:`FranchiseFlowService.build_mapping` (the same smart
        season/part detection the request pipeline uses) so the watch order here
        matches the rest of the system. Only ``included`` entries survive — that
        drops spin-offs/recaps the mapping already excludes.
        """
        try:
            from nekofetch.services.franchise_flow import FranchiseFlowService

            doc_id = franchise.get("anime_doc_id") or code
            mapping = FranchiseFlowService(self._c).build_mapping(franchise, doc_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("dist_cache.expand.failed", code=code, error=str(exc))
            return self._entries_from_relations(franchise)

        entries = self._mapping_to_entries(mapping)
        if entries:
            return entries
        return self._entries_from_relations(franchise)

    def _mapping_to_entries(self, mapping: Any) -> list[EntryData]:
        """Convert a :class:`FranchiseMapping`'s included entries to ``EntryData``.

        Shared by the first expansion and the watch-order edit path so both
        produce identically-shaped entries (index, label, kind, ids).
        """
        entries: list[EntryData] = []
        for i, e in enumerate(mapping.included_entries, start=1):
            kind = getattr(e.kind, "value", None) or str(getattr(e, "kind", "season"))
            entries.append(EntryData(
                index=i,
                label=self._entry_label(e),
                kind=str(kind).lower(),
                season_number=getattr(e, "season_number", 1),
                season_part=getattr(e, "season_part", None),
                title=getattr(e, "title", "") or "",
                episodes=getattr(e, "episodes", None),
                anilist_id=getattr(e, "anilist_id", None),
                media_type="movie" if str(kind).lower() == "movie" else "tv",
                format=getattr(e, "format", None) or "tv",
            ))
        return entries

    async def apply_order_correction(self, code: str, text: str) -> list[EntryData] | None:
        """Re-map an admin's edited watch-order text and persist the result.

        Rebuilds the canonical mapping, applies the correction via
        :meth:`FranchiseFlowService.parse_mapping_correction` (the same parser
        the request pipeline uses), converts back to ``EntryData``, and
        overwrites the cached entry list. Returns the new entries, or ``None``
        if the text couldn't be parsed (caller shows the retry prompt).
        """
        franchise = await self.get_franchise(code) or await self.ensure(code)
        if not franchise:
            return None
        try:
            from nekofetch.services.franchise_flow import FranchiseFlowService

            svc = FranchiseFlowService(self._c)
            doc_id = franchise.get("anime_doc_id") or code
            mapping = svc.build_mapping(franchise, doc_id)
            corrected = svc.parse_mapping_correction(text, mapping)
        except Exception as exc:  # noqa: BLE001
            log.warning("dist_cache.order_edit.failed", code=code, error=str(exc))
            return None
        if corrected is None:
            return None
        entries = self._mapping_to_entries(corrected)
        if not entries:
            return None
        await self.set_entries(code, entries)
        return entries

    @staticmethod
    def _entry_label(entry: Any) -> str:
        """Short, tree-safe label for an entry (delegates to FranchiseFlowService)."""
        try:
            from nekofetch.services.franchise_flow import FranchiseFlowService

            return FranchiseFlowService.entry_label(entry)
        except Exception:  # noqa: BLE001
            part = getattr(entry, "season_part", None)
            base = f"Season {getattr(entry, 'season_number', 1)}"
            return f"{base} Part {part}" if part else base

    @staticmethod
    def _entries_from_relations(franchise: dict) -> list[EntryData]:
        """Last-ditch entry list when mapping is unavailable: one entry per relation.

        Keeps the wizard functional for a bare franchise dict (e.g. a Telegram
        source that never resolved a relation graph) by treating the root title
        as a single season.
        """
        title = franchise.get("english") or franchise.get("title") or "Anime"
        return [EntryData(
            index=1, label="Season 1", kind="season", season_number=1,
            title=title, anilist_id=franchise.get("anilist_id"),
        )]

    # ── Reads ───────────────────────────────────────────────────────────────

    async def get_franchise(self, code: str) -> dict | None:
        raw = await safe_redis_get(
            self._redis, _K_FRANCHISE.format(code=code), label="dist_cache.franchise.get",
        )
        return json.loads(raw) if raw else None

    async def get_entries(self, code: str) -> list[EntryData]:
        raw = await safe_redis_get(
            self._redis, _K_ENTRIES.format(code=code), label="dist_cache.entries.get",
        )
        if not raw:
            return []
        try:
            return [EntryData(**d) for d in json.loads(raw)]
        except (TypeError, ValueError) as exc:
            log.warning("dist_cache.entries.decode_failed", code=code, error=str(exc))
            return []

    async def get_entry(self, code: str, index: int) -> EntryData | None:
        for e in await self.get_entries(code):
            if e.index == index:
                return e
        return None

    async def get_selections(self, code: str) -> dict[int, Selection]:
        raw = await safe_redis_get(
            self._redis, _K_SELECTIONS.format(code=code), label="dist_cache.sel.get",
        )
        if not raw:
            return {}
        try:
            return {int(k): Selection(**v) for k, v in json.loads(raw).items()}
        except (TypeError, ValueError) as exc:
            log.warning("dist_cache.sel.decode_failed", code=code, error=str(exc))
            return {}

    async def get_selection(self, code: str, index: int) -> Selection:
        return (await self.get_selections(code)).get(index, Selection())

    async def get_channel(self, code: str) -> dict | None:
        raw = await safe_redis_get(
            self._redis, _K_CHANNEL.format(code=code), label="dist_cache.channel.get",
        )
        return json.loads(raw) if raw else None

    # ── Writes ──────────────────────────────────────────────────────────────

    async def set_selection(
        self, code: str, index: int, *, asset: str | None = None,
        value: str | None = None, done: bool | None = None,
    ) -> Selection:
        """Update one entry's selection (asset pick or done flag); return the row.

        ``asset`` is one of ``logo`` / ``poster`` / ``bg`` / ``thumbnail`` and
        stores ``value`` in the matching field. Pass ``done=True`` to mark the
        entry finished. Read-modify-write of the whole selections blob keeps it a
        single Redis key (small, per-request).
        """
        selections = await self.get_selections(code)
        sel = selections.get(index, Selection())
        field_map = {
            "logo": "logo_url", "poster": "poster_url",
            "bg": "backdrop_url", "backdrop": "backdrop_url",
            "thumbnail": "thumbnail_url",
        }
        if asset and asset in field_map:
            setattr(sel, field_map[asset], value)
        if done is not None:
            sel.done = done
        selections[index] = sel

        await safe_redis_set(
            self._redis, _K_SELECTIONS.format(code=code),
            json.dumps({str(k): asdict(v) for k, v in selections.items()}),
            ex=_DEFAULT_TTL, label="dist_cache.sel.set",
        )
        return sel

    async def set_entries(self, code: str, entries: list[EntryData]) -> None:
        """Overwrite the entry list (used after a watch-order edit)."""
        await safe_redis_set(
            self._redis, _K_ENTRIES.format(code=code),
            json.dumps([asdict(e) for e in entries]), ex=_DEFAULT_TTL,
            label="dist_cache.entries.overwrite",
        )

    async def set_channel(self, code: str, *, handle: str, chat_id: int | None = None) -> None:
        await safe_redis_set(
            self._redis, _K_CHANNEL.format(code=code),
            json.dumps({"handle": handle, "chat_id": chat_id}),
            ex=_DEFAULT_TTL, label="dist_cache.channel.set",
        )

    async def all_done(self, code: str) -> bool:
        """True when every cached entry has a rendered thumbnail (or none exist)."""
        entries = await self.get_entries(code)
        if not entries:
            return False
        selections = await self.get_selections(code)
        return all(selections.get(e.index, Selection()).done for e in entries)

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def clear(self, code: str) -> None:
        """Drop every cache key for ``code`` — called once the info card is posted."""
        for tmpl in _ALL_KEYS:
            await safe_redis_delete(
                self._redis, tmpl.format(code=code), label="dist_cache.clear",
            )
        log.info("dist_cache.cleared", code=code)
