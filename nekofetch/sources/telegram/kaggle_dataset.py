"""Kaggle anime dataset tier — the FULL AniList snapshot, with relations.

A local dataset tier (chained after AniList, alongside the seasonal LeoRigasaki
one) sourced from the public ``calebmwelsh/anilist-anime-dataset`` Kaggle set.
Unlike the seasonal repo, this is the COMPLETE AniList table (~20k rows) and —
critically — carries a ``relations`` column (AniList's own JSON: ``relationType``
+ ``node{id,title,type,format,status}``). That lets us resolve the full
franchise graph OFFLINE: ``walk_franchise_full`` / ``franchise_totals`` BFS the
in-memory rows by id, so per-season episode counts are accurate even with every
API down.

Download + refresh (owner spec):
* Public, no-auth: ``https://www.kaggle.com/api/v1/datasets/download/<slug>``
  returns a 257 MB zip containing ``anilist_anime_data_complete.csv`` (~438 MB).
* Cached to ``<storage>/cache/kaggle_anilist_complete.csv`` + a sidecar meta
  (``zip_size``, ``fetched_at``).
* **Weekly (7-day) refresh with a byte-exact size check**: past the 7-day gate we
  read the remote zip's Content-Length (no body download); if it differs from the
  stored size by even one byte, the dataset changed → re-download, re-extract,
  re-index, and atomically swap it in. Otherwise we just bump ``fetched_at``.
* **Never disrupts an ongoing task**: the refresh runs as a background task off
  the event loop (``to_thread`` for I/O + parse) and swaps the in-memory index
  atomically, so concurrent lookups keep using the old index until the new one
  is fully built. The triggering call returns immediately.

Memory: the extracted CSV is stream-parsed into slim per-row dicts (only the
fields we map + the raw relations cell), which is comfortable on the target VPS.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import zipfile
from pathlib import Path

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.sources.telegram.anilist import (
    AnilistMedia,
    FranchiseEntry,
    FranchiseRelation,
    FranchiseTotals,
    _ANIME_FORMATS,
    _CONTENT_WALK_RELS,
    _CONTINUATION_RELATIONS,
    _SERIES_FORMATS,
    _TRAVERSE_RELATIONS,
)
from nekofetch.sources.telegram.anime_dataset import _int, _norm, _score

log = get_logger(__name__)

_DATASET_SLUG = "calebmwelsh/anilist-anime-dataset"
_DOWNLOAD_URL = f"https://www.kaggle.com/api/v1/datasets/download/{_DATASET_SLUG}"
_CSV_MEMBER_HINT = "anilist_anime_data_complete.csv"
_REFRESH_SECONDS = 7 * 86_400  # weekly, per owner spec

# csv relations cells can be large — lift the field size cap once at import.
csv.field_size_limit(16_000_000)


def _parse_genres(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("[]", "nan", "none", "null"):
        return []
    for loader in (json.loads, _literal_eval):
        try:
            val = loader(raw)
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
        except Exception:  # noqa: BLE001
            continue
    return [p.strip() for p in raw.replace("|", ",").split(",") if p.strip()]


def _literal_eval(raw):
    import ast
    return ast.literal_eval(raw)


class KaggleDatasetClient:
    """AniList-shaped tier backed by the full Kaggle CSV, WITH relations.

    Mirrors the ``AnilistClient`` interface the resilient chain uses, including
    ``walk_franchise_full`` / ``franchise_totals`` resolved from the ``relations``
    column entirely offline.
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        base = Path(cache_dir) if cache_dir else Path("data/storage") / "cache"
        self._cache_dir = base
        self._csv_path = base / "kaggle_anilist_complete.csv"
        self._meta_path = base / "kaggle_anilist_meta.json"
        self._by_title: dict[str, int] = {}   # normalized title -> id
        self._by_id: dict[int, dict] = {}      # id -> slim row
        self._loaded = False
        self._refreshing = False
        self._downloading = False
        self._load_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

    def set_cache_dir(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._csv_path = self._cache_dir / "kaggle_anilist_complete.csv"
        self._meta_path = self._cache_dir / "kaggle_anilist_meta.json"

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        return self._http

    # ── meta sidecar ───────────────────────────────────────────────────────────

    def _read_meta(self) -> dict:
        try:
            return json.loads(self._meta_path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _write_meta(self, zip_size: int) -> None:
        try:
            self._meta_path.write_text(json.dumps({
                "zip_size": zip_size,
                "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }))
        except OSError as exc:
            log.warning("kaggle.meta_write_failed", error=str(exc))

    def _refresh_due(self) -> bool:
        meta = self._read_meta()
        fetched = meta.get("fetched_at")
        if not fetched:
            return True
        try:
            when = datetime.datetime.fromisoformat(fetched)
        except ValueError:
            return True
        age = (datetime.datetime.now() - when).total_seconds()
        return age > _REFRESH_SECONDS

    # ── download + index ───────────────────────────────────────────────────────

    async def prefetch(self) -> bool:
        """Download the dataset to disk NOW (foreground), if absent or stale.

        For the launcher/warm-up: unlike :meth:`_ensure_loaded` (which backgrounds
        the download and returns a MISS so no user request blocks), this awaits the
        zip download + extract synchronously so the CSV is on disk before the bots
        start. Idempotent — a present, non-stale CSV is a no-op (returns True). The
        in-memory index is NOT built here; the running client builds it on first
        use. Returns True when the CSV exists on disk afterwards."""
        if self._csv_path.exists() and not self._refresh_due():
            return True
        zip_bytes = await self._download_zip()
        if not zip_bytes:
            return self._csv_path.exists()  # keep an existing (stale) copy usable
        if not await asyncio.to_thread(self._extract_to_cache, zip_bytes):
            return self._csv_path.exists()
        self._write_meta(len(zip_bytes))
        return self._csv_path.exists()

    async def _remote_zip_size(self) -> int | None:
        """Read the remote zip's Content-Length WITHOUT downloading the body."""
        try:
            async with self._client().stream("GET", _DOWNLOAD_URL) as resp:
                if resp.status_code != 200:
                    return None
                cl = resp.headers.get("content-length")
                return int(cl) if cl and cl.isdigit() else None
        except Exception as exc:  # noqa: BLE001
            log.debug("kaggle.size_probe_failed", error=str(exc))
            return None

    async def _download_zip(self) -> bytes | None:
        try:
            resp = await self._client().get(_DOWNLOAD_URL)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception as exc:  # noqa: BLE001
            log.warning("kaggle.download_failed", error=str(exc))
        return None

    def _extract_to_cache(self, zip_bytes: bytes) -> bool:
        """Extract the CSV member atomically into the cache. Returns success."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            z = zipfile.ZipFile(io.BytesIO(zip_bytes))
            member = next(
                (n for n in z.namelist()
                 if n.lower().endswith(_CSV_MEMBER_HINT.lower())),
                next((n for n in z.namelist() if n.lower().endswith(".csv")), None),
            )
            if member is None:
                return False
            tmp = self._csv_path.with_suffix(".csv.tmp")
            with z.open(member) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            tmp.replace(self._csv_path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("kaggle.extract_failed", error=str(exc))
            return False

    def _build_index(self, csv_path: Path) -> tuple[dict, dict]:
        """Stream-parse the CSV into (by_title, by_id) — off the event loop."""
        by_title: dict[str, int] = {}
        by_id: dict[int, dict] = {}
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                aid = _int(row.get("id"))
                if aid is None:
                    continue
                slim = {
                    "id": aid,
                    "mal": _int(row.get("idMal")),
                    "romaji": (row.get("title_romaji") or "").strip(),
                    "english": (row.get("title_english") or "").strip(),
                    "native": (row.get("title_native") or "").strip(),
                    "preferred": (row.get("title_userPreferred") or "").strip(),
                    "format": (row.get("format") or "").strip().upper() or None,
                    "episodes": _int(row.get("episodes")),
                    "duration": _int(row.get("duration")),
                    "status": (row.get("status") or "").strip().upper() or None,
                    "score": _score(row.get("averageScore")),
                    "popularity": _int(row.get("popularity")),
                    "genres": _parse_genres(row.get("genres") or ""),
                    "synopsis": (row.get("description") or "").strip() or None,
                    "cover": (row.get("coverImage_large")
                              or row.get("coverImage_extraLarge") or "").strip() or None,
                    "banner": (row.get("bannerImage") or "").strip() or None,
                    "season_year": _int(row.get("seasonYear")),
                    "relations_raw": row.get("relations") or "",
                }
                by_id[aid] = slim
                for key in ("english", "romaji", "native", "preferred"):
                    t = slim[key]
                    if t:
                        by_title.setdefault(_norm(t), aid)
        log.info("kaggle.indexed", ids=len(by_id), titles=len(by_title))
        return by_title, by_id

    async def _ensure_loaded(self) -> bool:
        if self._loaded and self._by_id:
            self._maybe_background_refresh()
            return True
        async with self._load_lock:
            if self._loaded and self._by_id:
                return True
            if not self._csv_path.exists():
                # No local copy yet — download the 257 MB set in the BACKGROUND
                # and MISS for now, so no user request ever blocks on it. The
                # chain falls through to the other tiers until it's ready; a
                # later search picks it up once indexed.
                if not self._downloading:
                    self._downloading = True
                    try:
                        asyncio.create_task(self._background_download_and_index())
                    except RuntimeError:  # no running loop (sync test)
                        self._downloading = False
                return False
            # CSV present — index it (a few-second parse, off the event loop).
            try:
                by_title, by_id = await asyncio.to_thread(
                    self._build_index, self._csv_path
                )
                self._by_title, self._by_id = by_title, by_id
            except Exception as exc:  # noqa: BLE001
                log.warning("kaggle.index_failed", error=str(exc))
            self._loaded = True
        self._maybe_background_refresh()
        return bool(self._by_id)

    async def _background_download_and_index(self) -> None:
        """Cold first-load: download + extract + index off the event loop."""
        try:
            zip_bytes = await self._download_zip()
            if not zip_bytes:
                return
            if not await asyncio.to_thread(self._extract_to_cache, zip_bytes):
                return
            self._write_meta(len(zip_bytes))
            by_title, by_id = await asyncio.to_thread(
                self._build_index, self._csv_path
            )
            self._by_title, self._by_id = by_title, by_id
            self._loaded = True
            log.info("kaggle.background_load.done", ids=len(by_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("kaggle.background_load.failed", error=str(exc))
        finally:
            self._downloading = False

    def _maybe_background_refresh(self) -> None:
        """Kick a background weekly refresh if due — never blocks the caller."""
        if self._refreshing or not self._refresh_due():
            return
        self._refreshing = True
        try:
            asyncio.create_task(self._background_refresh())
        except RuntimeError:
            self._refreshing = False  # no running loop (e.g. sync test)

    async def _background_refresh(self) -> None:
        """Weekly size-diff check → re-download + re-index + atomic swap."""
        try:
            remote = await self._remote_zip_size()
            stored = self._read_meta().get("zip_size")
            if remote is not None and stored is not None and remote == stored:
                # Unchanged to the byte — just reset the 7-day clock.
                self._write_meta(stored)
                log.info("kaggle.refresh.unchanged", size=stored)
                return
            log.info("kaggle.refresh.changed", stored=stored, remote=remote)
            zip_bytes = await self._download_zip()
            if not zip_bytes:
                return
            ok = await asyncio.to_thread(self._extract_to_cache, zip_bytes)
            if not ok:
                return
            by_title, by_id = await asyncio.to_thread(
                self._build_index, self._csv_path
            )
            # Atomic swap: in-flight lookups used the old dicts until this line.
            self._by_title, self._by_id = by_title, by_id
            self._write_meta(len(zip_bytes))
            log.info("kaggle.refresh.done", ids=len(by_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("kaggle.refresh.failed", error=str(exc))
        finally:
            self._refreshing = False

    # ── row → dataclasses ────────────────────────────────────────────────────--

    def _row_to_media(self, row: dict) -> AnilistMedia | None:
        aid = row.get("id")
        if aid is None:
            return None
        english = row["english"] or row["preferred"] or row["romaji"]
        romaji = row["romaji"] or row["preferred"] or english
        titles = [t for t in (english, romaji, row["native"], row["preferred"]) if t]
        seen: set[str] = set()
        titles = [t for t in titles if not (t.lower() in seen or seen.add(t.lower()))]
        fmt = row["format"] if row["format"] in _ANIME_FORMATS else row["format"]
        relations = self._parse_relations(row.get("relations_raw") or "")
        totals = self._totals_from_walk(aid)
        return AnilistMedia(
            id=aid,
            format=fmt,
            season=None,
            year=row.get("season_year"),
            start_date=None,
            episodes=row.get("episodes"),
            duration=row.get("duration"),
            status=row.get("status"),
            score=row.get("score"),
            popularity=row.get("popularity"),
            genres=list(row.get("genres") or []),
            synopsis=row.get("synopsis"),
            studio=None,
            cover_url=row.get("cover"),
            banner_url=row.get("banner"),
            english=english,
            romaji=romaji,
            titles=titles,
            synonyms=[],
            relations=relations,
            anilist_url=f"https://anilist.co/anime/{aid}",
            franchise_episodes=(totals.episodes or row.get("episodes")) or None,
            franchise_seasons=totals.seasons or 1,
            franchise_movies=totals.movies,
            franchise_ovas=totals.ovas,
            franchise_onas=totals.onas,
            franchise_specials=totals.specials,
        )

    def _row_to_entry(self, row: dict, *, relation: str) -> FranchiseEntry:
        english = row["english"] or row["preferred"] or row["romaji"]
        titles = [t for t in (english, row["romaji"], row["native"]) if t]
        return FranchiseEntry(
            anilist_id=row["id"],
            format=row["format"] or "",
            english_title=english,
            titles=titles,
            banner_url=row.get("banner"),
            cover_url=row.get("cover"),
            episodes=row.get("episodes"),
            duration=row.get("duration"),
            season_part=None,
            start_date=None,
            relation=relation,
            synopsis=row.get("synopsis"),
            score=row.get("score"),
        )

    def _parse_relations(self, cell: str) -> list[FranchiseRelation]:
        out: list[FranchiseRelation] = []
        for relation, node in self._relation_edges(cell):
            titles = node.get("title") or {}
            rid = _int(node.get("id"))
            if rid is None:
                continue
            out.append(FranchiseRelation(
                relation=relation,
                format=(node.get("format") or "").upper() or None,
                status=(node.get("status") or "").upper() or None,
                episodes=None,
                titles=[titles.get("english") or titles.get("romaji") or ""],
                anilist_id=rid,
                cover_url=None,
                banner_url=None,
            ))
        return out

    @staticmethod
    def _relation_edges(cell: str) -> list[tuple[str, dict]]:
        """Parse the AniList-JSON ``relations`` cell → [(relationType, node), …],
        keeping only ANIME nodes."""
        cell = (cell or "").strip()
        if not cell or cell in ("[]", "nan", "None"):
            return []
        try:
            items = json.loads(cell)
        except Exception:  # noqa: BLE001
            try:
                items = _literal_eval(cell)
            except Exception:  # noqa: BLE001
                return []
        edges: list[tuple[str, dict]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            node = item.get("node") or {}
            if (node.get("type") or "").upper() != "ANIME":
                continue
            relation = (item.get("relationType") or "").upper()
            if relation:
                edges.append((relation, node))
        return edges

    # ── public API ─────────────────────────────────────────────────────────────

    async def search(self, query: str) -> AnilistMedia | None:
        if not await self._ensure_loaded():
            return None
        key = _norm(query)
        aid = self._by_title.get(key)
        if aid is None and len(key) >= 8:
            cand = [(t, i) for t, i in self._by_title.items()
                    if key in t or t in key]
            if cand:
                cand.sort(key=lambda ti: len(ti[0]))
                aid = cand[0][1]
        row = self._by_id.get(aid) if aid is not None else None
        return self._row_to_media(row) if row else None

    async def search_candidates(self, query: str, *, limit: int = 25) -> list[dict]:
        if not await self._ensure_loaded():
            return []
        key = _norm(query)
        if not key:
            return []
        out: list[dict] = []
        for t, aid in self._by_title.items():
            if key not in t and t not in key:
                continue
            row = self._by_id.get(aid)
            if row is None:
                continue
            out.append({
                "id": aid,
                "title": row["english"] or row["romaji"],
                "format": row["format"],
                "popularity": row.get("popularity") or 0,
            })
            if len(out) >= limit:
                break
        return out

    async def _fetch_full(self, media_id: int) -> AnilistMedia | None:
        if not await self._ensure_loaded():
            return None
        row = self._by_id.get(int(media_id)) if media_id is not None else None
        return self._row_to_media(row) if row else None

    async def title_variants(self, query: str) -> list[str]:
        media = await self.search(query)
        return list(media.titles) if media and media.titles else [query]

    # ── franchise walk (offline BFS over the relations column) ─────────────────

    async def walk_franchise_full(
        self, root_id: int, *, max_nodes: int = 120
    ) -> dict[int, FranchiseEntry]:
        if not await self._ensure_loaded():
            return {}
        root = self._by_id.get(int(root_id))
        if root is None:
            return {}
        entries: dict[int, FranchiseEntry] = {
            root_id: self._row_to_entry(root, relation="ROOT")
        }
        visited = {root_id}
        relation_map: dict[int, str] = {}
        frontier = [root_id]
        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            row = self._by_id.get(nid)
            if row is None:
                continue
            for relation, node in self._relation_edges(row.get("relations_raw") or ""):
                if relation not in _CONTENT_WALK_RELS:
                    continue
                eid = _int(node.get("id"))
                if eid is None or eid in visited:
                    continue
                child = self._by_id.get(eid)
                if child is None or (child["format"] not in _ANIME_FORMATS):
                    continue
                relation_map.setdefault(eid, relation)
                visited.add(eid)
                entries[eid] = self._row_to_entry(child, relation=relation_map[eid])
                frontier.append(eid)
        return entries

    async def franchise_totals(
        self, root_id: int, *, max_nodes: int = 120
    ) -> FranchiseTotals:
        if not await self._ensure_loaded():
            return FranchiseTotals()
        return self._totals_from_walk(int(root_id), max_nodes=max_nodes)

    def _totals_from_walk(self, root_id: int, *, max_nodes: int = 120) -> FranchiseTotals:
        """Synchronous BFS tally over the local rows (used by _row_to_media too)."""
        root = self._by_id.get(root_id)
        if root is None:
            return FranchiseTotals()
        nodes: dict[int, tuple[str | None, int | None]] = {}
        cont_adj: dict[int, set[int]] = {}
        visited = {root_id}
        frontier = [root_id]
        while frontier and len(visited) <= max_nodes:
            nid = frontier.pop(0)
            if nid in nodes:
                continue
            row = self._by_id.get(nid)
            if row is None:
                continue
            nodes[nid] = (row["format"], row.get("episodes"))
            for relation, node in self._relation_edges(row.get("relations_raw") or ""):
                if relation not in _TRAVERSE_RELATIONS:
                    continue
                eid = _int(node.get("id"))
                if eid is None or eid in visited:
                    continue
                child = self._by_id.get(eid)
                if child is None or child["format"] not in _ANIME_FORMATS:
                    continue
                if relation in _CONTINUATION_RELATIONS:
                    cont_adj.setdefault(nid, set()).add(eid)
                    cont_adj.setdefault(eid, set()).add(nid)
                visited.add(eid)
                frontier.append(eid)

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
