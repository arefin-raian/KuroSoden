"""AniList metadata — franchise discovery, enrichment & relation resolution.

The single entry point for Phase 1 of the request flow: a user query is first
resolved through AniList to find the franchise, detect adaptations, and collect
full metadata (synopsis, genres, score, cover art, relation graph) before any
source plugin is touched.

Source plugins must never perform discovery searches — only AniList (with TMDB
as fallback) has that responsibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

try:  # Chrome-TLS transport — dodges Cloudflare's 403 wall on stock-Python TLS.
    from curl_cffi import requests as cf_requests
except ImportError:  # pragma: no cover - curl_cffi is a hard dep, but be safe
    cf_requests = None  # type: ignore[assignment]

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.matching import title_matches

log = get_logger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_SITE = "https://anilist.co/anime"
# Deterministic rendered info-card image for a media id (no API/userbot needed).
# Verified image/jpeg; works from just the AniList id, so it's available even
# when the GraphQL API is down as long as a dataset supplied the id.
ANILIST_CARD_IMAGE = "https://img.anili.st/media/{id}"


def anilist_card_image(anilist_id) -> str | None:
    """URL of AniList's rendered info-card image for ``anilist_id`` (or None)."""
    try:
        aid = int(str(anilist_id).strip())
    except (TypeError, ValueError):
        return None
    return ANILIST_CARD_IMAGE.format(id=aid) if aid > 0 else None

# Candidate search — AniList's SEARCH_MATCH can rank an obscure short above the
# real show (e.g. "Demon Slayer" → a TV_SHORT with a matching synonym), so we
# fetch several and pick by title-match then popularity ourselves.
_PAGE_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      popularity
      format
      title { romaji english native }
      synonyms
    }
  }
}
"""

# Full media query — fetches everything needed for a rich confirmation card.
_FULL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    format
    season
    seasonYear
    episodes
    duration
    status
    nextAiringEpisode { episode }
    averageScore
    popularity
    favourites
    description(asHtml: false)
    genres
    studios(isMain: true) { nodes { name } }
    coverImage { large extraLarge }
    bannerImage
    startDate { year month day }
    title { romaji english native }
    synonyms
    relations {
      edges {
        relationType
        node {
          id
          type
          format
          status
          episodes
          nextAiringEpisode { episode }
          title { romaji english native }
          coverImage { large }
          bannerImage
        }
      }
    }
  }
}
"""

# Formats to treat as "seasons" (watchable series entries) for breakdown counts.
_SERIES_FORMATS = {"TV", "TV_SHORT"}
# Real anime formats (excludes MANGA / NOVEL / ONE_SHOT source material).
_ANIME_FORMATS = {"TV", "TV_SHORT", "MOVIE", "OVA", "ONA", "SPECIAL"}
# Installments that don't exist yet (or never will) — never part of the franchise
# the user can actually get. We are not a manga distributor and don't list vapor.
_EXCLUDED_STATUS = {"NOT_YET_RELEASED", "CANCELLED"}


def _aired_episodes(media: dict) -> int | None:
    """Best episode count for an entry: the final total when AniList knows it,
    else the number already aired (``nextAiringEpisode - 1``) for a still-running
    show, else ``None``. Stops a currently-airing series rendering as "?"."""
    eps = media.get("episodes")
    if eps:
        return eps
    nxt = (media.get("nextAiringEpisode") or {}).get("episode")
    if nxt and nxt > 1:
        return nxt - 1
    return None
# Relation kinds that represent actual franchise *content* (watchable). Excludes
# ADAPTATION (the source manga/novel), CHARACTER (joke shorts), OTHER, SOURCE.
_CONTENT_RELATIONS = {
    "SEQUEL", "PREQUEL", "SIDE_STORY", "ALTERNATIVE", "SPIN_OFF",
    "PARENT", "SUMMARY",
}
# Relations that continue the same continuity (collapse into "seasons").
_CONTINUATION_RELATIONS = {"SEQUEL", "PREQUEL"}
# Relations to follow when walking the franchise graph. ALTERNATIVE is excluded
# on purpose: it links *different adaptations* (Hellsing TV vs Hellsing Ultimate,
# Fate adaptations), which are separate versions, not part of one franchise total.
_TRAVERSE_RELATIONS = {
    "SEQUEL", "PREQUEL", "SIDE_STORY", "PARENT", "SPIN_OFF", "SUMMARY",
}

