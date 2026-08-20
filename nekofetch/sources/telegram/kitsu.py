"""Kitsu metadata — franchise discovery, enrichment & relation resolution.

A fallback tier for when AniList is down (and after the local datasets miss).
Mirrors the :class:`AnilistClient` interface (``search``, ``search_candidates``,
``_fetch_full``, ``franchise_totals``, ``walk_franchise_full``, ``title_variants``,
``close``) by talking to Kitsu's public JSON:API (https://kitsu.io/api/edge).

All public methods return the **same dataclass types** as ``AnilistClient``
(``AnilistMedia``, ``FranchiseEntry``, ``FranchiseTotals``) so the resilient
client can chain to it transparently. Field names follow AniList's conventions;
the Kitsu-specific mappings (subtype→format, role→relation, 0-100 rating→0-10)
are handled internally.

Unlike Jikan, Kitsu's search response already carries full attributes, so a
title lookup needs no second by-id round trip. Relations are NOT inlined on a
resource, so the franchise walk fans out over the ``media-relationships``
endpoint (``filter[source_id]`` + ``include=destination``), following the same
SEQUEL / PREQUEL / SIDE_STORY / PARENT edges AniList/MAL do.

Rate limits
-----------
Kitsu has no hard published quota; we keep a ~300 ms gap between requests to
stay polite and honour ``Retry-After`` on 429 with backoff.
"""

from __future__ import annotations

import asyncio

import httpx

try:  # Chrome-TLS transport — same posture as the Jikan client.
    from curl_cffi import requests as cf_requests
except ImportError:  # pragma: no cover - curl_cffi is a hard dep, but be safe
    cf_requests = None  # type: ignore[assignment]

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.anilist import (
    AnilistMedia,
    FranchiseEntry,
    FranchiseRelation,
    FranchiseTotals,
    _ANIME_FORMATS,
    _CONTENT_WALK_RELS,
    _CONTINUATION_RELATIONS,
    _is_released,
    _SERIES_FORMATS,
)

log = get_logger(__name__)

KITSU_URL = "https://kitsu.io/api/edge"
KITSU_SITE = "https://kitsu.io/anime"
_JSONAPI_HEADERS = {"Accept": "application/vnd.api+json"}

# ── format / status / relation mapping ────────────────────────────────────────

# Kitsu ``subtype`` values (mixed case) → AniList format vocabulary.
_KITSU_FORMAT: dict[str, str] = {
    "tv": "TV",
    "movie": "MOVIE",
    "ova": "OVA",
    "ona": "ONA",
    "special": "SPECIAL",
    "music": "MUSIC",
}

# Kitsu ``status`` → AniList status vocabulary.
_KITSU_STATUS: dict[str, str] = {
    "current": "RELEASING",
    "finished": "FINISHED",
    "tba": "NOT_YET_RELEASED",
    "upcoming": "NOT_YET_RELEASED",
    "unreleased": "NOT_YET_RELEASED",
}

# Kitsu media-relationship ``role`` → AniList relation vocabulary.
_KITSU_RELATION: dict[str, str] = {
    "sequel": "SEQUEL",
    "prequel": "PREQUEL",
    "side_story": "SIDE_STORY",
    "parent_story": "PARENT",
    "spinoff": "SPIN_OFF",
    "summary": "SUMMARY",
    "full_story": "PARENT",
    "alternative_setting": "ALTERNATIVE",
    "alternative_version": "ALTERNATIVE",
    "adaptation": "ADAPTATION",
    "character": "CHARACTER",
    "other": "OTHER",
}

_RATE_LIMIT_GAP = 0.3  # seconds between requests


def _kitsu_format(subtype: str | None) -> str | None:
    if not subtype:
        return None
    mapped = _KITSU_FORMAT.get(subtype.strip().lower())
    return mapped if mapped in _ANIME_FORMATS else None


def _kitsu_status(status: str | None) -> str | None:
    if not status:
        return None
    return _KITSU_STATUS.get(status.strip().lower())


def _kitsu_relation(role: str | None) -> str | None:
    if not role:
        return None
    return _KITSU_RELATION.get(role.strip().lower())


