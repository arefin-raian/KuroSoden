"""Local anime dataset tier — fast, offline-ish metadata from a cached CSV.

A second tier right after AniList (before the REST APIs) sourced from the
public LeoRigasaki/Anime-dataset GitHub repo. The repo publishes date-stamped
**seasonal** snapshots (``data/raw/anilist_seasonal_YYYYMMDD.csv``) built from
AniList, so the columns already match AniList's conventions (``anime_id`` is the
AniList id, plus ``mal_id``, titles, type, episodes, score, genres, synopsis,
cover/banner). We cache one snapshot to disk and index it in memory; lookups are
then instant and survive AniList/API outages.

Scope + limits (be honest about them):
* The snapshot is **seasonal** (~the current season, ~900 titles), so this tier
  only *hits* recent titles; anything older simply returns ``None`` and the
  resilient chain falls through to Jikan/Kitsu. That's by design — this is a
  fast-path cache, not a complete database.
* There is **no relation graph** in the CSV, so ``walk_franchise_full`` /
  ``franchise_totals`` return empty and the chain falls through for franchise
  structure (Kaggle/Jikan/Kitsu supply that).

No GitHub API is used (its unauth quota is 60/hr): the newest snapshot is found
by walking dated raw URLs back from today, which ``raw.githubusercontent.com``
serves without rate limiting. Network + CSV parsing run in ``asyncio.to_thread``
so the bot's event loop never blocks.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
from pathlib import Path

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.anilist import AnilistMedia, FranchiseTotals

log = get_logger(__name__)

_RAW_BASE = "https://raw.githubusercontent.com/LeoRigasaki/Anime-dataset/main/data/raw"
_SNAPSHOT_FMT = "anilist_seasonal_%Y%m%d.csv"
_REFRESH_SECONDS = 86_400  # re-download at most once a day
_WALK_BACK_DAYS = 30       # how far back to look for the newest snapshot

_ANIME_FORMATS = {"TV", "TV_SHORT", "MOVIE", "OVA", "ONA", "SPECIAL", "MUSIC"}


def _norm(title: str) -> str:
    """Loose normalisation key for title matching."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def _split_list(raw: str) -> list[str]:
    """Genres/tags arrive as a delimited string; split on the usual separators."""
    if not raw:
        return []
    for sep in ("|", ";", ","):
        if sep in raw:
            return [p.strip() for p in raw.split(sep) if p.strip()]
    return [raw.strip()] if raw.strip() else []


