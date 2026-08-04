"""Resilient metadata client — AniList first, then MyAnimeList, then @acutebot.

Every public method mirrors ``AnilistClient``'s signature and return types.
On connection failures, HTTP errors, or when AniList returns ``None``
(notably a 403 when AniList is down), the call is transparently retried
against the MyAnimeList (Jikan) fallback.

When BOTH AniList and Jikan miss — the whole outside world being unreachable,
rate-limited, or simply not carrying the title — ``search`` makes one last
attempt through the @acutebot userbot probe.  That tier is opt-in: the
container wires it up once via :meth:`enable_acute_fallback`; if the userbot
session or Telegram API credentials aren't present, the tier stays dormant
and the client behaves exactly as before.

Usage from container::

    self.anilist = ResilientMetadataClient()
    self.anilist.enable_acute_fallback(env)   # optional third tier

Every caller that previously did ``await container.anilist.search(…)`` or
``await container.anilist.walk_franchise_full(…)`` continues to work without
modification.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.anilist import AnilistClient
from nekofetch.sources.telegram.myanimelist import MyAnimeListClient

if TYPE_CHECKING:
    from nekofetch.sources.telegram.anilist import (
        AnilistMedia,
        FranchiseEntry,
        FranchiseTotals,
    )

log = get_logger(__name__)

_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _coerce_score(raw: Any) -> "float | None":
    """@acutebot scores arrive as strings ("8.14", "8.14 / 10", "N/A")."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"\d+(?:\.\d+)?", str(raw))
    return float(m.group(0)) if m else None


def _coerce_int(raw: Any) -> "int | None":
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    m = re.search(r"\d+", str(raw))
    return int(m.group(0)) if m else None


def _year_from(*candidates: Any) -> "int | None":
    for c in candidates:
        if c is None:
            continue
        m = _YEAR_RE.search(str(c))
        if m:
            return int(m.group(0))
    return None


def _acute_meta_to_media(meta: dict) -> "AnilistMedia | None":
    """Adapt @acutebot's flat legacy dict into an :class:`AnilistMedia`.

    @acutebot returns title/romaji/format/status/score/genres/synopsis/
    episode_count/poster_url plus an ``anilist_id`` when the Information
    button resolved.  We map the overlapping fields and leave the franchise
    breakdown at its defaults — callers that need full relation graphs use
    ``walk_franchise_full``/``franchise_totals`` with the recovered id.
    """
    from nekofetch.sources.telegram.anilist import AnilistMedia

    title = meta.get("title") or meta.get("romaji")
    if not title:
        return None

    aid = _coerce_int(meta.get("anilist_id")) or 0
    romaji = meta.get("romaji")
    titles = [t for t in (title, romaji) if t]

    return AnilistMedia(
        id=aid,
        format=meta.get("format"),
        season=None,
        year=_year_from(meta.get("first_aired"), meta.get("last_aired")),
        episodes=_coerce_int(meta.get("episode_count")),
        duration=_coerce_int(meta.get("runtime")),
        status=meta.get("status"),
        score=_coerce_score(meta.get("score")),
        popularity=None,
        genres=list(meta.get("genres") or []),
        synopsis=meta.get("synopsis"),
        cover_url=meta.get("poster_url"),
        banner_url=meta.get("banner_url"),
        english=title,
        romaji=romaji,
        titles=titles,
        anilist_url=(f"https://anilist.co/anime/{aid}" if aid else None),
    )