# Lightweight query to walk the relation graph: for a batch of ids, return each
# node's format/episodes plus its immediate edges (so BFS can expand outward).
_GRAPH_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      format
      status
      episodes
      nextAiringEpisode { episode }
      relations {
        edges {
          relationType
          node { id type format status episodes }
        }
      }
    }
  }
}
"""

# Full-data batch query — fetches complete entry metadata (titles, banners,
# descriptions, start dates, relations) for up to 50 IDs at once. Used by
# ``walk_franchise_full`` to return rich ``FranchiseEntry`` objects for
# content generation (season cards, extra cards, watch guide).
_GRAPH_FULL_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      format
      episodes
      duration
      status
      averageScore
      bannerImage
      description(asHtml: false)
      title { romaji english native }
      coverImage { large extraLarge }
      startDate { year month day }
      relations {
        edges {
          relationType
          node { id format type status episodes }
        }
      }
    }
  }
}
"""

# Relations to follow when walking the franchise for content cards. Same as
# _TRAVERSE_RELATIONS but scoped to canonical continuity — deliberately excludes
# SPIN_OFF (Chibi Theatre, Junior High) and ALTERNATIVE (different adaptations)
# and SUMMARY (recap/compilation movies).
_CONTENT_WALK_RELS = {"SEQUEL", "PREQUEL", "SIDE_STORY", "PARENT"}


@dataclass
class FranchiseTotals:
    """Aggregated counts across the *entire* connected franchise graph.

    A node is a **season** ONLY if it is a TV/TV_SHORT entry sitting in the root's
    SEQUEL/PREQUEL *continuity chain*. A TV series reached by any other relation
    (SPIN_OFF, SIDE_STORY, …) is a **spin-off**, never a season. Movies, OVAs,
    ONAs and specials are classified purely by format.
    """
    seasons: int = 0
    movies: int = 0
    ovas: int = 0
    onas: int = 0
    specials: int = 0
    spin_offs: int = 0     # TV/TV_SHORT NOT in the main continuity chain
    episodes: int = 0      # summed across season (TV/TV_SHORT) entries
    nodes: int = 0         # total installments discovered


@dataclass
class FranchiseRelation:
    """A single relation edge in the franchise graph."""
    relation: str                 # e.g. "SEQUEL", "PREQUEL", "SIDE_STORY"
    format: str | None
    status: str | None
    episodes: int | None
    titles: list[str] = field(default_factory=list)
    anilist_id: int | None = None
    cover_url: str | None = None
    banner_url: str | None = None


@dataclass
class FranchiseEntry:
    """A single node in the full franchise graph with complete AniList data.

    Used by ``AnilistClient.walk_franchise_full`` to return a flat dict of
    every installment reachable from a root media — each entry carries enough
    data to build a season/extra card without a second API call."""
    anilist_id: int
    format: str
    english_title: str
    titles: list[str] = field(default_factory=list)
    banner_url: str | None = None
    cover_url: str | None = None
    episodes: int | None = None
    duration: int | None = None      # minutes per episode (drives movie/OVA runtime)
    season_part: int | None = None   # detected part number (e.g. 1 for "Season 3 Part 1")
    start_date: dict | None = None  # {"year": 2013, "month": 4, "day": 7}
    relation: str = ""              # how this entry connects to its parent
    synopsis: str | None = None
    score: float | None = None      # averageScore / 10 (per-entry AniList rating)


