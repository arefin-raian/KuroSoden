"""TMDB client — backdrops + display metadata for the search-confirm UI.

Given an anime title we fetch the best TMDB match (TV first, then movie), its
display info, and an English promotional backdrop (16:9) for the confirmation
card. Auth uses the v4 read access token (Bearer); falls back to the v3 api_key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import httpx

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

TMDB_API = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"


@dataclass
class TmdbResult:
    id: int
    media_type: str           # "tv" | "movie"
    title: str
    year: str | None
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    overview: str = ""
    seasons: int | None = None
    episodes: int | None = None
    backdrop_url: str | None = None     # textless 16:9 backdrop (original size)
    poster_url: str | None = None
    logo_url: str | None = None         # transparent title-art logo (PNG)
    runtime: str | None = None          # human label, e.g. "24m" / "2h 1m"
    certification: str | None = None    # e.g. "TV-MA", "PG-13"
    studio: str | None = None           # primary production company
    origin_country: str | None = None   # ISO-3166 alpha-2, e.g. "JP"
    native_title: str | None = None     # original-language title

    def backdrop(self, size: str = "w1280") -> str | None:
        if not self._backdrop_path:
            return self.backdrop_url
        return f"{IMG_BASE}/{size}{self._backdrop_path}"

    _backdrop_path: str | None = None


class TmdbClient:
    def __init__(self, token: str | None = None, api_key: str | None = None) -> None:
        self.token = token or os.getenv("TMDB_API_READ_ACCESS_TOKEN", "")
        self.api_key = api_key or os.getenv("TMDB_API_KEY", "")
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._http = httpx.AsyncClient(timeout=20.0, headers=headers)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _params(self, **extra) -> dict:
        p = dict(extra)
        if not self.token and self.api_key:  # v3 key fallback
            p["api_key"] = self.api_key
        return p

    async def _get(self, path: str, **params) -> dict:
        # TMDB (via httpx on Windows) occasionally flakes on the FIRST request of
        # a fresh connection with a spurious 401/5xx, then succeeds on retry.
        # Retry transient statuses a couple of times before giving up so a cold
        # connection can't null out an entire enrichment.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.http.get(f"{TMDB_API}{path}",
                                        params=self._params(**params))
                if r.status_code in (401, 429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise
        if last_exc:
            raise last_exc
        return {}

    async def search(self, title: str, *, prefer_media: str | None = None,
                     anime: bool = True) -> TmdbResult | None:
        """Best match for the franchise base title, with a raw-title fallback.

        ``anime=True`` (the default for this pipeline) biases results toward
        Japanese-origin animation so a generically-named anime doesn't lose to a
        more popular live-action show that happens to share the title (the
        "K-drama backdrops" bug). ``prefer_media`` ("tv"/"movie") nudges the
        media type when the caller already knows it (e.g. a movie entry).
        """
        from nekofetch.services.franchise_flow import strip_season_tokens

        base_title = strip_season_tokens(title)

        async def _search(query: str) -> list[dict]:
            found: list[dict] = []
            for media in ("tv", "movie"):
                data = await self._get(f"/search/{media}", query=query,
                                       include_adult="true", language="en-US")
                for item in data.get("results", [])[:8]:
                    item["_media"] = media
                    found.append(item)
            return found

        try:
            candidates = await _search(base_title)
            if not candidates and base_title != title:
                candidates = await _search(title)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("tmdb.search.failed", title=title, error=str(exc))
            return None
        if not candidates:
            return None

        _ANIMATION_GENRE_ID = 16  # TMDB "Animation"

        def _is_jp(c: dict) -> bool:
            oc = c.get("origin_country") or []
            return "JP" in oc or (c.get("original_language") == "ja")

        def _is_anime(c: dict) -> bool:
            return _is_jp(c) and _ANIMATION_GENRE_ID in (c.get("genre_ids") or [])

        def rank(c: dict) -> tuple:
            # Highest priority first: true anime (JP + animation), then JP-origin,
            # then animation anywhere, then the media-type preference, then TV,
            # then popularity. When anime=False the anime tiers collapse to 0 so
            # behaviour matches the old popularity-first ordering.
            if anime:
                a_anime = _is_anime(c)
                a_jp = _is_jp(c)
                a_anim = _ANIMATION_GENRE_ID in (c.get("genre_ids") or [])
            else:
                a_anime = a_jp = a_anim = False
            pref = prefer_media is not None and c["_media"] == prefer_media
            return (a_anime, a_jp, a_anim, pref, c["_media"] == "tv",
                    c.get("popularity", 0))

        candidates.sort(key=rank, reverse=True)
        top = candidates[0]
        return await self.details(top["id"], top["_media"])

    async def details(self, tmdb_id: int, media_type: str) -> TmdbResult | None:
        is_tv = media_type == "tv"
        # One call pulls the core doc plus credits + certification via
        # append_to_response (cheaper than 3 round-trips).
        extra = "credits,content_ratings" if is_tv else "credits,release_dates"
        try:
            d = await self._get(f"/{media_type}/{tmdb_id}", language="en-US",
                                 append_to_response=extra)
            backdrop_path = await self._confirm_backdrop(tmdb_id, media_type) \
                or d.get("backdrop_path")
            logo_path = await self._logo(tmdb_id, media_type)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("tmdb.details.failed", id=tmdb_id, error=str(exc))
            return None

        title = d.get("name") if is_tv else d.get("title")
        date = d.get("first_air_date") if is_tv else d.get("release_date")
        res = TmdbResult(
            id=tmdb_id, media_type=media_type, title=title or "",
            year=(date or "")[:4] or None,
            genres=[g["name"] for g in d.get("genres", [])],
            rating=round(d["vote_average"], 1) if d.get("vote_average") else None,
            overview=d.get("overview", "") or "",
            seasons=d.get("number_of_seasons") if is_tv else None,
            episodes=d.get("number_of_episodes") if is_tv else None,
            poster_url=f"{IMG_BASE}/w500{d['poster_path']}" if d.get("poster_path") else None,
            logo_url=f"{IMG_BASE}/w500{logo_path}" if logo_path else None,
            runtime=self._runtime_label(d, is_tv),
            certification=self._certification(d, is_tv),
            studio=self._studio(d),
            origin_country=self._origin_country(d),
            native_title=(d.get("original_name") if is_tv else d.get("original_title")) or None,
        )
        res._backdrop_path = backdrop_path
        res.backdrop_url = res.backdrop("original")
        return res

    @staticmethod
    def _runtime_label(d: dict, is_tv: bool) -> str | None:
        mins = None
        if is_tv:
            arr = d.get("episode_run_time") or []
            mins = arr[0] if arr else None
        else:
            mins = d.get("runtime")
        if not mins:
            return None
        h, m = divmod(int(mins), 60)
        return f"{h}h {m}m" if h else f"{m}m"

    @staticmethod
    def _certification(d: dict, is_tv: bool) -> str | None:
        if is_tv:
            for r in (d.get("content_ratings", {}) or {}).get("results", []):
                if r.get("iso_3166_1") == "US" and r.get("rating"):
                    return r["rating"]
        else:
            for r in (d.get("release_dates", {}) or {}).get("results", []):
                if r.get("iso_3166_1") == "US":
                    for rd in r.get("release_dates", []):
                        if rd.get("certification"):
                            return rd["certification"]
        return None

    @staticmethod
    def _studio(d: dict) -> str | None:
        companies = d.get("production_companies") or []
        return companies[0]["name"] if companies and companies[0].get("name") else None

    @staticmethod
    def _origin_country(d: dict) -> str | None:
        countries = d.get("origin_country") or []
        if countries:
            return countries[0]
        oc = d.get("production_countries") or []
        return oc[0]["iso_3166_1"] if oc and oc[0].get("iso_3166_1") else None

    async def _logo(self, tmdb_id: int, media_type: str) -> str | None:
        """Best English (or neutral) transparent title-art logo."""
        try:
            imgs = await self._get(f"/{media_type}/{tmdb_id}/images",
                                   include_image_language="en,null")
        except (httpx.HTTPError, ValueError):
            return None
        logos = imgs.get("logos", [])
        if not logos:
            return None

        def quality(b: dict) -> tuple:
            # Prefer PNG (transparent) over SVG, then rating/votes.
            return (b.get("file_path", "").endswith(".png"),
                    b.get("vote_average") or 0, b.get("vote_count") or 0)

        english = sorted((b for b in logos if b.get("iso_639_1") == "en"),
                         key=quality, reverse=True)
        if english:
            return english[0].get("file_path")
        neutral = sorted((b for b in logos if not b.get("iso_639_1")),
                         key=quality, reverse=True)
        if neutral:
            return neutral[0].get("file_path")
        return sorted(logos, key=quality, reverse=True)[0].get("file_path")

    async def _confirm_backdrop(self, tmdb_id: int, media_type: str) -> str | None:
        """Pick the best backdrop for the **confirmation card** — English-tagged
        FIRST (the ``?image_language=en`` gallery: art with the English title
        baked in), falling back to textless (``iso_639_1 == null``) only when no
        English backdrop exists, then anything, ranked by rating/votes/res.

        This is the opposite ordering from the thumbnail-generator picker, which
        offers only textless art because the user overlays their own logo there.
        """
        try:
            imgs = await self._get(f"/{media_type}/{tmdb_id}/images",
                                   include_image_language="en,null")
        except (httpx.HTTPError, ValueError):
            return None
        backdrops = imgs.get("backdrops", [])
        if not backdrops:
            return None

        def quality(b: dict) -> tuple:
            return (b.get("vote_average") or 0,
                    b.get("vote_count") or 0,
                    b.get("width") or 0)

        # English first (has the English title text — what we want on the card).
        english = sorted((b for b in backdrops if b.get("iso_639_1") == "en"),
                         key=quality, reverse=True)
        if english:
            return english[0].get("file_path")
        # Fallback: textless (no language tag = no title art baked in).
        neutral = sorted((b for b in backdrops if not b.get("iso_639_1")),
                         key=quality, reverse=True)
        if neutral:
            return neutral[0].get("file_path")
        return sorted(backdrops, key=quality, reverse=True)[0].get("file_path")

    async def _ranked_backdrops(self, tmdb_id: int, media_type: str) -> list[str]:
        """English-tagged then textless backdrop paths, best-first (no dupes).

        Same ordering rationale as ``_confirm_backdrop`` — English title-art
        first, language-neutral next — but returns the whole ranked gallery so
        a caller can rotate through an anime's different pieces of art.
        """
        try:
            imgs = await self._get(f"/{media_type}/{tmdb_id}/images",
                                   include_image_language="en,null")
        except (httpx.HTTPError, ValueError):
            return []
        backdrops = imgs.get("backdrops", [])

        def quality(b: dict) -> tuple:
            return (b.get("vote_average") or 0, b.get("vote_count") or 0, b.get("width") or 0)

        english = sorted((b for b in backdrops if b.get("iso_639_1") == "en"),
                         key=quality, reverse=True)
        neutral = sorted((b for b in backdrops if not b.get("iso_639_1")),
                         key=quality, reverse=True)
        other = sorted((b for b in backdrops
                        if b.get("iso_639_1") not in ("en", None)),
                       key=quality, reverse=True)
        seen: set = set()
        out: list[str] = []
        for b in (*english, *neutral, *other):
            path = b.get("file_path")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        return out

    async def backdrops(self, title: str, *, size: str = "w1280",
                        limit: int = 8) -> list[str]:
        """Up to ``limit`` full backdrop URLs for ``title`` — the anime's own art
        gallery, English-first then textless. Used to seed the per-anime artwork
        rotation so every card for that title shows a different piece of its art.
        """
        res = await self.search(title)
        if res is None:
            return []
        ranked = await self._ranked_backdrops(res.id, res.media_type)
        urls = [f"{IMG_BASE}/{size}{p}" for p in ranked[:limit]]
        if not urls and res.backdrop_url:
            urls = [res.backdrop_url]
        return urls

    async def _ranked_posters(self, tmdb_id: int, media_type: str) -> list[str]:
        """English/region-neutral poster paths, best-first — the same set TMDB's
        ``/images/posters?image_language=en&image_region=US`` page shows. English
        title-art posters first, then language-neutral, ranked by rating/votes/res."""
        try:
            imgs = await self._get(f"/{media_type}/{tmdb_id}/images",
                                   include_image_language="en,null")
        except (httpx.HTTPError, ValueError):
            return []
        posters = imgs.get("posters", [])

        def quality(b: dict) -> tuple:
            return (b.get("vote_average") or 0, b.get("vote_count") or 0, b.get("width") or 0)

        english = sorted((b for b in posters if b.get("iso_639_1") == "en"),
                         key=quality, reverse=True)
        neutral = sorted((b for b in posters if not b.get("iso_639_1")),
                         key=quality, reverse=True)
        seen: set = set()
        out: list[str] = []
        for b in (*english, *neutral):
            path = b.get("file_path")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        return out

    async def _english_poster(self, tmdb_id: int, media_type: str) -> str | None:
        ranked = await self._ranked_posters(tmdb_id, media_type)
        return ranked[0] if ranked else None

    async def poster_for(self, title: str, *, size: str = "w342", rank: int = 0) -> str | None:
        """Official English/US poster URL for ``title``, sized for the use site.

        ``rank=0`` is the best poster (used for file thumbnails); ``rank=1`` returns
        a DIFFERENT poster (used for a bot's profile photo) so the avatar isn't a
        carbon copy of the file thumbnails. Falls back to the best available."""
        res = await self.search(title)
        if res is None:
            return None
        ranked = await self._ranked_posters(res.id, res.media_type)
        if ranked:
            path = ranked[rank] if rank < len(ranked) else ranked[0]
            return f"{IMG_BASE}/{size}{path}"
        return res.poster_url
