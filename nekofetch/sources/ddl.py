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

import hashlib
import json
from pathlib import Path

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
                timeout=httpx.Timeout(60.0, read=None),
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

    async def _fetch_archive(self, url: str, dest: Path) -> Path:
        """Stream a remote archive to ``dest`` (skips the download if already cached)."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with self.http.stream("GET", url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(tmp, "wb") as fh:
                async for chunk in resp.aiter_bytes(_CHUNK):
                    await fh.write(chunk)
        tmp.replace(dest)
        return dest

    async def get_episodes(self, source_ref: str) -> list[Episode]:
        """Download + extract every provided archive, order EP1..EPN across them."""
        ref = _parse_ref(source_ref)
        archives = ref.get("archives") or []
        title = ref.get("title", "")
        cache = self._cache_root(ref)

        pooled: list[dict] = []
        for idx, arc in enumerate(archives):
            url = arc.get("url")
            if not url:
                continue
            season_hint = arc.get("season")  # positional fallback for Phase 4/5
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            archive_path = cache / f"{digest}{_ext_of(url)}"
            extract_dir = cache / digest
            try:
                await self._fetch_archive(url, archive_path)
                videos = await extract_archive(archive_path, extract_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("ddl.archive.failed", url=url, error=str(exc))
                continue
            for vp in videos:
                pooled.append({
                    "path": str(vp),
                    "name": vp.name,
                    "length": vp.stat().st_size,
                    "index": len(pooled) + 1,
                    "_season_hint": season_hint,
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