class ResilientMetadataClient:
    """Drop-in replacement for ``AnilistClient`` with automatic MAL fallback.

    Every method tries AniList first.  If AniList raises an exception or
    returns ``None`` (e.g. HTTP 403 — \"temporarily disabled\"), the call
    is transparently retried against MyAnimeList via the Jikan REST API.
    """

    def __init__(self) -> None:
        self.anilist = AnilistClient()
        self.mal = MyAnimeListClient()
        # Third tier (opt-in via enable_acute_fallback). Dormant by default.
        self._acute_env: Any = None
        self._acute_pool: Any = None

    def enable_acute_fallback(self, env: Any) -> None:
        """Arm the @acutebot tier used when AniList *and* Jikan both miss.

        ``env`` must expose ``telegram_api_id``, ``telegram_api_hash``,
        ``session_path`` and ``storage_path``.  We only stash it here — the
        userbot pool is built lazily on first use so process startup never
        blocks on a Telegram session that may not exist yet.
        """
        self._acute_env = env

    async def close(self) -> None:
        await self.anilist.close()
        await self.mal.close()

    # ── core fallback logic ───────────────────────────────────────────────────

    @staticmethod
    async def _try_both(
        primary_method, fallback_method, *args, **kwargs
    ):
        """Call ``primary_method``; on ``None`` or exception, call ``fallback_method``."""
        try:
            result = await primary_method(*args, **kwargs)
            if result is not None:
                return result
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "anilist.fallback",
                method=getattr(primary_method, "__name__", str(primary_method)),
                error=str(exc)[:200],
            )
        # Fallback
        try:
            return await fallback_method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "mal.fallback.failed",
                method=getattr(fallback_method, "__name__", str(fallback_method)),
                error=str(exc)[:200],
            )
            return None

    # ── public API ────────────────────────────────────────────────────────────

    async def _acute_search(self, query: str) -> "AnilistMedia | None":
        """Last-resort title lookup via the @acutebot userbot probe.

        Returns ``None`` (never raises) when the tier is disabled, the userbot
        session is unavailable, or @acutebot doesn't recognise the title — so
        it composes cleanly as the ``fallback_method`` of ``_try_both``.
        """
        env = self._acute_env
        if env is None:
            return None

        try:
            from nekofetch.providers.acute_bot import fetch_from_acutebot
            from nekofetch.sources.telegram.userbot import UserbotPool
        except Exception:  # noqa: BLE001 — optional dependency surface
            return None

        pool = self._acute_pool
        if pool is None:
            try:
                pool = UserbotPool.from_env(
                    env.telegram_api_id,
                    env.telegram_api_hash,
                    str(env.session_path),
                )
                self._acute_pool = pool
            except Exception as exc:  # noqa: BLE001
                log.debug("acute.no_pool", query=query, error=str(exc)[:200])
                return None

        try:
            photo_dir = str(env.storage_path / "acutebot_cards")
            meta = await fetch_from_acutebot(query, pool, photo_dir=photo_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("acute.fallback.failed", query=query, error=str(exc)[:200])
            return None

        if not meta:
            return None
        media = _acute_meta_to_media(meta)
        if media is not None:
            log.info("acute.fallback.hit", query=query, anilist_id=media.id)
        return media

    async def search(self, query: str) -> "AnilistMedia | None":
        # AniList → Jikan/MAL first.
        result = await self._try_both(self.anilist.search, self.mal.search, query)
        if result is not None:
            return result
        # Both public APIs missed — try the @acutebot userbot tier if armed.
        return await self._acute_search(query)

    async def search_candidates(self, query: str, *, limit: int = 25) -> "list[dict]":
        """Search-page candidates for the franchise picker. AniList-only: the
        grouping-into-franchises step just needs id/title/format, and a MAL page
        shape differs — on any AniList miss/error we return an empty list and the
        caller falls back to the single-best resolver."""
        try:
            return await self.anilist.search_candidates(query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.debug("resilient.search_candidates.failed",
                      query=query, error=str(exc)[:200])
            return []

    async def _fetch_full(self, media_id: int) -> "AnilistMedia | None":
        """Fetch full media data by ID.

        When ``media_id`` is a MAL ID (returned by a previous MAL ``search``)
        and AniList is unreachable, the call correctly falls through to MAL.
        If AniList is reachable but ``media_id`` doesn't exist on AniList,
        the result is ``None`` — the MAL fallback then handles it.
        """
        return await self._try_both(
            self.anilist._fetch_full, self.mal._fetch_full, media_id
        )

    async def franchise_totals(
        self, root_id: int, *, max_nodes: int = 120
    ) -> "FranchiseTotals":
        result = await self._try_both(
            self.anilist.franchise_totals,
            self.mal.franchise_totals,
            root_id,
            max_nodes=max_nodes,
        )
        # _try_both returns None on total failure, but both clients always
        # return FranchiseTotals (never None).  We fall back to empty totals.
        from nekofetch.sources.telegram.anilist import FranchiseTotals as FT

        return result if result is not None else FT()

    async def walk_franchise_full(
        self, root_id: int, *, max_nodes: int = 120
    ) -> "dict[int, FranchiseEntry]":
        result = await self._try_both(
            self.anilist.walk_franchise_full,
            self.mal.walk_franchise_full,
            root_id,
            max_nodes=max_nodes,
        )
        return result if result is not None else {}

    async def title_variants(self, query: str) -> "list[str]":
        result = await self._try_both(
            self.anilist.title_variants, self.mal.title_variants, query
        )
        return result if result is not None else [query]
