"""MyAnimeList metadata — franchise discovery, enrichment & relation resolution.

Fallback for when AniList is down. Mirrors the ``AnilistClient`` interface
(search, franchise walk, totals, title variants) by talking to the Jikan REST
API v4 (https://api.jikan.moe/v4), an unofficial MyAnimeList scraped API.

All public methods return the **same dataclass types** as ``AnilistClient``
(``AnilistMedia``, ``FranchiseEntry``, ``FranchiseTotals``) so callers can swap
the client transparently.  Field names come from AniList's conventions; the
mappings to MAL/Jikan fields are handled internally.

Rate limits
-----------
Jikan's public API allows ~3 req/s and ~60 req/min (varies by load).  We
enforce a 400 ms gap between requests and honour ``Retry-After`` on 429
responses with one automatic retry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

try:  # Chrome-TLS transport — the actual fix for Cloudflare's 504 wall.
    from curl_cffi import requests as cf_requests
except ImportError:  # pragma: no cover - curl_cffi is a hard dep, but be safe
    cf_requests = None  # type: ignore[assignment]

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.anilist import (
    ANILIST_SITE,
    AnilistMedia,
    FranchiseEntry,
    FranchiseRelation,
    FranchiseTotals,
    _aired_episodes,
    _ANIME_FORMATS,
    _CONTENT_WALK_RELS,
    _CONTINUATION_RELATIONS,
    _EXCLUDED_STATUS,
    _SERIES_FORMATS,
    _TRAVERSE_RELATIONS,
    _detect_part_from_title,
)

log = get_logger(__name__)

JIKAN_URL = "https://api.jikan.moe/v4"
MAL_SITE = "https://myanimelist.net/anime"

# ── format / status / relation mapping ────────────────────────────────────────

_JIKAN_FORMAT: dict[str, str] = {
    "TV": "TV",
    "TV_SHORT": "TV_SHORT",
    "Movie": "MOVIE",
    "OVA": "OVA",
    "ONA": "ONA",
    "Special": "SPECIAL",
    "Music": "MUSIC",
}

_JIKAN_STATUS: dict[str, str] = {
    "Finished Airing": "FINISHED",
    "Currently Airing": "RELEASING",
    "Not yet aired": "NOT_YET_RELEASED",
    "Cancelled": "CANCELLED",
}

_JIKAN_RELATION: dict[str, str] = {
    "Sequel": "SEQUEL",
    "Prequel": "PREQUEL",
    "Side Story": "SIDE_STORY",
    "Parent Story": "PARENT",
    "Spin-off": "SPIN_OFF",
    "Summary": "SUMMARY",
    "Alternative": "ALTERNATIVE",
    "Adaptation": "ADAPTATION",
    "Character": "CHARACTER",
    "Other": "OTHER",
}

_RATE_LIMIT_GAP = 0.4  # seconds between requests (~3 req/s)


def _jikan_format(fmt: str | None) -> str | None:
    if fmt is None:
        return None
    mapped = _JIKAN_FORMAT.get(fmt)
    return mapped if mapped in _ANIME_FORMATS else None


def _jikan_status(status: str | None) -> str | None:
    if status is None:
        return None
    return _JIKAN_STATUS.get(status)


def _jikan_relation(rel: str) -> str | None:
    return _JIKAN_RELATION.get(rel)


def _parse_start_date(aired: dict | None) -> dict | None:
    """Convert Jikan's ``aired.prop.from`` to ``{year, month, day}``."""
    if not aired:
        return None
    prop = aired.get("prop") or {}
    frm = prop.get("from") or {}
    year = frm.get("year")
    month = frm.get("month")
    day = frm.get("day")
    if year or month or day:
        return {"year": year, "month": month, "day": day}
    # Fallback: parse ISO string.
    from_str = aired.get("from")
    if from_str:
        try:
            dt = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
            return {"year": dt.year, "month": dt.month, "day": dt.day}
        except (ValueError, TypeError):
            pass
    return None


def _extract_titles(data: dict) -> list[str]:
    """Return [english, romaji, native, ...] from Jikan's ``titles`` array."""
    titles: list[str] = []
    seen: set[str] = set()
    for t in data.get("titles") or []:
        title = (t.get("title") or "").strip()
        if title and title.lower() not in seen:
            seen.add(title.lower())
            titles.append(title)
    # Ensure English and Japanese are included even if not in titles array.
    eng = (data.get("title_english") or "").strip()
    jpn = (data.get("title_japanese") or "").strip()
    if eng and eng.lower() not in seen:
        seen.add(eng.lower())
        titles.append(eng)
    if jpn and jpn.lower() not in seen:
        titles.append(jpn)
    return titles