@dataclass
class AnilistMedia:
    """Full media data from AniList — used for the search→confirm flow.

    ``all_titles()`` and ``related_by_kind()`` from the previous model
    remain available; new fields support the rich confirmation card.
    """
    id: int
    format: str | None
    season: str | None
    year: int | None
    episodes: int | None
    duration: int | None          # minutes per episode
    status: str | None            # FINISHED, RELEASING, NOT_YET_RELEASED, CANCELLED
    score: float | None           # averageScore / 10
    popularity: int | None
    start_date: dict | None = None  # {"year": 2013, "month": 4, "day": 7}
    genres: list[str] = field(default_factory=list)
    synopsis: str | None = None
    studio: str | None = None
    cover_url: str | None = None  # large cover image
    banner_url: str | None = None
    english: str | None = None    # preferred display title
    romaji: str | None = None     # transliterated original
    titles: list[str] = field(default_factory=list)        # english/romaji/native
    synonyms: list[str] = field(default_factory=list)
    relations: list[FranchiseRelation] = field(default_factory=list)
    anilist_url: str | None = None

    # ── derived breakdown ──
    franchise_episodes: int | None = None   # total across all series entries
    franchise_seasons: int = 0              # number of TV/TV_SHORT entries
    franchise_movies: int = 0               # number of MOVIE entries
    franchise_ovas: int = 0                 # number of OVA entries
    franchise_onas: int = 0                 # number of ONA entries
    franchise_specials: int = 0             # number of SPECIAL entries

    def all_titles(self) -> list[str]:
        """Every title we can match against, including related entries' titles."""
        out: list[str] = list(self.titles) + list(self.synonyms)
        for rel in self.relations:
            out += rel.titles
        seen: set[str] = set()
        uniq = []
        for t in out:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        return uniq

    def related_by_kind(self, kinds: tuple[str, ...]) -> list[FranchiseRelation]:
        return [r for r in self.relations if r.relation in kinds]

    def series_relations(self) -> list[FranchiseRelation]:
        """Relations that are themselves series (sequels/prequels to watch)."""
        return [r for r in self.relations
                if r.format in _SERIES_FORMATS
                and r.relation in ("SEQUEL", "PREQUEL", "ALTERNATIVE", "SIDE_STORY")]

    def non_series_relations(self) -> list[FranchiseRelation]:
        """Movies, OVAs, specials, spin-offs."""
        return [r for r in self.relations if r.format not in _SERIES_FORMATS]


def _detect_part_from_title(title: str) -> tuple[int | None, bool]:
    """Detect season part from an AniList entry title.

    Looks for "Part X", "Pt X", "Cour X", or parenthesised numbers.
    Returns ``(part_number, auto_detected)``."""
    import re
    patterns = [
        re.compile(r"\b(?:part|pt)\.?\s*(\d+)\b", re.IGNORECASE),
        re.compile(r"\bcour\s*(\d+)\b", re.IGNORECASE),
        re.compile(r"\((\d+)\)"),
    ]
    for pat in patterns:
        m = pat.search(title)
        if m:
            return int(m.group(1)), True
    return None, False


