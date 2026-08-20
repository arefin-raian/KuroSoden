"""Metadata prefetch — fetch everything once at acceptance, cache to disk.

The moment a request is accepted (the requester tapped "Yes, that's it" and the
request cleared the not-already-published criteria), we fetch EVERYTHING the
later stages will ever need and write it to the request's work folder as JSON,
plus mirror the TMDB artwork to our image hosts. From then on Levi/Senku/Gojo
read these files instead of re-hitting the public APIs — which is what was
getting us rate-limited (a single public AniList key, shared across every
stage, re-queried on every button tap).

Layout under ``<storage>/metadata/<folder>/`` (folder = safe anime_doc_id):

    anilist.json   — full AnilistMedia (search result) serialized
    jikan.json     — Jikan/MAL payload (raw), when reachable
    tmdb.json      — TmdbResult + the /images asset lists (logos/posters/backdrops)
    assets.json    — every artwork URL mirrored to catbox/telegraph/imgbb, with
                     which host stuck + the local file path
    <hash>.jpg     — the downloaded artwork bytes (deleted after publish/handoff)

Everything is best-effort: a source that fails leaves its file absent and the
consumer falls back to a live fetch, exactly as before. The one hard rule is the
AniList rate-limit dance — on HTTP 429 we wait the documented 60 s and retry
rather than aborting, because a mid-acceptance rate-limit used to poison the
whole franchise walk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

# AniList's documented cooldown after a 429 is 60 s. We wait it out and retry
# rather than aborting — the whole point of prefetch is to pay the API cost once,
# up front, off the user's critical path.
_ANILIST_RATELIMIT_WAIT = 60.0
_ANILIST_MAX_RETRIES = 2

_META_SUBDIR = "metadata"


def _folder_name(code: str, anime_doc_id: str | None) -> str:
    """Filesystem-safe folder name for a request's cached metadata."""
    import re

    base = (anime_doc_id or code or "work").strip()
    return re.sub(r"[^\w.\-]+", "_", base).strip("_") or "work"


def metadata_dir(container: Container, code: str,
                 anime_doc_id: str | None = None) -> Path:
    """The per-request metadata folder (created on demand)."""
    root = container.env.storage_path / _META_SUBDIR / _folder_name(code, anime_doc_id)
    return root