def _synonyms(data: dict) -> list[str]:
    """Return alternative titles from Jikan's ``titles`` array (non-Default)."""
    synonyms: list[str] = []
    seen: set[str] = set()
    for t in data.get("titles") or []:
        ttype = (t.get("type") or "").strip()
        title = (t.get("title") or "").strip()
        if ttype != "Default" and title and title.lower() not in seen:
            seen.add(title.lower())
            synonyms.append(title)
    return synonyms


class MyAnimeListClient:
    """Mirrors ``AnilistClient``'s public interface using the Jikan API v4.

    Every method has the **same signature** and returns the **same dataclass
    types** as its AniList counterpart, so callers can use this client as a
    transparent drop-in replacement.
    """

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._cf = None  # curl_cffi.AsyncSession — Chrome-impersonating transport
        self._last_request: float = 0.0

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    @property
    def _session(self):
        """The transport used for Jikan calls.

        Prefers ``curl_cffi`` with Chrome impersonation — Jikan's Cloudflare edge
        serves a synthetic 504 to Python's stock TLS fingerprint (httpx/urllib),
        so a plain httpx client fails *every* request. curl_cffi's browser-like
        handshake gets 200s. Falls back to httpx only if curl_cffi is missing.
        """
        if cf_requests is None:
            return self.http
        if self._cf is None:
            self._cf = cf_requests.AsyncSession(
                impersonate="chrome",
                timeout=30.0,
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
        return self._cf

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._cf is not None:
            # curl_cffi's AsyncSession.close() is an async coroutine in 0.15+.
            try:
                await self._cf.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._cf = None

    # ── rate limiter ──────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        """Enforce ~3 req/s gap between consecutive requests."""
        now = asyncio.get_event_loop().time()
        since_last = now - self._last_request
        if since_last < _RATE_LIMIT_GAP:
            await asyncio.sleep(_RATE_LIMIT_GAP - since_last)
        self._last_request = asyncio.get_event_loop().time()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, endpoint: str, params: dict | None = None) -> dict | None:
        """GET a Jikan endpoint with backoff on 429 / 5xx / transient errors.

        Jikan sits behind Cloudflare and routinely answers with 502/503/**504
        Gateway Time-out** under load — those are transient and clear on a short
        retry, so we treat them like a rate-limit rather than a hard failure.
        Up to 3 attempts with exponential backoff. Returns the parsed JSON
        ``data`` dict (without the wrapper key), or ``None`` on hard failure.

        Uses the curl_cffi (Chrome-TLS) session so Cloudflare doesn't 504 us on
        fingerprint; both curl_cffi and httpx responses expose the same
        ``.status_code`` / ``.headers`` / ``.json()`` surface, so the handling
        below is transport-agnostic. Network/transport exceptions are caught
        broadly (``Exception``) because curl_cffi raises its own error types,
        not ``httpx.*``.
        """
        url = f"{JIKAN_URL}/{endpoint.lstrip('/')}"
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            await self._throttle()
            last = attempt == max_attempts
            try:
                resp = await self._session.get(url, params=params)
                status = resp.status_code
                # Handle retryable status codes BEFORE parsing so a 504 gateway
                # timeout backs off instead of bailing after one try.
                if status == 429 and not last:
                    retry_after = float(resp.headers.get("Retry-After") or 2)
                    log.warning("jikan.ratelimit", retry_after=retry_after)
                    await asyncio.sleep(min(retry_after, 10.0))
                    continue
                if status in (500, 502, 503, 504) and not last:
                    backoff = 1.5 * attempt
                    log.warning("jikan.http_error", url=url,
                                status=status, retry_in=backoff)
                    await asyncio.sleep(backoff)
                    continue
                if status == 404:
                    return None  # entry not found — not an error
                if status >= 400:
                    log.warning("jikan.http_error.final", url=url, status=status)
                    return None
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001 - transport-agnostic (curl_cffi/httpx)
                log.warning("jikan.request.failed", url=url,
                            error=str(exc), attempt=attempt)
                if not last:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                return None
            return payload.get("data") or payload
        return None

    # ── search ────────────────────────────────────────────────────────────────

    async def _best_id(self, query: str) -> int | None:
        """Pick the best matching MAL ID from search results.

        Ranking: exact title match → fuzzy title match → popularity.
        """
        data = await self._get("anime", {"q": query, "limit": 10})
        results = (data or {}).get("data") if isinstance(data, dict) else []
        if not results:
            return None

        norm_query = query.strip().lower()

        def _rank(entry: dict) -> tuple:
            titles = _extract_titles(entry)
            exact = any(t.strip().lower() == norm_query for t in titles)
            from nekofetch.sources.telegram.matching import title_matches

            fuzzy = (
                max(
                    (1.0 if title_matches(query, t, threshold=0.85) else 0.0)
                    for t in titles
                )
                if titles
                else 0.0
            )
            return (1 if exact else 0, fuzzy, entry.get("members") or entry.get("popularity") or 0)

        ranked = sorted(results, key=_rank, reverse=True)
        return ranked[0].get("mal_id")

    async def search(self, query: str) -> AnilistMedia | None:
        """Resolve ``query`` to full ``AnilistMedia`` (MAL-backed)."""
        media_id = await self._best_id(query)
        if media_id is None:
            return None
        return await self._fetch_full(media_id)

    async def _fetch_full(self, media_id: int) -> AnilistMedia | None:
        """Fetch full data + relations for a MAL ID, return ``AnilistMedia``."""
        data = await self._get(f"anime/{media_id}/full")
        if not data or not isinstance(data, dict):
            return None
        return await self._parse_media(data)

    async def _parse_media(self, data: dict) -> AnilistMedia | None:
        """Parse Jikan full response into ``AnilistMedia``."""
        fmt_raw = data.get("type")
        fmt = _jikan_format(fmt_raw)

        titles = _extract_titles(data)
        english = data.get("title_english") or data.get("title") or ""
        # Prefer the first title as English-fallback; Jikan's "Default" is usually
        # the romanised title.
        romaji = data.get("title") or ""

        # Score: MAL is 0-10, already in our convention.
        score_raw = data.get("score")
        score = round(score_raw, 1) if score_raw is not None else None

        # Episodes
        episodes = data.get("episodes")

        # Status
        status = _jikan_status(data.get("status"))

        # Duration (string like "24 min per ep")
        duration_raw = data.get("duration") or ""
        import re

        duration_match = re.search(r"(\d+)", duration_raw)
        duration = int(duration_match.group(1)) if duration_match else None

        # Start date
        start_date = _parse_start_date(data.get("aired"))

        # Genres/studios
        genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
        studios = [s.get("name") for s in (data.get("studios") or []) if s.get("name")]
        studio_name = studios[0] if studios else None

        # Images
        images = (data.get("images") or {}).get("jpg") or {}
        cover_url = images.get("large_image_url") or images.get("image_url")
        # MAL has no banner images — leave None.

        # Relations
        relations: list[FranchiseRelation] = []
        for rel in data.get("relations") or []:
            rtype = _jikan_relation(rel.get("relation", ""))
            if not rtype:
                continue
            entries = rel.get("entry") or []
            for entry in entries:
                if entry.get("type") != "anime":
                    continue
                eid = entry.get("mal_id")
                if eid is None:
                    continue
                relations.append(
                    FranchiseRelation(
                        relation=rtype,
                        format=_jikan_format(entry.get("type")),
                        status=None,  # not available from relation entry
                        episodes=None,
                        titles=[entry.get("name") or ""],
                        anilist_id=eid,
                        cover_url=None,
                        banner_url=None,
                    )
                )

        mal_url = f"{MAL_SITE}/{data['mal_id']}"

        # Walk franchise totals for the full picture.
        try:
            totals = await self.franchise_totals(data["mal_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("mal.franchise_totals.failed", id=data["mal_id"], error=str(exc))
            totals = None

        if totals is not None:
            franchise_seasons = totals.seasons
            franchise_episodes = totals.episodes or None
            franchise_movies = totals.movies
            franchise_ovas = totals.ovas
            franchise_onas = totals.onas
            franchise_specials = totals.specials
        else:
            season_entries = [
                r
                for r in relations
                if r.format in _SERIES_FORMATS and r.relation in _CONTINUATION_RELATIONS
            ]
            franchise_seasons = 1 + len(season_entries)
            franchise_movies = sum(1 for r in relations if r.format == "MOVIE")
            franchise_ovas = sum(1 for r in relations if r.format == "OVA")
            franchise_onas = sum(1 for r in relations if r.format == "ONA")
            franchise_specials = sum(1 for r in relations if r.format == "SPECIAL")
            total_ep = episodes or 0
            for s in season_entries:
                if s.episodes is not None:
                    total_ep += s.episodes
            franchise_episodes = total_ep or None

        return AnilistMedia(
            id=data["mal_id"],
            format=fmt,
            season=data.get("season"),
            year=data.get("year"),
            start_date=start_date,
            episodes=episodes,
            duration=duration,
            status=status,
            score=score,
            popularity=data.get("popularity"),
            genres=genres,
            synopsis=data.get("synopsis"),
            studio=studio_name,
            cover_url=cover_url,
            banner_url=None,
            english=english,
            romaji=romaji,
            titles=titles,
            synonyms=_synonyms(data),
            relations=relations,
            anilist_url=mal_url,
            franchise_episodes=franchise_episodes,
            franchise_seasons=franchise_seasons,
            franchise_movies=franchise_movies,
            franchise_ovas=franchise_ovas,
            franchise_onas=franchise_onas,
            franchise_specials=franchise_specials,
        )

    # ── franchise walk ────────────────────────────────────────────────────────

    async def franchise_totals(self, root_id: int, *, max_nodes: int = 120) -> FranchiseTotals:
        """Walk the entire connected franchise graph and tally by format.

        BFS outward from ``root_id`` following SEQUEL / PREQUEL / SIDE_STORY /
        PARENT / SPIN_OFF / SUMMARY edges (mirrors AniList's ``_TRAVERSE_RELATIONS``).
        """
        visited: set[int] = {root_id}
        # id -> (format, episodes)
        nodes: dict[int, tuple[str | None, int | None]] = {}
        cont_adj: dict[int, set[int]] = {}  # continuity adjacency (SEQUEL/PREQUEL)

        frontier: list[int] = [root_id]

        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            if nid in nodes:
                continue
            data = await self._get(f"anime/{nid}/full")
            if not data or not isinstance(data, dict):
                continue

            fmt = _jikan_format(data.get("type"))
            status = _jikan_status(data.get("status"))
            eps = data.get("episodes")

            if nid != root_id and status in _EXCLUDED_STATUS:
                continue
            nodes[nid] = (fmt, eps)

            for rel in data.get("relations") or []:
                rtype = _jikan_relation(rel.get("relation", ""))
                if rtype not in _TRAVERSE_RELATIONS:
                    continue
                for entry in (rel.get("entry") or []):
                    eid = entry.get("mal_id")
                    if eid is None or eid in visited:
                        continue
                    if entry.get("type") != "anime":
                        continue
                    efmt = _jikan_format(entry.get("type"))
                    if efmt not in _ANIME_FORMATS:
                        continue
                    if rtype in _CONTINUATION_RELATIONS:
                        cont_adj.setdefault(nid, set()).add(eid)
                        cont_adj.setdefault(eid, set()).add(nid)
                    visited.add(eid)
                    frontier.append(eid)

        # Seasons: TV/TV_SHORT nodes reachable through continuity edges.
        season_ids: set[int] = set()
        stack, seen = [root_id], {root_id}
        while stack:
            cur = stack.pop()
            if nodes.get(cur, (None, None))[0] in _SERIES_FORMATS:
                season_ids.add(cur)
            for nb in cont_adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)

        totals = FranchiseTotals(nodes=len(nodes))
        for nid_val, (fmt_val, eps_val) in nodes.items():
            if fmt_val in _SERIES_FORMATS:
                if nid_val in season_ids:
                    totals.seasons += 1
                    totals.episodes += eps_val or 0
                else:
                    totals.spin_offs += 1
            elif fmt_val == "MOVIE":
                totals.movies += 1
            elif fmt_val == "OVA":
                totals.ovas += 1
            elif fmt_val == "ONA":
                totals.onas += 1
            elif fmt_val == "SPECIAL":
                totals.specials += 1

        if root_id not in season_ids:
            root_fmt, root_eps = nodes.get(root_id, (None, None))
            if root_fmt and root_fmt != "MOVIE":
                totals.episodes += root_eps or 0

        return totals

    async def walk_franchise_full(
        self, root_id: int, *, max_nodes: int = 120
    ) -> dict[int, FranchiseEntry]:
        """BFS-walk the entire franchise graph, return full entry data."""
        # 1. Fetch root.
        data = await self._get(f"anime/{root_id}/full")
        if not data or not isinstance(data, dict):
            return {}

        entries: dict[int, FranchiseEntry] = {}
        visited: set[int] = {root_id}
        relation_map: dict[int, str] = {}

        # Root entry.
        root_entry = self._to_franchise_entry(data, relation="ROOT")
        if root_entry is not None:
            entries[root_id] = root_entry

        # Seed frontier from root's immediate relations.
        frontier: list[int] = []
        for rel in data.get("relations") or []:
            rtype = _jikan_relation(rel.get("relation", ""))
            if rtype not in _CONTENT_WALK_RELS:
                continue
            for entry in (rel.get("entry") or []):
                eid = entry.get("mal_id")
                if eid is None or entry.get("type") != "anime" or eid in visited:
                    continue
                efmt = _jikan_format(entry.get("type"))
                if efmt not in _ANIME_FORMATS:
                    continue
                relation_map[eid] = rtype
                visited.add(eid)
                frontier.append(eid)

        # 2. BFS.
        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            data = await self._get(f"anime/{nid}/full")
            if not data or not isinstance(data, dict):
                continue

            entry = self._to_franchise_entry(data, relation=relation_map.get(nid, ""))
            if entry is not None:
                entries[nid] = entry

            # Discover deeper relations.
            for rel in data.get("relations") or []:
                rtype = _jikan_relation(rel.get("relation", ""))
                if rtype not in _CONTENT_WALK_RELS:
                    continue
                for entry_node in (rel.get("entry") or []):
                    eid = entry_node.get("mal_id")
                    if eid is None or entry_node.get("type") != "anime" or eid in visited:
                        continue
                    efmt = _jikan_format(entry_node.get("type"))
                    if efmt not in _ANIME_FORMATS:
                        continue
                    if eid not in relation_map:
                        relation_map[eid] = rtype
                    visited.add(eid)
                    frontier.append(eid)

        return entries

    def _to_franchise_entry(
        self, data: dict, relation: str = ""
    ) -> FranchiseEntry | None:
        """Parse a Jikan full response dict into a ``FranchiseEntry``."""
        mid = data.get("mal_id")
        if mid is None:
            return None
        fmt_raw = data.get("type")
        fmt = _jikan_format(fmt_raw)
        if fmt not in _ANIME_FORMATS:
            return None

        titles = _extract_titles(data)
        english_title = data.get("title_english") or data.get("title") or ""

        images = (data.get("images") or {}).get("jpg") or {}
        cover_url = images.get("large_image_url") or images.get("image_url")

        # Detect season part from title (mirrors AniList's _detect_part_from_title).
        part_num, _ = _detect_part_from_title(english_title)

        return FranchiseEntry(
            anilist_id=mid,
            format=fmt,
            english_title=english_title,
            titles=titles,
            banner_url=None,  # MAL has no banners
            cover_url=cover_url,
            episodes=data.get("episodes"),
            season_part=part_num,
            start_date=_parse_start_date(data.get("aired")),
            relation=relation,
            synopsis=data.get("synopsis"),
        )

    # ── title variants ────────────────────────────────────────────────────────

    async def title_variants(self, query: str) -> list[str]:
        """All titles to try on Telegram for ``query`` (self + relations)."""
        media = await self.search(query)
        if not media:
            return [query]
        variants = [query, *media.all_titles()]
        seen: set[str] = set()
        out: list[str] = []
        for t in variants:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    # ── episode titles ───────────────────────────────────────────────────────

    async def episode_titles(
        self, mal_id: int, *, max_pages: int = 5
    ) -> list[dict]:
        """Fetch episode list with titles from Jikan.

        Returns a list of ``{number: int, title: str}`` sorted by number.
        Jikan paginates at 100 per page; ``max_pages`` caps the fetch.
        """
        episodes: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self._get(
                f"anime/{mal_id}/episodes", {"page": page}
            )
            if not data:
                break
            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                break
            for ep in items:
                num = ep.get("mal_id") or ep.get("episode_id")
                title = (ep.get("title") or "").strip()
                if num is not None:
                    episodes.append({"number": int(num), "title": title})
            pagination = (
                data.get("pagination") if isinstance(data, dict) else None
            )
            if pagination and not pagination.get("has_next_page"):
                break
        episodes.sort(key=lambda e: e["number"])
        return episodes

    async def episode_titles_by_query(self, query: str) -> list[dict]:
        """Search for an anime by title and return its episode titles."""
        mal_id = await self._best_id(query)
        if mal_id is None:
            return []
        return await self.episode_titles(mal_id)