def _score_from_rating(raw) -> float | None:
    """Kitsu ``averageRating`` is a 0-100 string; our convention is 0-10."""
    if raw in (None, ""):
        return None
    try:
        return round(float(raw) / 10, 1)
    except (TypeError, ValueError):
        return None


def _year_from(date_str: str | None) -> int | None:
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return None
    return int(date_str[:4])


def _start_date(date_str: str | None) -> dict | None:
    """``"2023-09-29"`` → ``{year, month, day}`` (matches AniList's shape)."""
    if not date_str:
        return None
    parts = date_str.split("-")
    try:
        year = int(parts[0]) if len(parts) > 0 and parts[0] else None
        month = int(parts[1]) if len(parts) > 1 and parts[1] else None
        day = int(parts[2]) if len(parts) > 2 and parts[2] else None
    except ValueError:
        return None
    if year or month or day:
        return {"year": year, "month": month, "day": day}
    return None


def _titles_from(attrs: dict) -> tuple[str, str, list[str]]:
    """Return ``(english, romaji, all_titles)`` from a Kitsu attributes dict.

    Kitsu ``titles`` is a locale map: ``en`` (English), ``en_jp`` (the
    romanised title), ``ja_jp`` (native). ``canonicalTitle`` is the display
    default and seeds the list.
    """
    tmap = attrs.get("titles") or {}
    canonical = (attrs.get("canonicalTitle") or "").strip()
    english = (tmap.get("en") or "").strip()
    romaji = (tmap.get("en_jp") or "").strip()
    native = (tmap.get("ja_jp") or "").strip()

    ordered: list[str] = []
    seen: set[str] = set()
    for t in (canonical, english, romaji, native):
        if t and t.lower() not in seen:
            seen.add(t.lower())
            ordered.append(t)
    for t in (attrs.get("abbreviatedTitles") or []):
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            ordered.append(t)
    return (english or canonical, romaji or canonical, ordered)


