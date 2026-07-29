"""Miruro API source.

This source integrates with an operator-controlled Miruro-API instance, not the
Miruro frontend directly. The API is expected to expose the public contract from
``https://github.com/walterwhite-69/Miruro-API``:

    /search?query=...
    /info/{anilist_id}
    /episodes/{anilist_id}
    /watch/{provider}/{anilist_id}/{category}/{slug}

The adapter keeps the extraction boundary outside NekoFetch and only normalizes
the API response into the shared ``AnimeSource`` contract. Downloading itself is
handled by the existing HLS/subtitle/mux pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import AudioType
from nekofetch.sources._hls import (
    RECOMMENDED_LIMITS,
    RECOMMENDED_TIMEOUT,
    download_hls_ts,
    download_subtitles,
    find_ffmpeg,
    list_master_qualities,
    maybe_remux,
)
from nekofetch.sources._mux import assemble_final, audio_label
from nekofetch.sources.base import (
    AnimeDetails,
    AnimeSource,
    AnimeStub,
    Episode,
    ProgressCallback,
    SourceCoverage,
    VideoVariant,
)

log = get_logger(__name__)

DEFAULT_BASE_URL = "http://localhost:8000"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

_HARDSUB_PROVIDERS = {"kiwi", "pahe", "animepahe"}


def _title(media: dict) -> str:
    title = media.get("title") or {}
    if isinstance(title, dict):
        return title.get("english") or title.get("romaji") or title.get("native") or ""
    return str(title or "")


def _image_url(value) -> str | None:
    if isinstance(value, dict):
        return value.get("extraLarge") or value.get("large") or value.get("medium")
    return value if isinstance(value, str) else None


def _anilist_ref(raw: str) -> int | None:
    raw = str(raw or "").strip()
    if raw.startswith("anilist:"):
        raw = raw.split(":", 1)[1]
    try:
        return int(raw)
    except ValueError:
        return None


def _watch_parts(ref: str) -> tuple[str, int, str, str] | None:
    """Parse ``watch/{provider}/{anilist}/{category}/{slug}`` refs."""
    parts = str(ref or "").strip("/").split("/")
    if len(parts) < 5 or parts[0] != "watch":
        return None
    try:
        return parts[1], int(parts[2]), parts[3], "/".join(parts[4:])
    except ValueError:
        return None


def _episode_ref(anilist_id: int, number: int) -> str:
    return f"miruro:{anilist_id}:{number}"


def _episode_ref_parts(ref: str) -> tuple[int, int] | None:
    parts = str(ref or "").strip().split(":")
    if len(parts) != 3 or parts[0] != "miruro":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _episode_number(ep: dict) -> int:
    try:
        return int(float(ep.get("number") or 0))
    except (TypeError, ValueError):
        return 0


def _quality_label(raw: str | None) -> str:
    raw = str(raw or "").strip().lower()
    m = re.search(r"(\d{3,4})", raw)
    return f"{m.group(1)}p" if m else "1080p"


def _stream_url(stream: dict) -> str:
    return stream.get("url") or stream.get("file") or stream.get("src") or ""


def _stream_headers(stream: dict, fallback_referer: str) -> dict:
    referer = str(stream.get("referer") or fallback_referer)
    headers = {
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }
    custom = stream.get("headers")
    if isinstance(custom, dict):
        headers.update({str(k): str(v) for k, v in custom.items()})
    return headers


# Track "kinds" that are NOT real caption tracks — hi-anime/zoro-style APIs put a
# preview-sprite VTT in the same subtitles array as the captions. Embedding one as
# a subtitle is wrong, and its mere presence would make a raw stream look soft-subbed.
_NON_CAPTION_KINDS = {"thumbnails", "thumbnail", "sprite", "sprites", "storyboard", "preview"}


def _subtitle_pairs(items: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = item.get("file") or item.get("url") or item.get("src")
        if not url:
            continue
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        label = item.get("label") or item.get("lang") or item.get("language") or "Subtitle"
        # Drop non-caption tracks by kind AND by label (some feeds omit `kind`
        # and only name the track "thumbnails").
        if kind in _NON_CAPTION_KINDS or str(label).strip().lower() in _NON_CAPTION_KINDS:
            continue
        out.append((str(label), str(url)))
    return out


def _variant_info(variant: VideoVariant) -> dict:
    try:
        data = json.loads(variant.source_ref)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_hard_sub_variant(variant: VideoVariant) -> bool:
    return bool(_variant_info(variant).get("hard_sub"))


def _is_soft_sub_variant(variant: VideoVariant) -> bool:
    info = _variant_info(variant)
    return (
        variant.audio == AudioType.SUBBED
        and not info.get("hard_sub")
        and bool(info.get("subtitles"))
    )


def _count_audio_coverage(data: dict) -> tuple[int, int, int, int]:
    providers = data.get("providers", {}) if isinstance(data, dict) else {}
    total: set[int] = set()
    subbed: set[int] = set()
    dubbed: set[int] = set()

    for payload in providers.values():
        ep_groups = payload.get("episodes", {}) if isinstance(payload, dict) else {}
        if isinstance(ep_groups, list):
            ep_groups = {"sub": ep_groups}
        for category, bucket in ep_groups.items():
            for ep in bucket or []:
                if not isinstance(ep, dict):
                    continue
                number = _episode_number(ep)
                if number <= 0:
                    continue
                total.add(number)
                if category == "dub":
                    dubbed.add(number)
                else:
                    subbed.add(number)

    return len(total), len(subbed), len(dubbed), len(subbed & dubbed)


class MiruroSource(AnimeSource):
    """Source adapter for a self-hosted Miruro-API instance."""

    name = "miruro"

    def __init__(
        self,
        base_url: str | dict | None = None,
        api_base_url: str | None = None,
        provider_order: list[str] | None = None,
        stream_referer: str | None = None,
    ) -> None:
        if isinstance(base_url, dict):
            config = base_url
            base_url = config.get("api_base_url") or config.get("base_url")
            provider_order = config.get("provider_order") or provider_order
            stream_referer = config.get("stream_referer") or stream_referer
        elif api_base_url:
            base_url = api_base_url
        if isinstance(provider_order, str):
            provider_order = [p.strip() for p in provider_order.split(",") if p.strip()]

        self.base_url = (
            base_url or os.getenv("MIRURO_API_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.stream_referer = (
            stream_referer or os.getenv("MIRURO_STREAM_REFERER") or self.base_url
        ).rstrip("/") + "/"
        self.provider_order = provider_order or ["kiwi", "arc", "zoro", "hop", "pahe"]
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=RECOMMENDED_TIMEOUT,
                limits=RECOMMENDED_LIMITS,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def search(self, query: str) -> list[AnimeStub]:
        if query.startswith("anilist:"):
            aid = _anilist_ref(query)
            if aid is None:
                return []
            try:
                info = await self._get_json(f"/info/{aid}")
            except httpx.HTTPError:
                return []
            return [self._stub(info)] if info else []

        try:
            data = await self._get_json(
                "/search",
                params={"query": query, "page": 1, "per_page": 20},
            )
        except httpx.HTTPError as exc:
            log.warning("miruro.search.failed", query=query, error=str(exc))
            return []
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            log.warning("miruro.search.bad_response", query=query, data_type=type(data).__name__)
            return []
        stubs = [self._stub(row) for row in rows if row.get("id")]
        if not stubs:
            log.info("miruro.search.no_results", query=query, raw_count=len(rows))
        return stubs

    async def get_details(self, source_ref: str) -> AnimeDetails:
        aid = _anilist_ref(source_ref)
        if aid is None:
            raise ValueError(f"invalid Miruro source_ref: {source_ref!r}")
        info = await self._get_json(f"/info/{aid}")
        title = _title(info) or str(aid)
        start = info.get("startDate") or {}
        release_date = None
        if isinstance(start, dict) and start.get("year"):
            release_date = "-".join(
                str(start.get(k, "")).zfill(2)
                for k in ("year", "month", "day") if start.get(k)
            )
        studios = (
            info.get("studios", {}).get("nodes", [])
            if isinstance(info.get("studios"), dict)
            else []
        )
        studio = next(
            (s.get("name") for s in studios if isinstance(s, dict) and s.get("name")),
            None,
        )
        title_data = info.get("title") or {}
        return AnimeDetails(
            source_ref=str(aid),
            title=title,
            alt_titles=[
                t for t in [
                    title_data.get("romaji") if isinstance(title_data, dict) else None,
                    title_data.get("native") if isinstance(title_data, dict) else None,
                    *list(info.get("synonyms") or []),
                ] if t and t != title
            ],
            synopsis=re.sub(r"<[^>]+>", "", info.get("description") or "").strip() or None,
            genres=list(info.get("genres") or []),
            studio=studio,
            release_date=release_date,
            poster_url=_image_url(info.get("coverImage")),
            banner_url=info.get("bannerImage"),
            season_count=1,
            episode_count=int(info.get("episodes") or 0),
        )

    async def get_episodes(self, source_ref: str) -> list[Episode]:
        aid = _anilist_ref(source_ref)
        if aid is None:
            raise ValueError(f"invalid Miruro source_ref: {source_ref!r}")
        data = await self._get_json(f"/episodes/{aid}")
        providers = data.get("providers", {}) if isinstance(data, dict) else {}
        episodes: dict[int, Episode] = {}
        ordered_providers = sorted(
            providers.items(),
            key=lambda kv: self.provider_order.index(kv[0])
            if kv[0] in self.provider_order else len(self.provider_order),
        )
        for _provider, payload in ordered_providers:
            ep_groups = payload.get("episodes", {}) if isinstance(payload, dict) else {}
            if isinstance(ep_groups, list):
                ep_groups = {"sub": ep_groups}
            for category in ("sub", "dub", "hsub"):
                for ep in ep_groups.get(category, []) or []:
                    if not isinstance(ep, dict) or not ep.get("id"):
                        continue
                    number = _episode_number(ep)
                    if number <= 0 or number in episodes:
                        continue
                    episodes[number] = Episode(
                        source_ref=_episode_ref(aid, number),
                        season=1,
                        number=number,
                        title=ep.get("title") or None,
                    )
        return [episodes[n] for n in sorted(episodes)]

    async def get_variants(self, episode_ref: str) -> list[VideoVariant]:
        watch_refs = await self._watch_refs_for_episode(episode_ref)
        if not watch_refs:
            return []

        variants: list[VideoVariant] = []
        seen: set[tuple[str, AudioType, str]] = set()
        for watch_ref in watch_refs:
            for variant in await self._variants_for_watch_ref(watch_ref):
                key = (variant.resolution, variant.audio, variant.source_ref)
                if key in seen:
                    continue
                seen.add(key)
                variants.append(variant)
        variants.sort(key=lambda v: (
            0 if _is_soft_sub_variant(v)
            else 1 if v.audio == AudioType.DUBBED
            else 2
        ))
        return variants

    async def _watch_refs_for_episode(self, episode_ref: str) -> list[str]:
        if _watch_parts(episode_ref):
            return [episode_ref.strip("/")]

        parts = _episode_ref_parts(episode_ref)
        if parts is None:
            return []
        anilist_id, episode_number = parts
        try:
            data = await self._get_json(f"/episodes/{anilist_id}")
        except httpx.HTTPError:
            return []

        providers = data.get("providers", {}) if isinstance(data, dict) else {}
        refs: list[str] = []
        seen: set[str] = set()
        ordered_providers = sorted(
            providers.items(),
            key=lambda kv: self.provider_order.index(kv[0])
            if kv[0] in self.provider_order else len(self.provider_order),
        )
        for _provider, payload in ordered_providers:
            ep_groups = payload.get("episodes", {}) if isinstance(payload, dict) else {}
            if isinstance(ep_groups, list):
                ep_groups = {"sub": ep_groups}
            for category in ("sub", "dub", "hsub"):
                for ep in ep_groups.get(category, []) or []:
                    if not isinstance(ep, dict) or _episode_number(ep) != episode_number:
                        continue
                    ref = str(ep.get("id") or "").strip("/")
                    if ref and ref not in seen:
                        seen.add(ref)
                        refs.append(ref)
        return refs

    async def _variants_for_watch_ref(self, episode_ref: str) -> list[VideoVariant]:
        parts = _watch_parts(episode_ref)
        if parts is None:
            return []
        provider, anilist_id, category, _slug = parts
        try:
            data = await self._get_json(f"/{episode_ref.strip('/')}")
        except httpx.HTTPError:
            data = {}

        # Documented fallback: when the direct watch endpoint yields no streams
        # (transient upstream miss), retry the manual /sources endpoint with the
        # decomposed ref. See Miruro-API README "Fallback endpoint for manual control".
        if not (data.get("streams") or data.get("sources")):
            try:
                data = await self._get_json(
                    "/sources",
                    params={
                        "episodeId": episode_ref.strip("/"),
                        "provider": provider,
                        "anilistId": anilist_id,
                        "category": category,
                    },
                ) or data
            except httpx.HTTPError:
                pass

        streams = data.get("streams") or data.get("sources") or []
        if isinstance(streams, dict):
            streams = [streams]
        subtitles = _subtitle_pairs(data.get("subtitles") or data.get("tracks") or [])
        variants: list[VideoVariant] = []
        audio = AudioType.DUBBED if category == "dub" else AudioType.SUBBED
        hard_sub = (
            category == "hsub"
            or provider.lower() in _HARDSUB_PROVIDERS
            or (category != "dub" and not subtitles)
        )

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            url = _stream_url(stream)
            if not url:
                continue
            stream_type = str(stream.get("type") or "").lower()
            if stream_type == "embed":
                continue
            if ".m3u8" not in url and not re.search(r"\.(mp4|mkv|webm)(?:\?|$)", url, re.I):
                continue
            headers = _stream_headers(stream, self.stream_referer)
            qualities = [_quality_label(stream.get("quality"))]
            if ".m3u8" in url:
                probed = await list_master_qualities(self.http, url, headers)
                if probed:
                    qualities = [f"{q}p" for q in probed]

            for quality in qualities:
                variants.append(VideoVariant(
                    source_ref=json.dumps({
                        "episode_ref": episode_ref.strip("/"),
                        "provider": provider,
                        "anilist_id": anilist_id,
                        "category": category,
                        "stream": url,
                        "headers": headers,
                        "quality": quality,
                        "subtitles": [] if hard_sub else subtitles,
                        "hard_sub": hard_sub,
                        "intro": data.get("intro"),
                        "outro": data.get("outro"),
                    }),
                    resolution=quality,
                    audio=audio,
                    languages=["english" if audio == AudioType.DUBBED else "japanese"],
                    subtitles=[] if hard_sub else [s[0] for s in subtitles],
                    container="mkv",
                ))
        return variants

    async def dual_audio_plan(self, episode_ref: str, resolution: str | None = None) -> dict:
        """Check whether Miruro can build a true dual-audio file for one episode.

        Only soft-subbed streams are eligible for the Japanese/sub side. Hard-sub
        video may still be downloaded as a fallback, but it is not merge material.
        """
        from nekofetch.sources._dualaudio import are_mergeable, playlist_duration

        variants = await self.get_variants(episode_ref)
        if resolution:
            variants = [v for v in variants if v.resolution == resolution]

        sub = next(
            (
                v for v in variants
                if _is_soft_sub_variant(v)
            ),
            None,
        )
        dub = next((v for v in variants if v.audio == AudioType.DUBBED), None)
        if not sub or not dub:
            return {
                "feasible": False,
                "mergeable": False,
                "reason": "missing soft sub or dub",
                "sub_variant": sub,
                "dub_variant": dub,
            }

        async def _dur(v: VideoVariant) -> float | None:
            info = _variant_info(v)
            url = str(info.get("stream") or "")
            if ".m3u8" not in url:
                return None
            headers = info.get("headers") or _stream_headers({}, self.stream_referer)
            return await playlist_duration(self.http, url, headers)

        d_sub = await _dur(sub)
        d_dub = await _dur(dub)
        return {
            "feasible": True,
            "mergeable": are_mergeable(d_sub, d_dub),
            "sub_variant": sub,
            "dub_variant": dub,
            "sub_dur": d_sub,
            "dub_dur": d_dub,
        }

    async def coverage(self, *titles: str) -> SourceCoverage | None:
        candidates = [t for t in titles if t]
        if not candidates:
            return SourceCoverage(
                source=self.name, matched_title="", source_ref="", available=False,
            )
        stub = None
        searched = ""
        for query in candidates:
            searched = query
            hits = await self.search(query)
            if hits:
                stub = hits[0]
                break
        if not stub:
            log.warning("miruro.coverage.no_match", candidates=candidates)
            return SourceCoverage(
                source=self.name, matched_title=searched, source_ref="",
                available=False, note="no confident match",
            )
        try:
            data = await self._get_json(f"/episodes/{stub.source_ref}")
            total, subbed, dubbed, dual = _count_audio_coverage(data)
        except Exception as exc:  # noqa: BLE001
            return SourceCoverage(
                source=self.name, matched_title=stub.title, source_ref=stub.source_ref,
                available=False, note=str(exc)[:120],
            )
        if not total:
            try:
                total = len(await self.get_episodes(stub.source_ref))
                subbed = total
            except Exception:  # noqa: BLE001
                pass
        return SourceCoverage(
            source=self.name,
            matched_title=stub.title,
            source_ref=stub.source_ref,
            total_episodes=total,
            seasons=1,
            sub_episodes=subbed,
            dub_episodes=dubbed,
            dual_episodes=dual,
            approximate=True,
            available=bool(total),
        )

    async def download(
        self,
        variant: VideoVariant,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        resume_state: dict | None = None,
    ) -> dict:
        from nekofetch.sources._diagnostics import classify

        info = json.loads(variant.source_ref)
        url = info["stream"]
        headers = info.get("headers") or _stream_headers({}, self.stream_referer)
        quality = info.get("quality", variant.resolution).rstrip("p")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if on_progress:
            await on_progress(0, 1)

        try:
            if ".m3u8" in url:
                video = await download_hls_ts(
                    self.http, url, headers, quality, dest.with_name(f".{dest.stem}.video"),
                    on_progress,
                )
            else:
                video = await self._download_direct(url, headers, dest, on_progress)
        except Exception as exc:
            kind, reason = classify(exc)
            raise RuntimeError(
                f"miruro stream failed ({info.get('provider')}/{info.get('category')}): "
                f"{kind.value} - {reason}"
            ) from exc

        sub_info: list[dict] = []
        sub_tracks: list[tuple[str, str, Path]] = []
        if info.get("subtitles"):
            sub_info = await download_subtitles(self.http, info["subtitles"], headers, dest)
            for sub in sub_info:
                if sub.get("saved"):
                    label = str(sub.get("label") or "Subtitle")
                    lang_m = re.search(r"\b([a-z]{2,3}(?:-[A-Z]{2})?)\b", label)
                    sub_tracks.append((
                        label,
                        lang_m.group(1) if lang_m else "und",
                        Path(sub["saved"]),
                    ))

        audio_lang = "en" if variant.audio == AudioType.DUBBED else "ja"
        audio_name = "English" if audio_lang == "en" else "Japanese"
        warnings: list[str] = []
        if find_ffmpeg():
            try:
                out, sub_meta = await assemble_final(
                    video, [], sub_tracks, dest,
                    title=f"{dest.stem} [{audio_label([audio_lang])}]",
                    embedded_audio=(audio_name, audio_lang),
                )
                sub_info = sub_meta or sub_info
            except Exception as exc:  # noqa: BLE001
                log.warning("miruro.mux.failed", error=str(exc))
                out = maybe_remux(video, dest)
                warnings.append(f"mux failed: {exc}")
        else:
            out = maybe_remux(video, dest)
            warnings.append("ffmpeg not found - saved video-only stream")

        total = out.stat().st_size
        if on_progress:
            await on_progress(total, total)
        sha = hashlib.sha256()
        sha.update(out.read_bytes())
        return {
            "checksum": sha.hexdigest(),
            "bytes": total,
            "complete": True,
            "container": out.suffix.lstrip("."),
            "provider": info.get("provider"),
            "category": info.get("category"),
            "label": audio_label([audio_lang]),
            "subtitles": sub_info,
            "hard_sub": bool(info.get("hard_sub")),
            "warnings": warnings,
        }

    async def _download_direct(
        self, url: str, headers: dict, dest: Path, on_progress: ProgressCallback | None
    ) -> Path:
        out = dest.with_suffix(Path(url.split("?", 1)[0]).suffix or ".mp4")
        total = 0
        async with self.http.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            expected = int(resp.headers.get("content-length", 0))
            with out.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    total += len(chunk)
                    if on_progress and expected:
                        await on_progress(total, expected)
        if total == 0:
            raise RuntimeError("direct download produced an empty file")
        return out

    async def _get_json(self, path: str, *, params: dict | None = None) -> dict:
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        resp = await self.http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"results": data}

    def _stub(self, row: dict) -> AnimeStub:
        return AnimeStub(
            source_ref=str(row.get("id")),
            title=_title(row) or str(row.get("id")),
            poster_url=_image_url(row.get("coverImage")) or row.get("poster"),
            year=row.get("seasonYear") or row.get("year"),
        )