def _jsonable(obj: Any) -> Any:
    """Best-effort recursive conversion of dataclasses / objects to JSON types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    # Fall back to __dict__ for plain objects, else str().
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {str(k): _jsonable(v) for k, v in d.items() if not k.startswith("_")}
    return str(obj)


async def _write_json(path: Path, data: Any) -> None:
    """Serialize ``data`` to ``path`` atomically, off the event loop."""
    def _do() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_jsonable(data), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    await asyncio.to_thread(_do)


def _is_ratelimit(exc: Exception) -> bool:
    """True when ``exc`` looks like an HTTP 429 / rate-limit error."""
    s = str(exc).lower()
    if "429" in s or "too many requests" in s or "rate limit" in s:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


async def _with_anilist_retry(coro_factory, *, label: str):
    """Run an AniList call, waiting 60 s and retrying on a 429.

    ``coro_factory`` is a zero-arg callable returning a fresh awaitable each
    attempt (an awaitable can't be re-awaited). Returns the result, or ``None``
    if every attempt failed."""
    for attempt in range(_ANILIST_MAX_RETRIES + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            if _is_ratelimit(exc) and attempt < _ANILIST_MAX_RETRIES:
                log.warning("prefetch.anilist.ratelimited",
                            label=label, wait=_ANILIST_RATELIMIT_WAIT,
                            attempt=attempt + 1)
                await asyncio.sleep(_ANILIST_RATELIMIT_WAIT)
                continue
            log.warning("prefetch.anilist.failed", label=label, error=str(exc))
            return None
    return None


class MetadataPrefetchService:
    """Fetch + cache AniList / Jikan / TMDB data and artwork for one request."""

    def __init__(self, container: Container) -> None:
        self._c = container

    async def prefetch(self, code: str, anime_doc_id: str | None,
                       franchise: dict) -> dict:
        """Fetch everything for ``code`` and cache it to the request folder.

        Returns a small summary dict (which files were written, host tallies).
        Never raises — a prefetch failure must not block acceptance."""
        out_dir = metadata_dir(self._c, code, anime_doc_id)
        title = (franchise.get("english") or franchise.get("title")
                 or franchise.get("romaji") or code)
        anilist_id = franchise.get("anilist_id")
        summary: dict[str, Any] = {"dir": str(out_dir), "written": []}

        # Run the three source fetches concurrently; each is independently
        # best-effort. AniList + Jikan share the title; TMDB does its own search.
        results = await asyncio.gather(
            self._prefetch_anilist(out_dir, title, anilist_id),
            self._prefetch_jikan(out_dir, title, anilist_id),
            self._prefetch_tmdb(out_dir, title, franchise),
            return_exceptions=True,
        )
        for name, res in zip(("anilist", "jikan", "tmdb"), results):
            if isinstance(res, Exception):
                log.warning("prefetch.source.crashed", source=name, error=str(res))
            elif res:
                summary["written"].append(name)
        log.info("prefetch.done", code=code, title=title,
                 written=summary["written"])
        return summary

    # ── AniList ──────────────────────────────────────────────────────────────
    async def _prefetch_anilist(self, out_dir: Path, title: str,
                                anilist_id: int | None) -> bool:
        anilist = getattr(self._c, "anilist", None)
        if anilist is None:
            return False
        media = await _with_anilist_retry(
            lambda: anilist.search(title), label="search")
        if media is None:
            return False
        payload: dict[str, Any] = {"search": _jsonable(media)}
        # Full franchise walk (the expensive multi-request BFS) — cache it so
        # the distribution/publish stages never re-walk.
        mid = getattr(media, "id", None) or anilist_id
        if mid is not None and hasattr(anilist, "walk_franchise_full"):
            walk = await _with_anilist_retry(
                lambda: anilist.walk_franchise_full(int(mid)), label="walk")
            if walk is not None:
                payload["franchise"] = _jsonable(walk)
        await _write_json(out_dir / "anilist.json", payload)
        # Mirror every AniList cover (root + each franchise installment) to disk
        # and the image hosts, keyed by anilist_id. The thumbnail stage and the
        # pack uploader then read the saved file (or a hosted backup) instead of
        # re-hitting AniList on every run — and each season/OVA pack can use ITS
        # OWN cover as the file thumbnail (per operator spec), not a shared TMDB
        # poster.
        try:
            await self._mirror_anilist_covers(out_dir, media,
                                              payload.get("franchise"))
        except Exception as exc:  # noqa: BLE001
            log.warning("prefetch.anilist.covers_failed", error=str(exc))
        return True

    async def _mirror_anilist_covers(self, out_dir: Path, media: Any,
                                     franchise: Any) -> None:
        """Download + locally save + host-mirror every AniList cover.

        Writes ``anilist_images.json`` — ``{"covers": [{anilist_id, source_url,
        local, catbox, telegraph, imgbb, primary, host}]}`` — so consumers pick
        each installment's cover by ``anilist_id`` and load it from the local
        file (cleaned by :func:`cleanup_local_assets`, which wipes ``images/``)
        or a hosted backup, never a fresh AniList fetch."""
        from kurosoden.shared.image_backup import backup_bytes

        pairs: list[tuple[int | None, str]] = []
        root_cover = getattr(media, "cover_url", None)
        if root_cover:
            pairs.append((getattr(media, "id", None), root_cover))
        if isinstance(franchise, dict):
            for entry in franchise.values():
                if isinstance(entry, dict) and entry.get("cover_url"):
                    pairs.append((entry.get("anilist_id"), entry["cover_url"]))

        img_dir = out_dir / "images"
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()

        async def _one(aid: int | None, url: str) -> None:
            if not url or url in seen:
                return
            seen.add(url)
            blob = mime = None
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60.0,
                                             follow_redirects=True) as cli:
                    r = await cli.get(url)
                    r.raise_for_status()
                    blob = r.content
                    mime = (r.headers.get("content-type")
                            or "image/jpeg").split(";", 1)[0].strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("prefetch.anilist_cover.download_failed",
                            url=url, error=str(exc))
                return
            if not blob:
                return
            ext = ".png" if mime == "image/png" else ".jpg"
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            local_path = img_dir / f"anilist_{digest}{ext}"

            def _save() -> None:
                img_dir.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(blob)
            await asyncio.to_thread(_save)
            mirror = await backup_bytes(self._c, blob, mime=mime or "image/jpeg",
                                        source_url=url)
            host = ("imgbb" if mirror.imgbb_url else
                    "catbox" if mirror.catbox_url else
                    "telegraph" if mirror.telegraph_url else None)
            entries.append({
                "anilist_id": aid, "source_url": url,
                "local": str(local_path),
                "catbox": mirror.catbox_url, "telegraph": mirror.telegraph_url,
                "imgbb": mirror.imgbb_url, "primary": mirror.primary,
                "host": host,
            })

        sem = asyncio.Semaphore(4)

        async def _guarded(aid: int | None, url: str) -> None:
            async with sem:
                await _one(aid, url)

        if pairs:
            await asyncio.gather(*(_guarded(a, u) for a, u in pairs),
                                 return_exceptions=True)
        await _write_json(out_dir / "anilist_images.json", {"covers": entries})
        log.info("prefetch.anilist_covers.mirrored", count=len(entries))

    # ── Jikan / MAL ──────────────────────────────────────────────────────────
    async def _prefetch_jikan(self, out_dir: Path, title: str,
                              anilist_id: int | None) -> bool:
        """Fetch Jikan (jikan.moe, the unofficial MAL API) by title search.

        Jikan is public and unauthenticated; it rate-limits at ~3 req/s /
        60 req/min, so we make ONE search call and cache the top hit's full
        payload. Best-effort — a miss just leaves jikan.json absent.

        Jikan sits behind Cloudflare, which serves Python's stock TLS
        fingerprint (httpx/urllib) a synthetic 504 on every request. We use
        curl_cffi with Chrome impersonation (falling back to httpx only if
        curl_cffi is unavailable) so the call actually reaches MAL.
        """
        from nekofetch.sources.telegram.myanimelist import _jikan_bases
        params = {"q": title, "limit": 1}
        log.info("prefetch.jikan.start", title=title, anilist_id=anilist_id)

        async def _fetch(base: str) -> dict | None:
            url = f"{base}/anime"
            try:
                from curl_cffi import requests as cf_requests
            except ImportError:
                cf_requests = None
            if cf_requests is not None:
                sess = cf_requests.AsyncSession(
                    impersonate="chrome", timeout=30.0, allow_redirects=True)
                try:
                    for attempt in range(3):
                        r = await sess.get(url, params=params)
                        if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        if r.status_code >= 400:
                            log.warning("prefetch.jikan.http_error",
                                        transport="curl_cffi", status=r.status_code)
                            return None
                        return r.json()
                    log.debug("prefetch.jikan.gave_up",
                              transport="curl_cffi", base=base, status=r.status_code)
                    return None
                finally:
                    try:
                        await sess.close()
                    except Exception:  # noqa: BLE001
                        pass
            # Fallback: plain httpx (may 504 behind Cloudflare).
            log.warning("prefetch.jikan.no_curl_cffi",
                        hint="curl_cffi not installed — httpx may 504 behind Cloudflare")
            import httpx
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cli:
                for attempt in range(3):
                    r = await cli.get(url, params=params)
                    if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    return r.json()
                log.debug("prefetch.jikan.gave_up", transport="httpx", base=base)
                return None

        # Try each configured base (primary → JIKAN_FALLBACK_URL local instance).
        body = None
        for base in _jikan_bases():
            try:
                body = await _fetch(base)
            except Exception as exc:  # noqa: BLE001 - transport-agnostic; try next base
                log.warning("prefetch.jikan.failed", title=title,
                            base=base, error=str(exc))
                body = None
            if body is not None:
                break
        data = (body or {}).get("data") or []
        if not data:
            log.info("prefetch.jikan.empty", title=title)
            return False
        top = data[0]
        await _write_json(out_dir / "jikan.json", {"query": title, "top": top})
        log.info("prefetch.jikan.ok", title=title,
                 mal_id=top.get("mal_id"), score=top.get("score"))
        return True

    # ── TMDB (metadata + all artwork, mirrored) ────────────────────────────────
    async def _prefetch_tmdb(self, out_dir: Path, title: str,
                             franchise: dict) -> bool:
        tmdb = getattr(self._c, "tmdb", None)
        if tmdb is None:
            return False
        try:
            result = await tmdb.search(title, anime=True)
        except TypeError:
            # Older signature without the anime kwarg.
            result = await tmdb.search(title)
        except Exception as exc:  # noqa: BLE001
            log.warning("prefetch.tmdb.search_failed", title=title, error=str(exc))
            return False
        if result is None:
            return False

        tmdb_id = getattr(result, "id", None)
        media_type = getattr(result, "media_type", "tv")
        payload: dict[str, Any] = {"result": _jsonable(result)}

        # All three asset lists (the same fetchers the thumbnail wizard uses).
        logos = posters = backdrops = []
        try:
            from nekofetch.providers.metadata.tmdb_assets import (
                fetch_backdrops_ranked,
                fetch_logos,
                fetch_posters_ranked,
            )
            if tmdb_id is not None:
                logos, posters, backdrops = await asyncio.gather(
                    fetch_logos(tmdb, tmdb_id, media_type),
                    fetch_posters_ranked(tmdb, tmdb_id, media_type),
                    fetch_backdrops_ranked(tmdb, tmdb_id, media_type),
                    return_exceptions=False,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("prefetch.tmdb.assets_failed", title=title, error=str(exc))
        payload["logos"] = _jsonable(logos)
        payload["posters"] = _jsonable(posters)
        payload["backdrops"] = _jsonable(backdrops)
        await _write_json(out_dir / "tmdb.json", payload)

        # Mirror + locally save every artwork URL, recording which host stuck.
        await self._mirror_assets(out_dir, logos, posters, backdrops)
        return True

    async def _mirror_assets(self, out_dir: Path, logos: list, posters: list,
                             backdrops: list) -> None:
        """Download each artwork once, save it locally, mirror to image hosts.

        Writes ``assets.json`` mapping every source URL → {local, catbox,
        telegraph, imgbb, primary, host}. ``host`` names the first mirror that
        stuck (so a later consumer knows which link is authoritative). The local
        copy is deleted after publish/handoff by :func:`cleanup_local_assets`."""
        from kurosoden.shared.image_backup import backup_bytes

        img_dir = out_dir / "images"
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()

        async def _one(kind: str, asset: dict) -> None:
            url = (asset or {}).get("url")
            if not url or url in seen:
                return
            seen.add(url)
            # Download once.
            blob = mime = None
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60.0,
                                             follow_redirects=True) as cli:
                    r = await cli.get(url)
                    r.raise_for_status()
                    blob = r.content
                    mime = (r.headers.get("content-type")
                            or "image/jpeg").split(";", 1)[0].strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("prefetch.asset.download_failed", url=url,
                            error=str(exc))
                return
            if not blob:
                return
            # Save locally.
            ext = ".png" if mime == "image/png" else ".jpg"
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            local_path = img_dir / f"{kind}_{digest}{ext}"

            def _save() -> None:
                img_dir.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(blob)
            await asyncio.to_thread(_save)
            # Mirror to hosts.
            mirror = await backup_bytes(self._c, blob, mime=mime or "image/jpeg",
                                        source_url=url)
            host = None
            if mirror.imgbb_url:
                host = "imgbb"
            elif mirror.catbox_url:
                host = "catbox"
            elif mirror.telegraph_url:
                host = "telegraph"
            entries.append({
                "kind": kind, "source_url": url,
                "local": str(local_path),
                "catbox": mirror.catbox_url,
                "telegraph": mirror.telegraph_url,
                "imgbb": mirror.imgbb_url,
                "primary": mirror.primary,
                "host": host,
            })

        # Bound concurrency so we don't hammer the hosts (or our own bandwidth).
        sem = asyncio.Semaphore(4)

        async def _guarded(kind: str, asset: dict) -> None:
            async with sem:
                await _one(kind, asset)

        tasks = (
            [_guarded("logo", a) for a in (logos or [])]
            + [_guarded("poster", a) for a in (posters or [])]
            + [_guarded("backdrop", a) for a in (backdrops or [])]
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await _write_json(out_dir / "assets.json", {"images": entries})
        # Tally which hosts worked, for the acceptance log line.
        tally: dict[str, int] = {}
        for e in entries:
            if e["host"]:
                tally[e["host"]] = tally.get(e["host"], 0) + 1
        log.info("prefetch.assets.mirrored", count=len(entries), hosts=tally)


async def cleanup_local_assets(container: Container, code: str,
                               anime_doc_id: str | None = None) -> int:
    """Delete the locally-saved artwork for a request after publish/handoff.

    The hosted links stay in ``assets.json`` (and the DB); only the on-disk
    image bytes are removed. Returns the number of files deleted. The JSON
    metadata files are kept (they're tiny and still useful for updates)."""
    out_dir = metadata_dir(container, code, anime_doc_id)
    img_dir = out_dir / "images"

    def _do() -> int:
        if not img_dir.exists():
            return 0
        n = 0
        for p in img_dir.iterdir():
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        try:
            img_dir.rmdir()
        except Exception:  # noqa: BLE001
            pass
        return n

    n = await asyncio.to_thread(_do)
    log.info("prefetch.cleanup.local_assets", code=code, deleted=n)
    return n


# ── cached-read helpers (consumers prefer these over live fetches) ────────────


async def load_cached(container: Container, code: str, kind: str,
                      anime_doc_id: str | None = None) -> dict | None:
    """Read one cached JSON blob (``anilist`` / ``jikan`` / ``tmdb`` / ``assets``).

    Returns the parsed dict, or ``None`` when the file is absent (the consumer
    then falls back to a live fetch, preserving old behaviour)."""
    path = metadata_dir(container, code, anime_doc_id) / f"{kind}.json"

    def _read() -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    return await asyncio.to_thread(_read)


async def resolve_cached_cover(
    container: Container, code: str, *,
    anilist_id: int | None = None, kind: str | None = None,
    anime_doc_id: str | None = None,
) -> str | None:
    """Return a usable image reference for a prefetched cover, offline-first.

    Resolution order (never hits the network for a cache hit):
      1. AniList cover for ``anilist_id`` — the LOCAL file if it still exists,
         else a hosted backup (imgbb/catbox/telegraph/primary) from
         ``anilist_images.json``.
      2. When ``anilist_id`` is absent, the first AniList cover cached at all.
      3. A TMDB asset of ``kind`` ("poster"/"logo"/"backdrop") from
         ``assets.json`` — local file, else hosted backup.

    Returns a filesystem path or an ``http(s)`` URL (ffmpeg's ``_fit_thumb``
    accepts either), or ``None`` when nothing is cached — the caller then does
    its old live fetch.
    """
    from pathlib import Path as _Path

    def _pick(entry: dict) -> str | None:
        loc = entry.get("local")
        if loc and _Path(loc).exists() and _Path(loc).stat().st_size > 0:
            return loc
        for key in ("imgbb", "catbox", "telegraph", "primary"):
            u = entry.get(key)
            if u:
                return u
        return None

    # 1 & 2: AniList covers.
    covers = await load_cached(container, code, "anilist_images",
                               anime_doc_id=anime_doc_id)
    entries = (covers or {}).get("covers") or []
    if entries:
        if anilist_id is not None:
            for e in entries:
                if e.get("anilist_id") == anilist_id:
                    ref = _pick(e)
                    if ref:
                        return ref
        # Fallback to the first cover with a usable ref (root cover first — it's
        # appended first at mirror time).
        for e in entries:
            ref = _pick(e)
            if ref:
                return ref

    # 3: TMDB asset of the requested kind.
    if kind:
        assets = await load_cached(container, code, "assets",
                                   anime_doc_id=anime_doc_id)
        for e in (assets or {}).get("images") or []:
            if e.get("kind") == kind:
                ref = _pick(e)
                if ref:
                    return ref
    return None


async def load_cached_tmdb_assets(
    container: Container, code: str, asset_type: str,
    anime_doc_id: str | None = None, tmdb_id: int | None = None,
) -> list[dict] | None:
    """Return the prefetched ranked TMDB asset list for a type, or ``None``.

    ``asset_type`` is ``"logo"`` / ``"poster"`` / ``"backdrop"`` (singular, as
    the thumbnail wizard uses); ``tmdb.json`` stores them plural
    (``logos`` / ``posters`` / ``backdrops``). Each element keeps its original
    ``{url, language, width, height, ...}`` shape so a caller can build the
    Telegraph gallery from the cache exactly as it would from a live fetch.
    Returns ``None`` (not ``[]``) on a miss so the caller can tell "cached but
    empty" from "not cached" and fall back to a live fetch only in the latter.

    ``tmdb_id`` guards correctness: the prefetch stores assets for the ROOT
    title only, but a franchise's individual seasons/movies each have their own
    TMDB id. If the caller passes a ``tmdb_id`` that doesn't match the cached
    result's id, we return ``None`` so it fetches that entry's OWN assets live
    (this is what prevents a wrong-installment / wrong-image bug)."""
    blob = await load_cached(container, code, "tmdb", anime_doc_id=anime_doc_id)
    if not blob:
        return None
    if tmdb_id is not None:
        cached_id = (blob.get("result") or {}).get("id")
        if cached_id is not None and int(cached_id) != int(tmdb_id):
            return None
    key = {"logo": "logos", "poster": "posters", "backdrop": "backdrops"}.get(
        asset_type)
    if key is None or key not in blob:
        return None
    assets = blob.get(key)
    return assets if isinstance(assets, list) else None


async def load_cached_jikan(
    container: Container, code: str, *, anime_doc_id: str | None = None,
) -> dict | None:
    """Return the prefetched Jikan (MyAnimeList) top hit, or ``None`` on a miss.

    Reads ``jikan.json`` (written at acceptance by ``_prefetch_jikan``) and
    returns its ``top`` payload — the full MAL record for the best title match
    (``synopsis``, ``score``, ``genres``, ``rating``, …). This is the read-side
    consumer that makes the Jikan prefetch actually useful: callers use it as a
    synopsis/score/genre fallback behind AniList + TMDB. A hit emits a visible
    ``jikan.cache.hit`` log so the operator can confirm the cache is being used.
    """
    blob = await load_cached(container, code, "jikan", anime_doc_id=anime_doc_id)
    top = (blob or {}).get("top") if blob else None
    if top:
        log.info("jikan.cache.hit", code=code,
                 mal_id=top.get("mal_id"), score=top.get("score"))
    return top or None