class KitsuClient:
    """Mirrors ``AnilistClient``'s public interface using Kitsu's JSON:API.

    Every method has the **same signature** and returns the **same dataclass
    types** as its AniList counterpart, so the resilient client can chain to it
    as a transparent drop-in.
    """

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._cf = None  # curl_cffi.AsyncSession — Chrome-impersonating transport
        self._last_request: float = 0.0

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, headers=_JSONAPI_HEADERS)
        return self._http

    @property
    def _session(self):
        """curl_cffi Chrome session, falling back to httpx if unavailable."""
        if cf_requests is None:
            return self.http
        if self._cf is None:
            self._cf = cf_requests.AsyncSession(
                impersonate="chrome",
                timeout=30.0,
                allow_redirects=True,
                headers=_JSONAPI_HEADERS,
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

    # ── rate limiter ──────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        now = asyncio.get_event_loop().time()
        since_last = now - self._last_request
        if since_last < _RATE_LIMIT_GAP:
            await asyncio.sleep(_RATE_LIMIT_GAP - since_last)
        self._last_request = asyncio.get_event_loop().time()

    # ── HTTP helper ─────────────────────────────────────────────────────────--

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET a Kitsu endpoint, returning the **full JSON:API body**.

        Returns the parsed ``{data, included, meta, links}`` envelope untouched
        (JSON:API always wraps, and relations need ``included``) — callers read
        ``["data"]`` / ``["included"]`` themselves. Retries 429/5xx with backoff;
        returns ``None`` on 404 or hard failure. Transport-agnostic (curl_cffi or
        httpx expose the same ``.status_code`` / ``.json()`` surface).
        """
        url = f"{KITSU_URL}/{path.lstrip('/')}"
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            await self._throttle()
            last = attempt == max_attempts
            try:
                resp = await self._session.get(url, params=params)
                status = resp.status_code
                if status == 429 and not last:
                    retry_after = float(resp.headers.get("Retry-After") or 2)
                    log.warning("kitsu.ratelimit", retry_after=retry_after)
                    await asyncio.sleep(min(retry_after, 10.0))
                    continue
                if status in (500, 502, 503, 504) and not last:
                    backoff = 1.5 * attempt
                    log.warning("kitsu.http_error", url=url, status=status,
                                retry_in=backoff)
                    await asyncio.sleep(backoff)
                    continue
                if status == 404:
                    return None
                if status >= 400:
                    log.warning("kitsu.http_error.final", url=url, status=status)
                    return None
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - transport-agnostic
                log.warning("kitsu.request.failed", url=url, error=str(exc),
                            attempt=attempt)
                if not last:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                return None
        return None

    # ── search ──────────────────────────────────────────────────────────────--

    async def search(self, query: str) -> AnilistMedia | None:
        """Resolve ``query`` to a full ``AnilistMedia`` (Kitsu-backed)."""
        body = await self._get(
            "anime", {"filter[text]": query, "page[limit]": 10}
        )
        results = (body or {}).get("data") or []
        if not results:
            return None
        best = self._best_match(query, results)
        if best is None:
            return None
        return await self._resource_to_media(best)

    def _best_match(self, query: str, results: list[dict]) -> dict | None:
        """Rank Kitsu search hits: exact title → fuzzy → popularity."""
        from nekofetch.sources.telegram.matching import title_matches

        norm = query.strip().lower()

        def _rank(res: dict) -> tuple:
            attrs = res.get("attributes") or {}
            _e, _r, titles = _titles_from(attrs)
            exact = any(t.strip().lower() == norm for t in titles)
            fuzzy = max(
                (1.0 if title_matches(query, t, threshold=0.85) else 0.0)
                for t in titles
            ) if titles else 0.0
            # userCount is Kitsu's popularity proxy.
            pop = attrs.get("userCount") or attrs.get("favoritesCount") or 0
            return (1 if exact else 0, fuzzy, pop)

        return sorted(results, key=_rank, reverse=True)[0]

    async def search_candidates(
        self, query: str, *, limit: int = 25
    ) -> list[dict]:
        """Search-page candidates for the franchise picker.

        Returns ``[{id, title, format, popularity}]`` mirroring AniList's shape,
        so the grouping-into-franchises step keeps working when AniList is down.
        """
        body = await self._get(
            "anime", {"filter[text]": query, "page[limit]": min(limit, 20)}
        )
        results = (body or {}).get("data") or []
        out: list[dict] = []
        for res in results:
            attrs = res.get("attributes") or {}
            eng, _r, _titles = _titles_from(attrs)
            try:
                cid = int(res.get("id"))
            except (TypeError, ValueError):
                continue
            out.append({
                "id": cid,
                "title": eng or (attrs.get("canonicalTitle") or ""),
                "format": _kitsu_format(attrs.get("subtype")),
                "popularity": attrs.get("userCount") or 0,
            })
        return out

    async def _fetch_full(self, media_id: int) -> AnilistMedia | None:
        """Fetch full data + relations for a Kitsu id, return ``AnilistMedia``."""
        body = await self._get(f"anime/{media_id}")
        resource = (body or {}).get("data")
        if not resource:
            return None
        return await self._resource_to_media(resource)

    # ── resource → AnilistMedia ─────────────────────────────────────────────--

    async def _resource_to_media(self, resource: dict) -> AnilistMedia | None:
        """Map a Kitsu anime resource into a fully-populated ``AnilistMedia``."""
        attrs = resource.get("attributes") or {}
        try:
            kid = int(resource.get("id"))
        except (TypeError, ValueError):
            return None

        english, romaji, titles = _titles_from(attrs)
        fmt = _kitsu_format(attrs.get("subtype"))
        poster = attrs.get("posterImage") or {}
        cover = attrs.get("coverImage") or {}

        # Immediate relations (one media-relationships call).
        relations = await self._immediate_relations(kid)

        # Full franchise picture via BFS; soft-fall back to immediate relations.
        try:
            totals = await self.franchise_totals(kid)
        except Exception as exc:  # noqa: BLE001
            log.warning("kitsu.franchise_totals.failed", id=kid, error=str(exc))
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
                r for r in relations
                if r.format in _SERIES_FORMATS and r.relation in _CONTINUATION_RELATIONS
            ]
            franchise_seasons = 1 + len(season_entries)
            franchise_movies = sum(1 for r in relations if r.format == "MOVIE")
            franchise_ovas = sum(1 for r in relations if r.format == "OVA")
            franchise_onas = sum(1 for r in relations if r.format == "ONA")
            franchise_specials = sum(1 for r in relations if r.format == "SPECIAL")
            franchise_episodes = attrs.get("episodeCount") or None

        return AnilistMedia(
            id=kid,
            format=fmt,
            season=None,
            year=_year_from(attrs.get("startDate")),
            start_date=_start_date(attrs.get("startDate")),
            episodes=attrs.get("episodeCount"),
            duration=attrs.get("episodeLength"),
            status=_kitsu_status(attrs.get("status")),
            score=_score_from_rating(attrs.get("averageRating")),
            popularity=attrs.get("userCount"),
            genres=[],  # Kitsu genres need a separate include; left empty (best-effort)
            synopsis=attrs.get("synopsis") or attrs.get("description"),
            studio=None,
            cover_url=poster.get("large") or poster.get("original")
            or poster.get("medium"),
            banner_url=cover.get("large") or cover.get("original"),
            english=english,
            romaji=romaji,
            titles=titles,
            synonyms=[],
            relations=relations,
            anilist_url=f"{KITSU_SITE}/{kid}",
            franchise_episodes=franchise_episodes,
            franchise_seasons=franchise_seasons,
            franchise_movies=franchise_movies,
            franchise_ovas=franchise_ovas,
            franchise_onas=franchise_onas,
            franchise_specials=franchise_specials,
        )

    def _resource_to_entry(
        self, resource: dict, *, relation: str
    ) -> FranchiseEntry | None:
        """Map a Kitsu anime resource into a ``FranchiseEntry``."""
        attrs = resource.get("attributes") or {}
        try:
            kid = int(resource.get("id"))
        except (TypeError, ValueError):
            return None
        english, _romaji, titles = _titles_from(attrs)
        poster = attrs.get("posterImage") or {}
        cover = attrs.get("coverImage") or {}
        return FranchiseEntry(
            anilist_id=kid,
            format=_kitsu_format(attrs.get("subtype")) or "",
            english_title=english or (attrs.get("canonicalTitle") or ""),
            titles=titles,
            banner_url=cover.get("large") or cover.get("original"),
            cover_url=poster.get("large") or poster.get("original"),
            episodes=attrs.get("episodeCount"),
            duration=attrs.get("episodeLength"),
            season_part=None,
            start_date=_start_date(attrs.get("startDate")),
            relation=relation,
            synopsis=attrs.get("synopsis") or attrs.get("description"),
            score=_score_from_rating(attrs.get("averageRating")),
            status=_kitsu_status(attrs.get("status")),
        )

    # ── relations (media-relationships endpoint) ──────────────────────────────

    async def _relationship_edges(
        self, anime_id: int
    ) -> list[tuple[str, dict]]:
        """Return ``[(relation, destination_resource), …]`` for ``anime_id``.

        Hits ``/media-relationships`` with the destination side-loaded, keeps
        only anime destinations, and maps Kitsu roles to AniList relations.
        """
        body = await self._get("media-relationships", {
            "filter[source_id]": anime_id,
            "filter[source_type]": "Anime",
            "include": "destination",
            "page[limit]": 20,
        })
        if not body:
            return []
        # Index side-loaded destinations by (type, id).
        included = {
            (inc.get("type"), str(inc.get("id"))): inc
            for inc in (body.get("included") or [])
        }
        edges: list[tuple[str, dict]] = []
        for rel in body.get("data") or []:
            relation = _kitsu_relation((rel.get("attributes") or {}).get("role"))
            if relation is None:
                continue
            dest = (((rel.get("relationships") or {}).get("destination") or {})
                    .get("data") or {})
            dtype, did = dest.get("type"), str(dest.get("id"))
            # Only follow anime destinations (Kitsu links manga adaptations too).
            if dtype != "anime":
                continue
            resource = included.get((dtype, did))
            if resource is not None:
                edges.append((relation, resource))
        return edges

    async def _immediate_relations(self, anime_id: int) -> list[FranchiseRelation]:
        """Immediate ``FranchiseRelation`` list for the AnilistMedia.relations."""
        relations: list[FranchiseRelation] = []
        for relation, resource in await self._relationship_edges(anime_id):
            attrs = resource.get("attributes") or {}
            try:
                rid = int(resource.get("id"))
            except (TypeError, ValueError):
                continue
            eng, _r, _titles = _titles_from(attrs)
            poster = attrs.get("posterImage") or {}
            relations.append(FranchiseRelation(
                relation=relation,
                format=_kitsu_format(attrs.get("subtype")),
                status=_kitsu_status(attrs.get("status")),
                episodes=attrs.get("episodeCount"),
                titles=[eng or (attrs.get("canonicalTitle") or "")],
                anilist_id=rid,
                cover_url=poster.get("large") or poster.get("original"),
                banner_url=None,
            ))
        return relations

    async def franchise_totals(
        self, root_id: int, *, max_nodes: int = 120
    ) -> FranchiseTotals:
        """Walk the connected franchise graph and tally by format.

        BFS outward from ``root_id`` over SEQUEL / PREQUEL / SIDE_STORY / PARENT
        (mirrors AniList's ``_TRAVERSE_RELATIONS``); TV/TV_SHORT nodes reachable
        through continuity edges (SEQUEL/PREQUEL) count as seasons.
        """
        visited: set[int] = {root_id}
        nodes: dict[int, tuple[str | None, int | None]] = {}
        cont_adj: dict[int, set[int]] = {}
        frontier: list[int] = [root_id]

        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            if nid in nodes:
                continue
            # Root's own format/episodes come from its resource; children come
            # from the edge destination resources we already side-loaded.
            resource = await self._get(f"anime/{nid}")
            data = (resource or {}).get("data")
            if not data:
                nodes[nid] = (None, None)
                continue
            attrs = data.get("attributes") or {}
            # Skip in-flight / cancelled installments (never the root) so an
            # airing/announced season can't inflate the count — matches AniList.
            if nid != root_id and not _is_released(_kitsu_status(attrs.get("status"))):
                continue
            nodes[nid] = (_kitsu_format(attrs.get("subtype")), attrs.get("episodeCount"))

            for relation, dest in await self._relationship_edges(nid):
                if relation not in _CONTENT_WALK_RELS:
                    continue
                try:
                    eid = int(dest.get("id"))
                except (TypeError, ValueError):
                    continue
                if eid in visited:
                    continue
                efmt = _kitsu_format((dest.get("attributes") or {}).get("subtype"))
                if efmt not in _ANIME_FORMATS:
                    continue
                if relation in _CONTINUATION_RELATIONS:
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
        """BFS-walk the franchise graph, return ``{id: FranchiseEntry}``."""
        body = await self._get(f"anime/{root_id}")
        root = (body or {}).get("data")
        if not root:
            return {}

        entries: dict[int, FranchiseEntry] = {}
        visited: set[int] = {root_id}
        relation_map: dict[int, str] = {}

        root_entry = self._resource_to_entry(root, relation="ROOT")
        if root_entry is not None:
            entries[root_id] = root_entry

        # Seed + BFS. We already receive the destination resource on each edge,
        # so children are turned into entries without an extra fetch.
        frontier: list[int] = [root_id]
        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            for relation, dest in await self._relationship_edges(nid):
                if relation not in _CONTENT_WALK_RELS:
                    continue
                try:
                    eid = int(dest.get("id"))
                except (TypeError, ValueError):
                    continue
                efmt = _kitsu_format((dest.get("attributes") or {}).get("subtype"))
                if efmt not in _ANIME_FORMATS:
                    continue
                if eid not in relation_map:
                    relation_map[eid] = relation
                if eid in visited:
                    continue
                visited.add(eid)
                entry = self._resource_to_entry(dest, relation=relation_map[eid])
                # Only released/finished canonical entries belong in the walk —
                # an airing/announced Kitsu season must not enter the franchise.
                if entry is not None and _is_released(entry.status):
                    entries[eid] = entry
                frontier.append(eid)

        return entries

    async def title_variants(self, query: str) -> list[str]:
        """All known titles for the best match (for search-term expansion)."""
        media = await self.search(query)
        if media is None:
            return [query]
        variants = list(media.titles or [])
        for t in (media.english, media.romaji):
            if t and t not in variants:
                variants.append(t)
        return variants or [query]
