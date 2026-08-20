"""DDL source — direct-download-link archives (zip / rar / 7z).

Unlike the streaming sources, DDL is admin-driven: the operator pastes a direct
link to an archive. The flow mirrors the torrent source, minus the peer layer:

  episodes ──> stream the archive to a cache dir, extract it (7-Zip / stdlib zip),
               parse the per-release naming pattern and order EP1..EPN across every
               archive provided (multi-season packs are supported).
  variants ──> one variant per extracted file; resolution/audio parsed from the
               name. EVERY provided quality is kept (no 1080p-only collapse) so the
               pipeline downloads each tier and only encodes what's genuinely missing.
  download ──> a resumable chunked copy of the already-extracted local file, exactly
               like the local library source.

``source_ref`` is JSON: ``{"archives": [{"url": …, "season": null}, …],
"title": …, "code": …}``. The ``archives`` list is what lets one request accumulate
several links (e.g. one archive per season); ``code`` (optional) scopes the extract
cache under ``work/<code>/.ddl`` so it's cleaned up with the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiofiles
import httpx

from nekofetch.core.config import get_env
from nekofetch.core.exceptions import NotFound
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import AudioType
from nekofetch.sources._archive import extract_archive
from nekofetch.sources._torrent import group_variants, order_episodes
from nekofetch.sources.base import (
    AnimeDetails,
    AnimeSource,
    AnimeStub,
    Episode,
    ProgressCallback,
    VideoVariant,
)

log = get_logger(__name__)

_CHUNK = 1024 * 1024  # 1 MiB stream/copy chunk


def _audio_from_name(name: str) -> AudioType:
    """Best-effort audio classification from a filename (reuses nyaa's classifier)."""
    from nekofetch.sources.nyaa import classify_audio

    kind = classify_audio(name)["audio"]  # 'dual' | 'multi' | 'single'
    if kind == "multi":
        return AudioType.MULTI
    if kind == "dual":
        return AudioType.DUAL_AUDIO
    return AudioType.SUBBED


class DdlSource(AnimeSource):
    name = "ddl"

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                # A real READ timeout is essential: worker.dev links stall
                # mid-stream, and read=None (the old value) hangs the whole job
                # forever. 90s between chunks is generous; a stall raises
                # ReadTimeout → _fetch_archive retries, then fails cleanly.
                timeout=httpx.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── AnimeSource interface (search/get_details are thin — DDL is admin-driven) ──
    async def search(self, query: str) -> list[AnimeStub]:
        return []

    async def get_details(self, source_ref: str) -> AnimeDetails:
        ref = _parse_ref(source_ref)
        return AnimeDetails(source_ref=source_ref, title=ref.get("title", "DDL release"))

    # ── cache location ──
    def _cache_root(self, ref: dict) -> Path:
        code = ref.get("code")
        base = get_env().storage_path / "work"
        root = (base / str(code) / ".ddl") if code else (base / ".ddl_cache")
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def _fetch_archive(self, url: str, dest: Path, *, on_bytes=None,
                             total_hint: int = 0) -> Path:
        """Download a remote archive to ``dest`` (skips if already cached).

        ``on_bytes(done, total)`` reports byte progress. ``total_hint`` is a
        pre-resolved content length (from the caller's redirect-following HEAD) so
        we don't issue a second HEAD here. PRIMARY path is aria2c (16 Range
        connections + per-piece timeout + retry + resume) — the technique the
        Leech bot uses to pull these MoviesMod ``workers.dev`` links reliably; a
        naive single-socket stream gets throttled/cut by the Cloudflare worker and
        stalls. Falls back to a resumable httpx stream only when aria2c isn't
        installed.
        """
        if dest.exists() and dest.stat().st_size > 0:
            if on_bytes:
                size = dest.stat().st_size
                await on_bytes(size, size)
            return dest

        # ── Primary: aria2c multi-connection (robust on worker.dev) ──
        from nekofetch.sources._torrentdl import download_http_file, find_aria2

        if find_aria2() is not None:
            total = total_hint or await self._content_length(url)
            try:
                await download_http_file(
                    url, dest, total_bytes=total, on_progress=on_bytes,
                )
                return dest
            except Exception as exc:  # noqa: BLE001 — fall back to httpx below
                log.warning("ddl.aria2.failed", url=url, error=str(exc)[:200])

        # ── Fallback: resumable httpx stream (with a real read timeout) ──
        tmp = dest.with_suffix(dest.suffix + ".part")
        attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self.http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    done = 0
                    async with aiofiles.open(tmp, "wb") as fh:
                        if on_bytes:
                            await on_bytes(0, total)
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            await fh.write(chunk)
                            done += len(chunk)
                            if on_bytes:
                                await on_bytes(done, total)
                tmp.replace(dest)
                return dest
            except Exception as exc:  # noqa: BLE001 — retry transient host flakiness
                last_exc = exc
                log.warning("ddl.fetch.retry", url=url, attempt=attempt,
                            error=str(exc)[:200])
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt < attempts:
                    await asyncio.sleep(1.5 * attempt)
        raise last_exc or RuntimeError(f"failed to download {url}")

    async def _content_length(self, url: str) -> int:
        """Best-effort total size for the progress bar (HEAD, then a ranged GET)."""
        size, _name = await self._head_meta(url)
        return size

    async def _head_meta(self, url: str) -> tuple[int, str | None]:
        """Resolve ``(content_length, filename)`` in a single redirect-following
        HEAD. The client has ``follow_redirects=True``, so ``r.url`` is the FINAL
        URL after a shortener 302 (``flyn.im/9pYDxXE`` → ``…/A.Couple.Of.Cuckoos
        .S01…zip``); the real name comes from ``Content-Disposition`` first, else
        the final URL's basename — never the shortener tail. Size falls back to a
        ranged GET; name falls back to ``None`` (caller keeps its own default)."""
        size = 0
        name: str | None = None
        try:
            r = await self.http.head(url)
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                size = int(cl)
            name = _name_from_disposition(r.headers.get("content-disposition"))
            if not name:
                # Final URL after redirects — its path holds the real archive name.
                final = _archive_name(str(r.url))
                name = final if final != "archive.zip" else None
        except Exception:  # noqa: BLE001
            pass
        if size == 0:
            try:
                async with self.http.stream("GET", url) as resp:
                    cl = resp.headers.get("content-length")
                    size = int(cl) if cl and cl.isdigit() else 0
                    if not name:
                        name = _name_from_disposition(
                            resp.headers.get("content-disposition"))
                        if not name:
                            final = _archive_name(str(resp.url))
                            name = final if final != "archive.zip" else None
            except Exception:  # noqa: BLE001
                pass
        return size, name

    async def get_episodes(self, source_ref: str, *, on_progress=None) -> list[Episode]:
        """Download + extract every provided archive, order EP1..EPN across them.

        Two visible phases (owner-requested), reported via the optional async
        ``on_progress(info)`` callback so the live card shows them BEFORE the
        naming prompt:
          1. DOWNLOAD every archive — one transfer bar per archive (per quality).
          2. EXTRACT every downloaded archive — per-FILE progress.
        ``info`` = ``{stage, archive_name, index, count, done, total}`` where
        ``stage`` is ``download`` | ``extract`` | ``extract_done`` | ``failed``.
        Best-effort: a progress hiccup never affects the result.
        """
        ref = _parse_ref(source_ref)
        archives = [a for a in (ref.get("archives") or []) if a.get("url")]
        title = ref.get("title", "")
        cache = self._cache_root(ref)
        count = len(archives)

        async def _emit(stage: str, archive_name: str, index: int,
                        done: int = 0, total: int = 0) -> None:
            if on_progress is None:
                return
            try:
                await on_progress({
                    "stage": stage, "archive_name": archive_name,
                    "index": index, "count": count, "done": done, "total": total,
                })
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

        # ── Phase 1: download every archive (one transfer bar per archive) ──
        downloaded: list[dict] = []  # {arc_name, path, dir, season_hint}
        for pos, arc in enumerate(archives, start=1):
            url = arc["url"]
            # Resolve the REAL archive name (and size) up front via one
            # redirect-following HEAD, so the card shows "A.Couple.Of.Cuckoos
            # .S01…zip" from the first tick — not the shortener tail "9pYDxXE".
            size_hint, resolved_name = await self._head_meta(url)
            arc_name = resolved_name or _archive_name(url)
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            archive_path = cache / f"{digest}{_ext_of(url)}"

            async def _on_bytes(done, total, an=arc_name, i=pos):
                await _emit("download", an, i, done, total)

            try:
                await self._fetch_archive(
                    url, archive_path, on_bytes=_on_bytes, total_hint=size_hint,
                )
            except Exception as exc:  # noqa: BLE001 — a dead link shouldn't kill the rest
                log.warning("ddl.download.failed", url=url, error=str(exc))
                await _emit("failed", f"{arc_name}: {exc}", pos)
                continue
            downloaded.append({
                "arc_name": arc_name, "path": archive_path,
                "dir": cache / digest, "season_hint": arc.get("season"),
            })

        # ── Phase 2: extract every downloaded archive (per-file progress) ──
        pooled: list[dict] = []
        for pos, item in enumerate(downloaded, start=1):
            arc_name = item["arc_name"]
            # Track the real per-file counts the extractor reports so the FINAL
            # "extract_done" frame shows the true episode total (e.g. 24/24) — not
            # a hardcoded 1/1, which used to render every season as "File 1/1".
            last_count = {"done": 0, "total": 0}

            async def _on_file(done, total, name="", an=arc_name, i=pos,
                               _c=last_count):
                _c["done"], _c["total"] = int(done or 0), int(total or 0)
                await _emit("extract", an, i, done, total)

            try:
                await _emit("extract", arc_name, pos, 0, 0)
                videos = await extract_archive(
                    item["path"], item["dir"], on_file=_on_file,
                )
                # Prefer the extracted video count; fall back to the last poller
                # frame. Both beat the old hardcoded (1, 1).
                final_total = len(videos) or last_count["total"] or 1
                await _emit("extract_done", arc_name, pos, final_total, final_total)
            except Exception as exc:  # noqa: BLE001
                log.warning("ddl.archive.failed", archive=arc_name, error=str(exc))
                await _emit("failed", f"{arc_name}: {exc}", pos)
                continue
            for vp in videos:
                pooled.append({
                    "path": str(vp),
                    "name": vp.name,
                    "length": vp.stat().st_size,
                    "index": len(pooled) + 1,
                    "_season_hint": item["season_hint"],
                })

        if not pooled:
            return []

        # Keep EVERY quality (no prefer_resolution collapse) and group per real
        # (season, episode) so a multi-quality pack becomes ONE episode with a
        # sibling file per tier — get_variants expands them, the worker downloads
        # each, and EncodeStage fills only genuinely-missing tiers.
        ordered = order_episodes(pooled)
        groups = group_variants(ordered)
        episodes: list[Episode] = []
        for g in groups:
            primary = g["files"][0]  # highest resolution
            audio = _audio_from_name(f"{title} {primary['name']}")
            episodes.append(
                Episode(
                    source_ref=json.dumps({
                        "files": g["files"],
                        # Back-compat single-file fields (primary = highest res).
                        "path": primary["path"],
                        "name": primary["name"],
                        "length": primary["length"],
                        "resolution": primary.get("resolution"),
                        "audio_kind": audio.value,
                        "season": g["season"],
                        "episode": g["episode"],
                        "kind": g["kind"],
                        # Whether the filename stated a season — the post-extract
                        # franchise mapping trusts an explicit S02 over re-deriving.
                        "season_explicit": bool(g.get("season_explicit")),
                    }),
                    season=g["season"],
                    number=g["number"],
                    title=primary["name"],
                )
            )
        return episodes

    async def get_variants(self, episode_ref: str) -> list[VideoVariant]:
        """One VideoVariant per resolution this episode ships. Each variant's ref
        points at its OWN extracted file so ``download`` copies the right one."""
        e = _parse_ref(episode_ref)
        kind = e.get("audio_kind")
        audio = {
            "multi": AudioType.MULTI,
            "dual_audio": AudioType.DUAL_AUDIO,
            "dubbed": AudioType.DUBBED,
        }.get(kind, AudioType.SUBBED)
        base = {k: v for k, v in e.items() if k != "files"}
        files = e.get("files") or [{
            "path": e.get("path"), "name": e.get("name"),
            "length": e.get("length"), "resolution": e.get("resolution"),
        }]
        variants: list[VideoVariant] = []
        for f in files:
            ref = {
                **base,
                "path": f.get("path"),
                "name": f.get("name"),
                "length": f.get("length"),
                "resolution": f.get("resolution"),
            }
            variants.append(
                VideoVariant(
                    source_ref=json.dumps(ref),
                    resolution=f.get("resolution") or "1080p",
                    audio=audio,
                    container=Path(f.get("name") or "").suffix.lstrip("."),
                    size_bytes=f.get("length"),
                )
            )
        return variants

    async def download(
        self,
        variant: VideoVariant,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        resume_state: dict | None = None,
    ) -> dict:
        """Resumable chunked copy from the extracted archive file into the work store."""
        info = _parse_ref(variant.source_ref)
        src = Path(info["path"])
        if not src.exists():
            raise NotFound(f"Extracted file missing: {src}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        total = src.stat().st_size
        already = dest.stat().st_size if (resume_state and dest.exists()) else 0
        if already > total:
            already = 0  # stale/oversized partial — start clean

        sha = hashlib.sha256()
        mode = "ab" if already else "wb"
        async with aiofiles.open(src, "rb") as fsrc, aiofiles.open(dest, mode) as fdst:
            if already:
                await fsrc.seek(already)
            written = already
            while True:
                data = await fsrc.read(_CHUNK)
                if not data:
                    break
                await fdst.write(data)
                sha.update(data)
                written += len(data)
                if on_progress:
                    await on_progress(written, total)

        complete = written >= total
        checksum: str | None = None
        if complete:
            if already:
                # Resumed copy only hashed this run's bytes — re-hash the whole file.
                full = hashlib.sha256()
                async with aiofiles.open(dest, "rb") as fv:
                    while True:
                        block = await fv.read(_CHUNK)
                        if not block:
                            break
                        full.update(block)
                checksum = full.hexdigest()
            else:
                checksum = sha.hexdigest()
        return {
            "path": str(dest),
            "name": dest.name,
            "bytes": written,
            "checksum": checksum,
            "complete": complete,
        }


def _parse_ref(ref: str) -> dict:
    try:
        return json.loads(ref)
    except (TypeError, json.JSONDecodeError):
        return {}


def _ext_of(url: str) -> str:
    """Archive extension from a URL path (defaults to .zip when absent)."""
    low = url.split("?", 1)[0].lower()
    for ext in (".zip", ".rar", ".7z"):
        if low.endswith(ext):
            return ext
    return ".zip"


def _archive_name(url: str) -> str:
    """Human archive filename from a URL for the progress card (the ZIP name).

    Worker links bury the real name in the path (…/<hash>/Akudama.Drive.S01.480p…
    .zip); take the last path segment, URL-decode it, and fall back to the host."""
    try:
        path = urlparse(url).path
    except Exception:  # noqa: BLE001
        path = url
    name = unquote((path or "").rstrip("/").split("/")[-1])
    return name or "archive.zip"


def _name_from_disposition(disposition: str | None) -> str | None:
    """Extract a filename from a ``Content-Disposition`` header, if present.

    Handles both ``filename="…"`` and RFC 5987 ``filename*=UTF-8''…`` forms.
    Returns None when the header is absent or carries no usable name — the caller
    then falls back to the final-URL basename."""
    if not disposition:
        return None
    # RFC 5987 extended form takes precedence (it carries the real UTF-8 name).
    m = re.search(r"filename\*\s*=\s*(?:[\w-]+'')?([^;\r\n]+)", disposition, re.I)
    if not m:
        m = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', disposition, re.I)
    if not m:
        return None
    name = unquote(m.group(1).strip().strip('"')).strip()
    # Guard against a path/traversal sneaking in via the header.
    name = name.replace("\\", "/").rstrip("/").split("/")[-1]
    return name or None