class AnilistClient:
    # AniList's documented limit is ~90 req/min (degraded to 30/min at times).
    # We serialize every request through one lock and hold a minimum gap so a
    # single confirm flow (best_id + full + BFS walk) can never self-burst into
    # a 429. ~1.4 req/s ≈ 40/min — comfortably under even the degraded ceiling.
    _MIN_GAP = 0.7

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._cf = None  # curl_cffi.AsyncSession — Chrome-impersonating transport
        # Serialize requests: AniList rate-limits per-IP, and concurrent POSTs
        # from one franchise walk are what tripped the 429 storm.
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        # Adaptive throttle: AniList advertises the live budget via
        # ``X-RateLimit-Remaining``. When it runs low we stretch the gap so we
        # glide under whichever ceiling is active (90/min normal, 30/min when
        # degraded) instead of blindly hammering into a 429.
        self._remaining: int | None = None
        # Cache resolved franchise totals per root id for this process — the
        # confirm flow otherwise walks the SAME graph twice (_parse_media then
        # apply_franchise_totals), doubling every BFS burst for no new data.
        self._totals_cache: dict[int, "FranchiseTotals"] = {}

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=20.0, headers={"Accept": "application/json"}
            )
        return self._http

    @property
    def _session(self):
        """Transport for GraphQL POSTs.

        Prefers ``curl_cffi`` with Chrome impersonation: AniList sits behind
        Cloudflare, which periodically serves a **403 "temporarily disabled"** to
        stock-Python TLS fingerprints (the recurring "AniList is down" the owner
        hit) while a browser-like handshake still gets 200s. Falls back to plain
        httpx only when curl_cffi is unavailable. Both expose the same
        ``.status_code`` / ``.headers`` / ``.json()`` surface, so ``_post`` is
        transport-agnostic.
        """
        if cf_requests is None:
            return self.http
        if self._cf is None:
            self._cf = cf_requests.AsyncSession(
                impersonate="chrome",
                timeout=25.0,
                headers={"Accept": "application/json"},
            )
        return self._cf

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._cf is not None:
            try:
                await self._cf.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._cf = None

    async def _throttle(self) -> None:
        """Hold a minimum gap between consecutive requests (called under lock).

        The gap widens when AniList reports a low remaining budget: at/under 5
        requests left we back off to ~2s between calls (≈30/min, the degraded
        ceiling) so the window refills instead of tipping into a 429.
        """
        gap = self._MIN_GAP
        if self._remaining is not None and self._remaining <= 5:
            gap = max(gap, 2.0)
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        self._last_request = asyncio.get_event_loop().time()

    async def _post(self, query: str, variables: dict) -> dict | None:
        """POST a GraphQL query, serialized + throttled, with backoff on 429/5xx.

        AniList enforces ~90 req/min (sometimes throttled to 30) and answers with
        429 + a ``Retry-After`` header (seconds) or an occasional 5xx. Every call
        goes through a single lock with a minimum inter-request gap so one confirm
        flow can't self-burst; on 429 we honour the FULL ``Retry-After`` (AniList
        routinely asks for 30-60s — capping it at 10s guaranteed the retry failed
        too). Returns the parsed ``data`` object, or ``None`` on hard failure.
        """
        # Up to 3 attempts: initial + two retries. AniList's Retry-After is
        # authoritative, so honouring it is what actually clears the limit.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._lock:
                    await self._throttle()
                    resp = await self._session.post(
                        ANILIST_URL, json={"query": query, "variables": variables}
                    )
                # Track the remaining budget AniList reports so the next call's
                # _throttle can pre-emptively widen the gap before we hit 0.
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining is not None:
                    try:
                        self._remaining = int(remaining)
                    except ValueError:
                        self._remaining = None
                if resp.status_code == 429 and attempt < max_attempts:
                    # A 429 means the window is spent; force the slow gap next call.
                    self._remaining = 0
                    # Honour the full window (+1s slack). Header is in seconds;
                    # X-RateLimit-Reset (epoch) is the fallback when it's absent.
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) + 1.0 if retry_after is not None else 5.0
                    log.warning("anilist.retry", status=429, retry_after=wait)
                    await asyncio.sleep(min(wait, 65.0))
                    continue
                if resp.status_code in (500, 502, 503, 504) and attempt < max_attempts:
                    backoff = 2.0 * attempt
                    log.warning("anilist.retry", status=resp.status_code,
                                retry_after=backoff)
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("anilist.request.failed", error=str(exc))
                if attempt < max_attempts:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                return None
            except Exception as exc:  # noqa: BLE001 — curl_cffi raises its own types
                log.warning("anilist.request.failed", error=str(exc)[:200])
                if attempt < max_attempts:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                return None
            if payload.get("errors"):
                log.warning("anilist.graphql.errors", errors=payload["errors"])
            return payload.get("data")
        return None

    async def _best_id(self, query: str) -> int | None:
        """Pick the best candidate id.

        Ranking, strongest first: an exact (case-insensitive) title equality with
        the query, then fuzzy title-match strength, then popularity. The exact tier
        is essential — searching "Hellsing" must return the TV series even though
        the OVA ("Hellsing Ultimate") is far more popular.
        """
        data = await self._post(_PAGE_QUERY, {"search": query})
        media = (data or {}).get("Page", {}).get("media", [])
        if not media:
            return None

        norm_query = query.strip().lower()

        def primary_titles(m: dict) -> list[str]:
            t = m.get("title", {})
            return [x for x in (t.get("romaji"), t.get("english"), t.get("native")) if x]

        def all_titles(m: dict) -> list[str]:
            return primary_titles(m) + list(m.get("synonyms") or [])

        def rank(m: dict) -> tuple[int, float, int]:
            # Exact equality is checked on PRIMARY titles only: an obscure show
            # ("Onigiri") may carry the query as a fan-synonym ("Demon Slayer"),
            # which must not outrank the popular real match.
            exact = any(t.strip().lower() == norm_query for t in primary_titles(m))
            titles = all_titles(m)
            fuzzy = max((1.0 if title_matches(query, t, threshold=0.85) else 0.0)
                        for t in titles) if titles else 0.0
            return (1 if exact else 0, fuzzy, m.get("popularity") or 0)

        # Keep fuzzy matches when any exist, else fall back to the whole page.
        def matches(m: dict) -> bool:
            return any(title_matches(query, t, threshold=0.85) for t in all_titles(m))

        ranked = [m for m in media if matches(m)] or media
        ranked.sort(key=rank, reverse=True)
        return ranked[0].get("id")

    async def search_candidates(self, query: str, *, limit: int = 25) -> list[dict]:
        """Return up to ``limit`` search-page candidates as lightweight dicts
        ``{id, title, format, popularity}``, matching-title first then by
        popularity.

        Unlike :meth:`search` (which resolves the single best match), this exposes
        the whole page so a caller can group the hits into distinct *franchises*
        for a picker (the batch flow's "which franchise did you mean?" menu).
        """
        data = await self._post(_PAGE_QUERY, {"search": query})
        media = (data or {}).get("Page", {}).get("media", [])
        if not media:
            return []

        norm_query = query.strip().lower()

        def primary_titles(m: dict) -> list[str]:
            t = m.get("title", {})
            return [x for x in (t.get("romaji"), t.get("english"), t.get("native")) if x]

        def all_titles(m: dict) -> list[str]:
            return primary_titles(m) + list(m.get("synonyms") or [])

        def rank(m: dict) -> tuple[int, float, int]:
            exact = any(t.strip().lower() == norm_query for t in primary_titles(m))
            titles = all_titles(m)
            fuzzy = max((1.0 if title_matches(query, t, threshold=0.85) else 0.0)
                        for t in titles) if titles else 0.0
            return (1 if exact else 0, fuzzy, m.get("popularity") or 0)

        def matches(m: dict) -> bool:
            return any(title_matches(query, t, threshold=0.85) for t in all_titles(m))

        ranked = [m for m in media if matches(m)] or media
        ranked.sort(key=rank, reverse=True)

        out: list[dict] = []
        for m in ranked[:limit]:
            t = m.get("title", {})
            title = t.get("english") or t.get("romaji") or t.get("native") or query
            out.append({
                "id": m.get("id"),
                "title": title,
                "format": m.get("format"),
                "popularity": m.get("popularity") or 0,
            })
        return out

    async def search(self, query: str) -> AnilistMedia | None:
        """Resolve ``query`` to a full AnilistMedia with relation breakdown.

        This is the sole discovery entry point — source plugins must not
        perform name searches.
        """
        media_id = await self._best_id(query)
        if media_id is None:
            return None
        return await self._fetch_full(media_id)

    async def _fetch_full(self, media_id: int) -> AnilistMedia | None:
        """Fetch full media data + full franchise breakdown for the given AniList id.

        ``_parse_media`` is now async because it also kicks off a BFS
        ``franchise_totals`` walk so the AniListMedia.franchise_* fields reflect
        every installment reachable from the root, not just the immediate
        SEQUEL children (e.g. Attack on Titan reports 7 seasons, not 2).
        """
        data = await self._post(_FULL_QUERY, {"id": media_id})
        media = (data or {}).get("Media")
        if not media:
            return None
        return await self._parse_media(media)

    async def franchise_totals(self, root_id: int, *, max_nodes: int = 120) -> FranchiseTotals:
        """Walk the *whole* connected franchise graph from ``root_id`` and tally
        every installment by format.

        AniList only returns a node's immediate relations, so the breakdown on a
        single entry misses later seasons/cours/movies. We BFS outward, following
        only canonical-continuity edges (SEQUEL / PREQUEL / SIDE_STORY / PARENT —
        the same ``_CONTENT_WALK_RELS`` set the preview/distribution walk uses).
        SUMMARY (recap/compilation movies), SPIN_OFF, and ALTERNATIVE (a different
        adaptation) are deliberately excluded so the counts match the "perfect"
        franchise map, expanding a level per request via ``id_in`` batching.

        Result is memoized per root id for the process lifetime: the confirm flow
        walks the same graph twice (``_parse_media`` then ``apply_franchise_totals``),
        and re-walking just doubles the API burst for identical output.
        """
        cached = self._totals_cache.get(root_id)
        if cached is not None:
            return cached

        visited: set[int] = {root_id}
        frontier: list[int] = [root_id]
        # id -> (format, episodes) for every node we actually resolve
        nodes: dict[int, tuple[str | None, int | None]] = {}
        # Continuity adjacency from SEQUEL/PREQUEL edges only — this is what
        # defines "seasons". Spin-offs/side-stories are deliberately NOT in here.
        cont_adj: dict[int, set[int]] = {}

        while frontier and len(visited) <= max_nodes:
            batch = frontier[:50]
            frontier = frontier[50:]
            data = await self._post(_GRAPH_QUERY, {"ids": batch})
            medias = (data or {}).get("Page", {}).get("media", [])
            if not medias:
                continue
            for m in medias:
                mid = m.get("id")
                if mid is None:
                    continue
                # Skip not-yet-released / cancelled installments entirely (but never
                # the root the user actually asked about). ``continue`` also skips
                # their edges, so we don't expand vapor branches into the totals.
                if mid != root_id and m.get("status") in _EXCLUDED_STATUS:
                    continue
                nodes[mid] = (m.get("format"), _aired_episodes(m))
                for edge in m.get("relations", {}).get("edges", []):
                    rtype = edge.get("relationType")
                    # Canonical continuity ONLY (same set the preview/distribution
                    # walk uses): SEQUEL / PREQUEL / SIDE_STORY / PARENT. This
                    # deliberately EXCLUDES SUMMARY (recap/compilation movies) and
                    # SPIN_OFF, so the confirm-card counts match the "perfect"
                    # franchise map the preview bot produces — no phantom recap
                    # movies inflating the total.
                    if rtype not in _CONTENT_WALK_RELS:
                        continue
                    node = edge.get("node") or {}
                    nid = node.get("id")
                    if not (node.get("type") == "ANIME"
                            and node.get("format") in _ANIME_FORMATS
                            and node.get("status") not in _EXCLUDED_STATUS
                            and nid is not None):
                        continue
                    if rtype in _CONTINUATION_RELATIONS:  # SEQUEL / PREQUEL
                        cont_adj.setdefault(mid, set()).add(nid)
                        cont_adj.setdefault(nid, set()).add(mid)
                    if nid not in visited:
                        visited.add(nid)
                        frontier.append(nid)

        # Seasons = TV/TV_SHORT nodes reachable from the root through continuity
        # edges ONLY. Walk that component; spin-offs hang off non-continuity edges
        # and therefore never enter it.
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
        for nid, (fmt, eps) in nodes.items():
            if fmt in _SERIES_FORMATS:
                if nid in season_ids:
                    totals.seasons += 1
                    totals.episodes += eps or 0
                else:                       # a TV series off the main line = spin-off
                    totals.spin_offs += 1
            elif fmt == "MOVIE":
                totals.movies += 1
            elif fmt == "OVA":
                totals.ovas += 1
            elif fmt == "ONA":
                totals.onas += 1
            elif fmt == "SPECIAL":
                totals.specials += 1
        # When the root isn't a TV season (it's an ONA/OVA/Special with its own
        # episode count), THAT count is the title's episode count — otherwise an
        # ONA-only entry like a 6-episode ONA would report 0 episodes. Spin-off and
        # side-story episode counts are still deliberately excluded.
        if root_id not in season_ids:
            root_fmt, root_eps = nodes.get(root_id, (None, None))
            if root_fmt and root_fmt != "MOVIE":
                totals.episodes += root_eps or 0
        self._totals_cache[root_id] = totals
        return totals

    async def walk_franchise_full(self, root_id: int, *, max_nodes: int = 120) -> dict[int, FranchiseEntry]:
        """BFS-walk the ENTIRE franchise graph from ``root_id``, returning full
        ``FranchiseEntry`` data for every reachable installment.

        AniList only returns a node's immediate relations, so the breakdown on a
        single entry misses later seasons/cours/movies. This walker discovers every
        installment reachable through SEQUEL / PREQUEL / SIDE_STORY / PARENT edges
        (deliberately excluding SPIN_OFF, ALTERNATIVE, and SUMMARY) and returns a
        flat ``dict`` keyed by AniList ID with complete metadata per entry.

        The caller (``BotContentService.generate_posts``) uses the returned dict to
        build per-entry season/extra cards and a unified release-order watch guide.
        """
        # ── 1. Seed: fetch the root with full data ──
        data = await self._post(_FULL_QUERY, {"id": root_id})
        root_media = (data or {}).get("Media")
        if not root_media:
            return {}

        entries: dict[int, FranchiseEntry] = {}
        visited: set[int] = {root_id}
        relation_map: dict[int, str] = {}

        # Root entry from _FULL_QUERY data (already has startDate + description).
        title_dict = root_media.get("title", {})
        root_titles = [t for t in (
            title_dict.get("english"), title_dict.get("romaji"),
            title_dict.get("native"),
        ) if t]
        root_title = title_dict.get("english") or title_dict.get("romaji") or ""
        root_part_num, _ = _detect_part_from_title(root_title)
        entries[root_id] = FranchiseEntry(
            anilist_id=root_id,
            format=root_media.get("format") or "TV",
            english_title=root_title,
            titles=root_titles,
            banner_url=root_media.get("bannerImage"),
            cover_url=(root_media.get("coverImage") or {}).get("extraLarge")
                       or (root_media.get("coverImage") or {}).get("large"),
            episodes=_aired_episodes(root_media),
            duration=root_media.get("duration"),
            season_part=root_part_num,
            start_date=root_media.get("startDate"),
            relation="ROOT",
            synopsis=root_media.get("description"),
            score=(root_media.get("averageScore") / 10
                   if root_media.get("averageScore") is not None else None),
        )

        # Seed frontier from root's immediate relations.
        frontier: set[int] = set()
        for edge in root_media.get("relations", {}).get("edges", []):
            rtype = edge.get("relationType", "")
            node = edge.get("node") or {}
            nid = node.get("id")
            if not (
                rtype in _CONTENT_WALK_RELS
                and node.get("type") == "ANIME"
                and node.get("format") in _ANIME_FORMATS
                and node.get("status") not in _EXCLUDED_STATUS
                and nid is not None
            ):
                continue
            relation_map[nid] = rtype
            frontier.add(nid)

        frontier_list = list(frontier - visited)

        # ── 2. BFS: batch-fetch full data, discover deeper relations ──
        while frontier_list and len(visited) <= max_nodes:
            batch = frontier_list[:50]
            frontier_list = frontier_list[50:]

            data = await self._post(_GRAPH_FULL_QUERY, {"ids": batch})
            for m in (data or {}).get("Page", {}).get("media", []):
                mid = m.get("id")
                if mid is None:
                    continue
                fmt = m.get("format")
                status = m.get("status")
                if fmt not in _ANIME_FORMATS or status in _EXCLUDED_STATUS:
                    continue

                title_dict = m.get("title", {})
                titles_list = [t for t in (
                    title_dict.get("english"), title_dict.get("romaji"),
                    title_dict.get("native"),
                ) if t]

                # Detect season part from the entry's English title
                entry_title = title_dict.get("english") or title_dict.get("romaji") or ""
                part_num, _ = _detect_part_from_title(entry_title)

                entries[mid] = FranchiseEntry(
                    anilist_id=mid,
                    format=fmt,
                    english_title=entry_title,
                    titles=titles_list,
                    banner_url=m.get("bannerImage"),
                    cover_url=(m.get("coverImage") or {}).get("extraLarge")
                               or (m.get("coverImage") or {}).get("large"),
                    episodes=m.get("episodes"),
                    duration=m.get("duration"),
                    season_part=part_num,
                    start_date=m.get("startDate"),
                    relation=relation_map.get(mid, ""),
                    synopsis=m.get("description"),
                    score=(m.get("averageScore") / 10
                           if m.get("averageScore") is not None else None),
                )
                visited.add(mid)

                # Discover deeper relations from this entry's edges.
                for edge in m.get("relations", {}).get("edges", []):
                    rtype = edge.get("relationType", "")
                    node = edge.get("node") or {}
                    nid, nfmt, nstatus = node.get("id"), node.get("format"), node.get("status")
                    if not (
                        rtype in _CONTENT_WALK_RELS
                        and node.get("type") == "ANIME"
                        and nfmt in _ANIME_FORMATS
                        and nstatus not in _EXCLUDED_STATUS
                        and nid is not None
                    ):
                        continue
                    if nid not in visited:
                        if nid not in relation_map:
                            relation_map[nid] = rtype
                        if nid not in frontier_list:
                            frontier_list.append(nid)

        return entries

    async def _parse_media(self, media: dict) -> AnilistMedia:
        """Parse the raw GraphQL response into AnilistMedia with the FULL franchise
        breakdown.

        The immediate-relation counts (``1 + len(continuations)``) only see the
        root's DIRECT sequels. We also call ``franchise_totals`` to BFS-walk
        SEQUEL / PREQUEL / SIDE_STORY / PARENT / SPIN_OFF / SUMMARY so the
        resulting ``AnilistMedia.franchise_*`` fields reflect every installment
        reachable from the root — Attack on Titan reports 7 seasons + all
        subsequent course / final-season entries, not just 2. Falls back to
        the immediate-relation numbers on walker failure so a transient AniList
        hiccup never blocks the caller.

        Titles are ordered **English-first** (then romaji, then native) so the
        first element is the best display title — AniList stores e.g. the Hellsing
        OVA's romaji as "HELLSING OVA" but its English as "Hellsing Ultimate".
        """
        def titles_of(t: dict) -> list[str]:
            return [t.get("english"), t.get("romaji"), t.get("native")]

        title_dict = media.get("title", {})
        english = title_dict.get("english")
        romaji = title_dict.get("romaji")
        titles = [t for t in titles_of(title_dict) if t]

        relations = []
        for edge in media.get("relations", {}).get("edges", []):
            node = edge.get("node", {})
            fmt = node.get("format")
            status = node.get("status")
            # Only real, released anime installments belong in the franchise — no
            # manga/novel source material, no not-yet-released or cancelled entries.
            if fmt not in _ANIME_FORMATS or status in _EXCLUDED_STATUS:
                continue
            relations.append(FranchiseRelation(
                relation=edge.get("relationType", ""),
                format=fmt,
                status=status,
                episodes=_aired_episodes(node),
                titles=[t for t in titles_of(node.get("title", {})) if t],
                anilist_id=node.get("id"),
                cover_url=node.get("coverImage", {}).get("large"),
                banner_url=node.get("bannerImage"),
            ))

        studios = media.get("studios", {}).get("nodes", [])
        studio_name = studios[0]["name"] if studios else None

        cover = media.get("coverImage", {})
        cover_url = cover.get("extraLarge") or cover.get("large")

        score = media.get("averageScore")
        if score is not None:
            score = round(score / 10, 1)

        # Walk the FULL sequel/prequel chain via BFS. The immediate-relation
        # counts above would otherwise miss later installments (AoT S3, Final,
        # Final 2, Final Chapters, …). On walker failure we silently fall back
        # to the immediate-relation numbers so a transient AniList hiccup
        # never blocks a user's request flow.
        try:
            totals = await self.franchise_totals(media["id"])
        except Exception as exc:  # noqa: BLE001 - soft pass, never break the caller
            log.warning("anilist.franchise_totals.failed",
                        id=media["id"], error=str(exc))
            totals = None

        if totals is not None:
            franchise_seasons = totals.seasons
            franchise_episodes = totals.episodes or None
            franchise_movies = totals.movies
            franchise_ovas = totals.ovas
            franchise_onas = totals.onas
            franchise_specials = totals.specials
        else:
            # Fallback: derive from the root's immediate relations only.
            content = [r for r in relations
                       if r.relation in _CONTENT_RELATIONS
                       and r.format in _ANIME_FORMATS]
            season_entries = [r for r in content
                              if r.format in _SERIES_FORMATS
                              and r.relation in _CONTINUATION_RELATIONS]
            franchise_seasons = 1 + len(season_entries)
            franchise_movies = sum(1 for r in content if r.format == "MOVIE")
            franchise_ovas = sum(1 for r in content if r.format == "OVA")
            franchise_onas = sum(1 for r in content if r.format == "ONA")
            franchise_specials = sum(1 for r in content if r.format == "SPECIAL")
            total_ep = _aired_episodes(media) or 0
            for s in season_entries:
                if s.episodes is not None:
                    total_ep += s.episodes
            franchise_episodes = total_ep or None

        anilist_url = f"{ANILIST_SITE}/{media['id']}"

        return AnilistMedia(
            id=media["id"],
            format=media.get("format"),
            season=media.get("season"),
            year=media.get("seasonYear"),
            start_date=media.get("startDate"),
            episodes=_aired_episodes(media),
            duration=media.get("duration"),
            status=media.get("status"),
            score=score,
            popularity=media.get("popularity"),
            genres=[g for g in media.get("genres", []) if g],
            synopsis=media.get("description"),
            studio=studio_name,
            cover_url=cover_url,
            banner_url=media.get("bannerImage"),
            english=english,
            romaji=romaji,
            titles=titles,
            synonyms=[s for s in media.get("synonyms", []) if s],
            relations=relations,
            anilist_url=anilist_url,
            franchise_episodes=franchise_episodes,
            franchise_seasons=franchise_seasons,
            franchise_movies=franchise_movies,
            franchise_ovas=franchise_ovas,
            franchise_onas=franchise_onas,
            franchise_specials=franchise_specials,
        )

    async def title_variants(self, query: str) -> list[str]:
        """All titles to try on Telegram for ``query`` (self + relations)."""
        media = await self.search(query)
        if not media:
            return [query]
        variants = [query, *media.all_titles()]
        seen: set[str] = set()
        out = []
        for t in variants:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out