def _int(raw) -> int | None:
    try:
        return int(float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _score(raw) -> float | None:
    """Dataset ``score`` is AniList averageScore (0-100); our convention is 0-10."""
    v = _int(raw)
    return round(v / 10, 1) if v is not None else None


class AnimeDatasetClient:
    """AniList-shaped tier backed by a cached seasonal CSV snapshot.

    Mirrors the subset of the ``AnilistClient`` interface the resilient chain
    uses. Relation methods intentionally return empties (the CSV has no graph),
    so the chain falls through to the API tiers for franchise structure.
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        # Default matches the config STORAGE_PATH default ("data/storage"); the
        # container overrides it with the real env.storage_path via set_cache_dir.
        base = Path(cache_dir) if cache_dir else Path("data/storage") / "cache"
        self._cache_dir = base
        self._csv_path = base / "anilist_dataset.csv"
        self._by_title: dict[str, dict] = {}
        self._by_id: dict[int, dict] = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

    def set_cache_dir(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._csv_path = self._cache_dir / "anilist_dataset.csv"

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── snapshot acquisition + indexing ───────────────────────────────────────

    def _needs_refresh(self) -> bool:
        try:
            if not self._csv_path.exists():
                return True
            age = datetime.datetime.now().timestamp() - self._csv_path.stat().st_mtime
            return age > _REFRESH_SECONDS
        except OSError:
            return True

    async def prefetch(self) -> bool:
        """Download the CSV to disk NOW (foreground), if absent or stale.

        Launcher/warm-up counterpart to :meth:`_ensure_loaded` — awaits the
        download synchronously so the CSV is on disk before the bots start,
        instead of the runtime path's download-and-refresh. Idempotent: a present,
        non-stale CSV is a no-op (True). No in-memory index is built here. Returns
        True when the CSV exists on disk afterwards."""
        if not self._needs_refresh():
            return True
        blob = await self._download_latest()
        if blob:
            await asyncio.to_thread(self._write_cache, blob)
        return self._csv_path.exists()

    async def _download_latest(self) -> bytes | None:
        """Fetch the newest dated snapshot via raw URLs (no GitHub API)."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        today = datetime.date.today()
        for delta in range(_WALK_BACK_DAYS):
            day = today - datetime.timedelta(days=delta)
            url = f"{_RAW_BASE}/{day.strftime(_SNAPSHOT_FMT)}"
            try:
                resp = await self._http.get(url)
                if resp.status_code == 200 and resp.content:
                    log.info("dataset.snapshot.fetched", url=url,
                             bytes=len(resp.content))
                    return resp.content
            except Exception as exc:  # noqa: BLE001
                log.debug("dataset.snapshot.miss", url=url, error=str(exc)[:120])
        log.warning("dataset.snapshot.none", window_days=_WALK_BACK_DAYS)
        return None

    def _write_cache(self, blob: bytes) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._csv_path.with_suffix(".csv.tmp")
        tmp.write_bytes(blob)
        tmp.replace(self._csv_path)

    def _index_csv(self, text: str) -> None:
        by_title: dict[str, dict] = {}
        by_id: dict[int, dict] = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            for key in ("title", "english_title", "user_preferred_title",
                        "japanese_title"):
                t = (row.get(key) or "").strip()
                if t:
                    by_title.setdefault(_norm(t), row)
            aid = _int(row.get("anime_id"))
            mid = _int(row.get("mal_id"))
            if aid is not None:
                by_id[aid] = row
            if mid is not None:
                by_id.setdefault(mid, row)
        self._by_title, self._by_id = by_title, by_id
        log.info("dataset.indexed", titles=len(by_title), ids=len(by_id))

    async def _ensure_loaded(self) -> bool:
        """Load (and refresh) the snapshot once; returns False if unavailable."""
        if self._loaded and self._by_title:
            # Still consider a daily refresh, but never block a ready index on it.
            if not self._needs_refresh():
                return True
        async with self._load_lock:
            if self._loaded and self._by_title and not self._needs_refresh():
                return True
            blob: bytes | None = None
            if self._needs_refresh():
                blob = await self._download_latest()
                if blob:
                    try:
                        await asyncio.to_thread(self._write_cache, blob)
                    except OSError as exc:
                        log.warning("dataset.cache_write_failed", error=str(exc))
            if blob is None and self._csv_path.exists():
                try:
                    blob = await asyncio.to_thread(self._csv_path.read_bytes)
                except OSError as exc:
                    log.warning("dataset.cache_read_failed", error=str(exc))
                    blob = None
            if not blob:
                self._loaded = True  # avoid hammering downloads every call
                return bool(self._by_title)
            try:
                text = blob.decode("utf-8", errors="replace")
                await asyncio.to_thread(self._index_csv, text)
            except Exception as exc:  # noqa: BLE001
                log.warning("dataset.index_failed", error=str(exc))
            self._loaded = True
            return bool(self._by_title)

    # ── row → AnilistMedia ─────────────────────────────────────────────────────

    def _row_to_media(self, row: dict) -> AnilistMedia | None:
        aid = _int(row.get("anime_id")) or _int(row.get("mal_id"))
        if aid is None:
            return None
        english = (row.get("english_title") or "").strip()
        romaji = (row.get("title") or row.get("user_preferred_title") or "").strip()
        native = (row.get("japanese_title") or "").strip()
        titles = [t for t in (english, romaji, native,
                              (row.get("user_preferred_title") or "").strip()) if t]
        # de-dup, keep order
        seen: set[str] = set()
        titles = [t for t in titles if not (t.lower() in seen or seen.add(t.lower()))]

        fmt = (row.get("type") or "").strip().upper() or None
        if fmt not in _ANIME_FORMATS:
            fmt = fmt if fmt else None
        episodes = _int(row.get("episodes"))
        return AnilistMedia(
            id=aid,
            format=fmt,
            season=(row.get("season") or None),
            year=_int(row.get("season_year")),
            start_date=None,
            episodes=episodes,
            duration=_int(row.get("duration")),
            status=(row.get("status") or "").strip().upper() or None,
            score=_score(row.get("score") or row.get("mean_score")),
            popularity=_int(row.get("popularity")),
            genres=_split_list(row.get("genres") or ""),
            synopsis=(row.get("synopsis") or "").strip() or None,
            studio=(_split_list(row.get("main_studios") or row.get("studios") or "")
                    or [None])[0],
            cover_url=(row.get("cover_image_large") or "").strip() or None,
            banner_url=(row.get("banner_image") or "").strip() or None,
            english=english or romaji,
            romaji=romaji or english,
            titles=titles,
            synonyms=[],
            relations=[],  # no relation graph in the seasonal CSV
            anilist_url=(f"https://anilist.co/anime/{aid}"
                         if _int(row.get("anime_id")) else None),
            # Single-entry view: the CSV has no franchise graph, so report this
            # title as one season with its own episode count (never a fake total).
            franchise_episodes=episodes,
            franchise_seasons=1,
        )

    def _find_row(self, query: str) -> dict | None:
        key = _norm(query)
        if not key:
            return None
        row = self._by_title.get(key)
        if row is not None:
            return row
        # Loose containment fallback — but only for keys long enough that a
        # substring match is meaningful. A dataset miss just falls through to the
        # API tiers (which do proper fuzzy matching), so it's safer to return
        # None than a wrong loose hit on a short query ("One", "Air", …).
        if len(key) < 8:
            return None
        candidates = [
            (t, r) for t, r in self._by_title.items()
            if key in t or t in key
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda tr: len(tr[0]))
        return candidates[0][1]

    # ── public API (AnilistClient-shaped) ──────────────────────────────────────

    async def search(self, query: str) -> AnilistMedia | None:
        if not await self._ensure_loaded():
            return None
        row = self._find_row(query)
        return self._row_to_media(row) if row is not None else None

    async def search_candidates(
        self, query: str, *, limit: int = 25
    ) -> list[dict]:
        if not await self._ensure_loaded():
            return []
        key = _norm(query)
        if not key:
            return []
        out: list[dict] = []
        seen_ids: set[int] = set()
        for t, row in self._by_title.items():
            if key not in t and t not in key:
                continue
            aid = _int(row.get("anime_id")) or _int(row.get("mal_id"))
            if aid is None or aid in seen_ids:
                continue
            seen_ids.add(aid)
            eng = (row.get("english_title") or row.get("title") or "").strip()
            out.append({
                "id": aid,
                "title": eng,
                "format": (row.get("type") or "").strip().upper() or None,
                "popularity": _int(row.get("popularity")) or 0,
            })
            if len(out) >= limit:
                break
        return out

    async def _fetch_full(self, media_id: int) -> AnilistMedia | None:
        if not await self._ensure_loaded():
            return None
        row = self._by_id.get(int(media_id)) if media_id is not None else None
        return self._row_to_media(row) if row is not None else None

    async def franchise_totals(
        self, root_id: int, *, max_nodes: int = 120
    ) -> FranchiseTotals:
        # No relation graph in the dataset — empty so the chain falls through.
        return FranchiseTotals()

    async def walk_franchise_full(
        self, root_id: int, *, max_nodes: int = 120
    ) -> dict:
        return {}

    async def title_variants(self, query: str) -> list[str]:
        media = await self.search(query)
        return list(media.titles) if media and media.titles else [query]
