"""Concrete processing stages.

External media tooling (ffmpeg / mkvpropedit) is invoked via subprocess and guarded by
feature toggles, so the pipeline runs even where a tool or capability is unavailable —
it simply records a note and moves on rather than failing the whole job.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import ProcessingStage
from nekofetch.services.branding_service import BrandingService
from nekofetch.services.processing.base import Stage, StageContext
from nekofetch.ui import templates

log = get_logger(__name__)


# ── Title shortening ───────────────────────────────────────────────────────────
#
# For filenames, long titles are shortened to an acronym. First we check the
# request's ``franchise_data`` for AniList synonyms — widely-recognised short
# alternatives (e.g. ``Tensura`` for Slime, ``Roshidere`` for Alya). If none
# are suitable, we fall back to generating an acronym by stripping filler words
# and taking the first letter of each remaining word.

_FILLER_WORDS = frozenset({
    "the", "of", "a", "an", "and", "in", "to", "is", "for",
    "with", "on", "at", "by", "its", "my", "no", "or", "as",
})


# A subtitle divider splits a "main title" from its tagline. Anime titles very
# often carry a recognisable short name before the divider and a flavour subtitle
# after it: "Tsukimichi: Moonlit Fantasy", "Mushoku Tensei ~Jobless~",
# "Kaguya-sama - Love is War". The part before the FIRST divider is usually the
# name people actually search for, so we prefer it for filenames.
_SUBTITLE_DIVIDERS = (":", "：", " - ", " ~ ", " – ", " — ", "~", "|")


def _main_title(title: str) -> str:
    """Return the segment before the first subtitle divider (or the whole title).

    "Tsukimichi: Moonlit Fantasy"          → "Tsukimichi"
    "Mushoku Tensei ~Jobless Reincarnation~"→ "Mushoku Tensei"
    "Re:Zero kara Hajimeru Isekai Seikatsu"→ unchanged (leading part too short)
    """
    best = title
    for div in _SUBTITLE_DIVIDERS:
        idx = title.find(div)
        # Require at least 3 chars before the divider so we never strip a title
        # down to something like "Re" (from "Re:Zero").
        if idx >= 3:
            candidate = title[:idx].strip()
            if candidate and len(candidate) < len(best):
                best = candidate
    return best


def _short_title(title: str, franchise_data: dict | None = None) -> str:
    """Return the shortest usable title for file naming.

    Priority:
      1. A synonym from ``franchise_data`` that is shorter than the original
         title and at least 3 characters long.
      2. The "main title" before a subtitle divider, when it's short enough to
         stand on its own (e.g. "Tsukimichi: Moonlit Fantasy" → "Tsukimichi").
      3. An acronym generated from the title.
      4. The original title if all else fails.
    """
    if not title:
        return ""

    # 1. Check AniList synonyms (stored in franchise_data)
    if franchise_data:
        synonyms = franchise_data.get("synonyms", [])
        if synonyms:
            # Pick the shortest synonym that's still recognizable (>= 3 chars)
            # and shorter than the original title.
            candidates = [
                s for s in synonyms
                if len(s) >= 3 and len(s) < len(title)
            ]
            if candidates:
                return min(candidates, key=len)

    # 2. Prefer the main title before a subtitle divider when it's concise
    #    (<= 3 words). This handles the very common "Name: Tagline" pattern
    #    without collapsing a genuinely multi-word name into an acronym.
    main = _main_title(title)
    if main != title:
        main_words = [w for w in re.split(r"[\s\-]+", main.strip()) if w]
        if len(main_words) <= 3:
            return main

    # 3. For short titles (<= 3 words), keep the original — no acronym needed
    words = re.split(r"[\s\-]+", title.strip())
    words = [w for w in words if w]
    if len(words) <= 3:
        return title

    # 4. Long titles (> 3 words): shorten. Acronym the main title if we have a
    #    concise one, otherwise the whole title.
    acronym = _generate_acronym(main if main != title else title)
    if acronym and len(acronym) >= 2:
        return acronym

    # 5. Last resort: original title
    return title


def _generate_acronym(title: str) -> str:
    """Generate an acronym from a title.

    Rules:
      - If the title has 3 or fewer words, use the first letter of every word.
      - If the title has more than 3 words, remove filler words first, then
        take the first letter of each remaining word.
      - If removing fillers leaves nothing, fall back to all words.

    Examples:
      "Attack on Titan" → "AOT" (3 words, use all)
      "That Time I Got Reincarnated as a Slime" → "TTIGRS" (fillers removed)
      "Alya Sometimes Hides Her Feelings in Russian" → "ASHHFIR"
    """
    # Split on spaces and hyphens
    words = re.split(r"[\s\-]+", title.strip())
    words = [w for w in words if w]  # remove empties

    if len(words) <= 3:
        # Short titles: use first letter of every word (including fillers)
        acronym = "".join(w[0] for w in words if w and w[0].isalpha()).upper()
    else:
        # Long titles: remove filler words first
        relevant = [w for w in words if w.lower() not in _FILLER_WORDS and w[0].isalpha()]
        if not relevant:
            relevant = [w for w in words if w[0].isalpha()]
        acronym = "".join(w[0] for w in relevant).upper()

    return acronym[:15]  # cap at 15 chars to avoid absurdly long acronyms


# ── Content-kind classification ────────────────────────────────────────────────
#
# By the time files reach rename/upload, the rich AniList ``ContentKind`` is gone
# — only ``season`` / ``episode`` survive on ``MediaFile``. Extras are encoded via
# the season slot: regular TV seasons are 1..~89, while movies/OVAs/ONAs/specials
# live at 90+ (see the manual-upload ingest and franchise mapping). A movie is the
# further special case of a single-file, single-episode entry with no season.
#
# ``KIND_SEASON`` / ``KIND_MOVIE`` / ``KIND_SPECIAL`` are the canonical labels used
# for BOTH the per-type filename template selection and the ``{content_type}``
# header variable, so a title is named and headered consistently everywhere.

KIND_SEASON = "Season"
KIND_MOVIE = "Movie"
KIND_SPECIAL = "Special"

# Season slot at/above which an entry is treated as an extra (not a TV season).
EXTRA_SEASON_THRESHOLD = 90


def classify_kind(season: int | None, *, episode_count: int = 2) -> str:
    """Classify an entry as ``Season`` / ``Movie`` / ``Special`` from its slot.

    ``episode_count`` is the number of episodes in the entry's pack — used only to
    distinguish a single-file movie from a multi-episode OVA/special when the
    season slot itself is ambiguous (``None``).
    """
    if season is None:
        # No season → a standalone extra. One file ⇒ Movie, otherwise a Special.
        return KIND_MOVIE if episode_count <= 1 else KIND_SPECIAL
    if season >= EXTRA_SEASON_THRESHOLD:
        return KIND_SPECIAL
    return KIND_SEASON


def _content_type_label(season: int | None, episode_count: int,
                        name_hint: str | None = None) -> str:
    """A user-facing entry-type label for templates: Season / Movie / OVA / ONA / Special.

    Refines :func:`classify_kind` with a filename hint so an OVA reads as "OVA"
    (not the generic "Special"). ``name_hint`` is the original filename/title —
    the same substring signal ``LocalFileSource`` uses on ingest.
    """
    kind = classify_kind(season, episode_count=episode_count)
    if kind == KIND_SEASON:
        return KIND_SEASON
    if kind == KIND_MOVIE:
        return KIND_MOVIE
    low = (name_hint or "").lower()
    if "ova" in low:
        return "OVA"
    if "ona" in low:
        return "ONA"
    if "movie" in low:
        return KIND_MOVIE
    return KIND_SPECIAL


async def _push_stage_progress(
    c, ctx: StageContext, stage_name: str, progress: float,
    *, file_index: int | None = None, file_total: int | None = None,
) -> None:
    """Push a ProgressSnapshot for the current processing stage so the log channel
    shows bars even during compression/watermarking. Falls back silently if Redis
    is unavailable — cosmetic telemetry must never break the actual job."""
    store = getattr(c, "progress", None)
    if store is None:
        return
    from nekofetch.infrastructure.database.redis.progress import ProgressSnapshot
    try:
        snap = ProgressSnapshot(
            job_id=ctx.job_id,
            status="RUNNING",
            progress=progress,
            stage=stage_name,
            episode_index=file_index,
            total_episodes=file_total,
        )
        await store.set(snap, ttl=600)
    except Exception:  # noqa: BLE001
        pass


async def _run(*args: str) -> tuple[int, str]:
    """Run a subprocess; return (rc, stderr). rc=-1 if the binary is missing."""
    if shutil.which(args[0]) is None:
        return -1, f"{args[0]} not found"
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    return proc.returncode or 0, err.decode(errors="ignore")


async def _ffprobe_ok(ffprobe: str, path: Path) -> tuple[bool, str]:
    """Decode-probe a media file. A non-corrupt file parses cleanly, has at least
    one video stream, and a positive duration. Returns (ok, reason)."""
    import json

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-of", "json",
            "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except Exception as exc:  # noqa: BLE001
        return False, f"probe error: {exc}"
    if proc.returncode != 0:
        return False, (err.decode(errors="ignore").strip()[:120] or "ffprobe error")
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return False, "unparseable ffprobe output"
    streams = data.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        return False, "no video stream"
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return False, "zero/unknown duration"
    return True, "ok"


async def _audio_track_langs(ffprobe: str, path: Path) -> list[str]:
    """Return the language tag of each audio track in order (``""`` when unset).

    Used by :class:`BrandingStage` to name each audio track. An empty list means
    "couldn't probe" — the caller then falls back to a single-track guess.
    """
    return await _track_langs(ffprobe, path, "a")


async def _track_langs(ffprobe: str, path: Path, select: str) -> list[str]:
    """Return the language tag of each track of a stream type, in order.

    ``select`` is an ffmpeg stream selector: ``"a"`` for audio, ``"s"`` for
    subtitles. ``""`` fills a track whose language tag is unset. An empty list
    means the probe failed (or there are no such tracks).
    """
    import json

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", select,
            "-show_entries", "stream=index:stream_tags=language",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return []
    langs: list[str] = []
    for s in data.get("streams", []):
        langs.append((s.get("tags", {}) or {}).get("language", "") or "")
    return langs


async def _sub_tracks(ffprobe: str, path: Path) -> list[dict]:
    """Probe every subtitle track: ``{index, codec, lang, title}`` in order.

    ``index`` is the subtitle-relative index (0-based, i.e. the ``s:N`` selector),
    ``codec`` the codec_name (``ass``/``subrip``/``hdmv_pgs_subtitle``…), ``lang``
    the ISO tag, and ``title`` the original track title tag (``""`` when unset).
    Used by torrent subtitle branding, which keeps the ORIGINAL title (e.g.
    "Signs & Songs") rather than the language, and needs the codec to know which
    tracks are text (brandable) vs image (PGS/VobSub — title-only).
    """
    import json

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return []
    tracks: list[dict] = []
    for rel, s in enumerate(data.get("streams", [])):
        tags = s.get("tags", {}) or {}
        tracks.append({
            "index": rel,
            "codec": (s.get("codec_name") or "").lower(),
            "lang": tags.get("language", "") or "",
            "title": tags.get("title", "") or "",
        })
    return tracks


async def _probe_duration_ms(ffprobe: str, path: Path) -> int | None:
    """Media duration in ms (for subtitle branding-window placement), or None."""
    import json

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads(out or b"{}")
        return int(float(data.get("format", {}).get("duration") or 0) * 1000) or None
    except Exception:  # noqa: BLE001
        return None


class VerifyStage(Stage):
    stage = ProcessingStage.VERIFY

    def enabled(self) -> bool:
        return self.c.config.processing.verify_files

    async def process(self, ctx: StageContext) -> None:
        from nekofetch.core.exceptions import ProcessingError
        from nekofetch.sources._hls import find_ffprobe

        ffprobe = find_ffprobe()
        corrupt: list[str] = []
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Verifying", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            path = Path(f.local_path) if f.local_path else None
            if not (path and path.exists() and path.stat().st_size > 0):
                # Missing files are expected during incremental quality-tier
                # runs (e.g. only 360p downloaded so far, 720p not yet).
                # Skip them — they'll be verified when their tier downloads.
                f.verified = False
                continue
            if ffprobe:
                ok, reason = await _ffprobe_ok(ffprobe, path)
            else:  # no ffprobe — fall back to a size-only check, can't prove corrupt
                ok, reason = True, "ffprobe unavailable (size-only check)"
                ctx.notes.append("verify: ffprobe unavailable, size-only check")
            f.verified = ok
            if not ok:
                corrupt.append(f"{path.name}: {reason}")
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Verifying", pct, file_index=i + 1, file_total=n)
        # Corrupt files must never reach the database channel — fail the whole job
        # so it's surfaced and can be retried, rather than silently shipping garbage.
        if corrupt:
            raise ProcessingError("corrupt file(s): " + "; ".join(corrupt[:5]))


# Canonical short audio tags used in file names — the AudioType enum values
# ("subbed" / "dubbed" / "dual_audio" / "multi") are too verbose for filenames.
_AUDIO_TAG = {
    "subbed": "Sub",
    "dubbed": "Dub",
    "dual_audio": "Dual",
    "multi": "Multi",
}


class RenameStage(Stage):
    stage = ProcessingStage.RENAME

    def enabled(self) -> bool:
        return self.c.config.rename.enabled

    async def process(self, ctx: StageContext) -> None:
        branding = BrandingService(self.c)
        cfg = self.c.config.rename
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Renaming", 0.0, file_index=0, file_total=n)

        # Pre-compute short title from AniList synonyms or acronym fallback
        anime_title = ctx.request.anime_title
        franchise_data = ctx.request.franchise_data or {}
        short_title = _short_title(anime_title, franchise_data)

        # Pre-compute total episodes per season for dynamic padding AND for the
        # movie-vs-special disambiguation (a lone file with no season is a movie).
        season_totals: dict[int, int] = {}
        for f in ctx.files:
            s = f.season or 0
            season_totals[s] = season_totals.get(s, 0) + 1

        for i, f in enumerate(ctx.files):
            raw = (f.audio.value if f.audio else "").lower()
            audio_short = _AUDIO_TAG.get(raw, raw)
            season_part = f.season_part
            part_str = f"P{season_part:02d}" if season_part else ""

            # Dynamic episode padding based on total episodes in this season.
            # Minimum is ALWAYS two digits — never ``E1``/``S1``. Even a 6-episode
            # season is ``E01``..``E06``; scale to ``E001`` only past 99 episodes.
            s = f.season or 0
            total_for_season = season_totals.get(s, 0)
            if total_for_season <= 99:
                ep_str = f"{f.episode or 0:02d}"
            else:
                ep_str = f"{f.episode or 0:03d}"

            # Select the filename template by entry kind so movies/OVAs/specials
            # aren't forced into a nonsensical "S90E01" season pattern.
            kind = classify_kind(f.season, episode_count=total_for_season or 1)
            if kind == KIND_MOVIE and cfg.movie_template:
                tmpl = cfg.movie_template
            elif kind == KIND_SPECIAL and cfg.special_template:
                tmpl = cfg.special_template
            else:
                tmpl = cfg.template
            content_type = _content_type_label(f.season, total_for_season or 1,
                                                f.original_name)

            new_name = templates.render_filename(
                tmpl,
                title=anime_title,
                short_title=short_title,
                season=f"{f.season or 1:02d}",
                season_part=part_str,
                episode=ep_str,
                content_type=content_type,
                resolution=f.resolution or "",
                audio=audio_short,
                source=ctx.request.source,
                group=branding.group,
            )
            ext = Path(f.local_path).suffix if f.local_path else f".{f.container or 'mkv'}"
            f.final_name = f"{new_name}{ext}"
            if f.local_path:
                dest = Path(f.local_path).with_name(f.final_name)
                try:
                    Path(f.local_path).rename(dest)
                    f.local_path = str(dest)
                except OSError as exc:
                    ctx.notes.append(f"rename skipped: {exc}")
            pct = (i + 1) / n * 100
            await _push_stage_progress(self.c, ctx, "Renaming", pct, file_index=i + 1, file_total=n)


class MetadataStage(Stage):
    stage = ProcessingStage.METADATA

    def enabled(self) -> bool:
        return self.c.config.features.metadata_editing and self.c.config.metadata.enabled

    async def process(self, ctx: StageContext) -> None:
        meta = self.c.config.metadata
        branding = BrandingService(self.c).metadata_fields()
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Metadata", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            if not f.local_path:
                continue
            container = (f.container or "").lower()
            if container not in meta.supported_containers:
                ctx.notes.append(f"metadata: unsupported container {container}")
                continue
            # Build every ``--set`` flag the configured flags + branding fields
            # ask for. ``mkvpropedit`` runs ONCE per file with the union list —
            # a single write-batch avoids re-validating the file header N times.
            tags: list[str] = []
            if meta.update_title:
                title_value = ctx.request.anime_title or ""
                if title_value:
                    tags += ["--edit", "info", "--set", f"title={title_value}"]
            # Author / Comment — driven by ``meta.update_*`` GATES so an operator
            # can disable either field without touching config.yaml. Values come
            # from :class:`BrandingService.metadata_fields` which returns the
            # ``branding.metadata_author`` / ``branding.metadata_comment``
            # strings (e.g. ``Anime Weebs`` / ``Provided by Anime Weebs``).
            if container == "mkv":
                if meta.update_author and branding.get("author"):
                    tags += ["--edit", "info", "--set",
                             f"author={branding['author']}"]
                if meta.update_comment and branding.get("comment"):
                    tags += ["--edit", "info", "--set",
                             f"comment={branding['comment']}"]
                if meta.update_description:
                    # Description is sourced from the AniList-derived
                    # ``franchise_data.synopsis`` (set by
                    # ``bots/admin/handlers/requests.py``) when present, else
                    # falls back to the anime title — the same fallback
                    # ``render_anime_info`` uses when no synopsis lands.
                    desc = ""
                    fd = ctx.request.franchise_data or {}
                    if isinstance(fd, dict):
                        desc = (fd.get("description")
                                or fd.get("synopsis") or "").strip()
                    if not desc:
                        desc = (ctx.request.anime_title or "").strip()
                    if desc:
                        # mkvpropedit treats arbitrarily-long description as
                        # an opaque UTF-8 string; truncate to 500 chars to
                        # avoid surprising long-string UI in older players.
                        tags += ["--edit", "info", "--set",
                                 f"description={desc[:500]}"]
            # mkvpropedit handles MKV title/author/comment/description; ffmpeg
            # covers other container types in a full build (out of scope here).
            if container == "mkv" and tags:
                rc, err = await _run("mkvpropedit", f.local_path, *tags)
                if rc != 0:
                    ctx.notes.append(
                        f"metadata: {err.strip() or 'mkvpropedit unavailable'}"
                    )
                else:
                    applied = sum(1 for t in tags if t == "--set")
                    ctx.notes.append(f"metadata: {applied} mkvpropedit field(s) applied")
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Metadata", pct, file_index=i + 1, file_total=n)


# Language code → human display name for track branding. Torrents arrive already
# muxed, so we read the embedded track's ISO-639 language tag (e.g. "jpn") and
# turn it into the same display name the streaming mux path uses ("Japanese"),
# so both paths produce identical chrome-bracket labels. Unknown/blank tags fall
# back to the "Anime Weebs #N" form inside ``brand_track_title``.
_LANG_DISPLAY = {
    "jpn": "Japanese", "ja": "Japanese", "jp": "Japanese",
    "eng": "English", "en": "English",
    "spa": "Spanish", "es": "Spanish",
    "por": "Portuguese", "pt": "Portuguese",
    "fra": "French", "fre": "French", "fr": "French",
    "deu": "German", "ger": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "rus": "Russian", "ru": "Russian",
    "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi",
    "kor": "Korean", "ko": "Korean",
    "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ind": "Indonesian", "id": "Indonesian",
}


def _lang_display(lang: str) -> str:
    """Map an ISO-639 language tag to a human display name ("jpn" → "Japanese").

    Returns "" for unknown/untagged languages so ``brand_track_title`` uses its
    "Anime Weebs #N" fallback instead of an ugly raw code.
    """
    c = (lang or "").lower().split("-")[0].strip()
    if c in ("", "und", "unk", "mis", "zxx"):
        return ""
    return _LANG_DISPLAY.get(c, "")


# Sources whose files arrive as a single high-quality torrent download. These
# need us to (a) derive the lower resolution tiers (EncodeStage), and (b) brand
# subtitle CONTENT + keep original-title track names (BrandingStage). Streaming
# sources acquire each quality natively and are mux-branded at assembly time.
_TORRENT_SOURCES = frozenset({"nyaa"})


_CORNER_OVERLAY = {
    "top_left": "10:10",
    "top_right": "main_w-overlay_w-10:10",
    "bottom_left": "10:main_h-overlay_h-10",
    "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
}
_CORNER_TEXT = {
    "top_left": "x=10:y=10",
    "top_right": "x=w-tw-10:y=10",
    "bottom_left": "x=10:y=h-th-10",
    "bottom_right": "x=w-tw-10:y=h-th-10",
}


class BrandingStage(Stage):
    stage = ProcessingStage.BRANDING

    def enabled(self) -> bool:
        return self.c.config.processing.branding and self.c.config.branding.enabled

    async def process(self, ctx: StageContext) -> None:
        """Brand a torrent's already-muxed MKV with the canonical chrome-bracket
        style — matching the labels the streaming mux path writes via
        ``_branding.py`` — with a torrent-specific subtitle rule.

          * Container title → ``"AnimeName〢@AniXWeebs"``
          * Audio track     → ``"Japanese〘 @AniXWeebs 〙"`` (language-based)
          * Subtitle track  → torrent-specific: keep the ORIGINAL track title
            (e.g. "Signs & Songs") + ``〘 @AniXWeebs 〙`` (fansub subs carry
            meaningful names — Full / Signs&Songs / Dialogue — that we preserve),
            and INJECT a ``Telegram: @AniXWeebs`` cue into the subtitle content of
            every episode. Streaming sources instead name subs by language and are
            already content-branded at mux time by ``_subs``.

        Streaming sources arrive branded (mux path), so this stage only re-affirms
        their metadata cheaply via ``mkvpropedit`` (no content remux). Torrents get
        the full extract → inject-cue → remux pass for their subtitle CONTENT, then
        the original-title track names. MKV-only, best-effort: a missing binary /
        unsupported container records a note and moves on rather than failing.
        """
        from nekofetch.sources._branding import (
            brand_container_title,
            brand_track_title,
        )
        from nekofetch.sources._hls import find_ffprobe

        if not self.c.config.branding.enabled:
            return
        is_torrent = (ctx.request.source or "").lower() in _TORRENT_SOURCES
        ffprobe = find_ffprobe()
        anime_title = (ctx.request.anime_title or "").strip()
        branded_title = brand_container_title(anime_title) if anime_title else ""
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Branding", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            if not f.local_path or (f.container or "").lower() != "mkv":
                if f.local_path and (f.container or "").lower() != "mkv":
                    ctx.notes.append(f"branding: skipped non-mkv {f.container}")
                continue
            path = Path(f.local_path)

            # ── Torrent path: inject the Telegram cue into subtitle CONTENT and
            #    keep original-title track names via a single remux. ──
            if is_torrent and ffprobe:
                did = await self._brand_torrent_file(
                    ctx, path, branded_title, brand_track_title,
                )
                if did:
                    pct = ((i + 1) / n) * 100
                    await _push_stage_progress(
                        self.c, ctx, "Branding", pct, file_index=i + 1, file_total=n)
                    continue
                # Remux unavailable/failed → fall through to metadata-only branding
                # so the file is at least title/track branded.

            audio_langs = await _track_langs(ffprobe, path, "a") if ffprobe else []
            sub_langs = await _track_langs(ffprobe, path, "s") if ffprobe else []

            tags: list[str] = []
            # Container title — branded + idempotent (the helper no-ops if already
            # branded), so re-running the stage never double-appends the handle.
            if branded_title:
                tags += ["--edit", "info", "--set", f"title={branded_title}"]
            # Audio: fall back to a single track when the probe found none (so a
            # container ffprobe can't read still gets a branded track name).
            audio_count = len(audio_langs) or 1
            for t in range(1, audio_count + 1):
                lang = audio_langs[t - 1] if t - 1 < len(audio_langs) else ""
                name = brand_track_title(_lang_display(lang), t)
                tags += ["--edit", f"track:a{t}", "--set", f"name={name}"]
            # Subtitles: only name tracks we actually detected — never invent a
            # phantom subtitle track on a file that has none.
            for t in range(1, len(sub_langs) + 1):
                name = brand_track_title(_lang_display(sub_langs[t - 1]), t)
                tags += ["--edit", f"track:s{t}", "--set", f"name={name}"]

            rc, err = await _run("mkvpropedit", f.local_path, *tags)
            if rc != 0:
                ctx.notes.append(f"branding: {err.strip() or 'mkvpropedit unavailable'}")
            else:
                ctx.notes.append(
                    f"branding: title + {audio_count} audio + {len(sub_langs)} "
                    "subtitle track(s) branded"
                )
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Branding", pct, file_index=i + 1, file_total=n)

    async def _brand_torrent_file(
        self, ctx: StageContext, path: Path, branded_title: str, brand_track_title,
    ) -> bool:
        """Torrent subtitle branding for one file: inject the Telegram cue into
        subtitle content + keep original-title track names, via one remux.

        Returns True when the remux replaced the file, False when there's nothing
        to do or the remux couldn't run (caller falls back to metadata-only).
        Audio-track names + container title are applied afterwards via mkvpropedit
        (fast, no second remux) so this method owns ONLY the subtitle work.
        """
        from nekofetch.sources._hls import find_ffprobe
        from nekofetch.sources._torrent_subs import brand_torrent_subtitles

        ffprobe = find_ffprobe()
        sub_tracks = await _sub_tracks(ffprobe, path) if ffprobe else []
        if not sub_tracks:
            return False  # no subs → nothing torrent-specific to do here

        video_ms = await _probe_duration_ms(ffprobe, path)
        tmp_out = path.with_name(path.stem + ".brand.mkv")
        try:
            manifest = await brand_torrent_subtitles(
                path, tmp_out, sub_tracks=sub_tracks, video_ms=video_ms,
                container_title=branded_title or None,
                brand_track_title=brand_track_title,
            )
        except Exception as exc:  # noqa: BLE001 — remux failure is recoverable
            ctx.notes.append(f"branding(torrent): remux error {exc}")
            tmp_out.unlink(missing_ok=True)
            return False
        if not manifest.get("ok"):
            tmp_out.unlink(missing_ok=True)
            return False

        try:
            tmp_out.replace(path)  # swap the branded remux in
        except OSError as exc:
            ctx.notes.append(f"branding(torrent): swap failed {exc}")
            tmp_out.unlink(missing_ok=True)
            return False

        # Audio track names + container title (subtitle titles were set in the
        # remux). Cheap metadata-only mkvpropedit pass, no second transcode.
        audio_langs = await _track_langs(ffprobe, path, "a")
        tags: list[str] = []
        if branded_title:
            tags += ["--edit", "info", "--set", f"title={branded_title}"]
        audio_count = len(audio_langs) or 1
        for t in range(1, audio_count + 1):
            lang = audio_langs[t - 1] if t - 1 < len(audio_langs) else ""
            tags += ["--edit", f"track:a{t}", "--set",
                     f"name={brand_track_title(_lang_display(lang), t)}"]
        await _run("mkvpropedit", str(path), *tags)
        ctx.notes.append(
            f"branding(torrent): {manifest['branded_tracks']} sub track(s) content-branded "
            f"({manifest['total_cues']} cues) + {audio_count} audio named"
        )
        return True


class WatermarkStage(Stage):
    """Optional video watermark overlay (text or image) via ffmpeg.

    Opt-in (``watermark.enabled``) and re-encodes video, so it is off by default. Honors
    corner, opacity, and scale. Falls back to a note (not a failure) when ffmpeg is missing.
    """

    stage = ProcessingStage.BRANDING

    def enabled(self) -> bool:
        return self.c.config.watermark.enabled

    def _filter(self, w) -> tuple[str, list[str]]:
        """Build the ffmpeg filter and any extra input args for the configured watermark."""
        if w.type == "image" and w.image_path:
            pos = _CORNER_OVERLAY.get(w.corner, _CORNER_OVERLAY["bottom_right"])
            # scale watermark to a fraction of video width, apply opacity, overlay
            flt = (
                f"[1:v]format=rgba,colorchannelmixer=aa={w.opacity},"
                f"scale=iw*{w.scale}:-1[wm];[0:v][wm]overlay={pos}"
            )
            return flt, ["-i", w.image_path]
        # text watermark
        pos = _CORNER_TEXT.get(w.corner, _CORNER_TEXT["bottom_right"])
        text = (w.text or "").replace(":", r"\:").replace("'", r"\'")
        fontsize = "h*" + str(max(w.scale, 0.03))
        flt = (
            f"drawtext=text='{text}':fontcolor=white@{w.opacity}:"
            f"fontsize={fontsize}:{pos}:box=1:boxcolor=black@0.3:boxborderw=6"
        )
        return flt, []

    async def process(self, ctx: StageContext) -> None:
        w = self.c.config.watermark
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Watermarking", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            if not f.local_path:
                continue
            src = Path(f.local_path)
            out = src.with_name(src.stem + ".wm" + src.suffix)
            flt, extra_inputs = self._filter(w)
            args = ["ffmpeg", "-y", "-i", str(src), *extra_inputs,
                    "-filter_complex" if extra_inputs else "-vf", flt,
                    "-c:a", "copy", str(out)]
            rc, err = await _run(*args)
            if rc != 0:
                ctx.notes.append(f"watermark: {err.strip() or 'ffmpeg unavailable'}")
                continue
            try:
                out.replace(src)  # swap in the watermarked file
            except OSError as exc:
                ctx.notes.append(f"watermark swap failed: {exc}")
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Watermarking", pct, file_index=i + 1, file_total=n)


# Name of the shared poster thumbnail dropped in a request's work folder. The
# uploader looks for this sibling and attaches it to every file in the pack.
POSTER_THUMB_NAME = "poster.jpg"


class ThumbnailStage(Stage):
    stage = ProcessingStage.THUMBNAIL

    def enabled(self) -> bool:
        return self.c.config.features.thumbnail_generation and self.c.config.thumbnail.enabled

    async def process(self, ctx: StageContext) -> None:
        """Produce one poster.jpg for the whole request — preferring the official
        TMDB poster (English/US) over an ffmpeg frame-grab — that the storage
        uploader attaches as the Telegram document thumbnail for every file.

        The image is fit within 320×320 JPEG (Telegram's thumbnail limit). If TMDB
        has nothing usable we fall back to a frame from the first file."""
        first = next((f for f in ctx.files if f.local_path), None)
        if first is None:
            return
        thumb = Path(first.local_path).with_name(POSTER_THUMB_NAME)
        await _push_stage_progress(self.c, ctx, "Fetching Poster", 0.0)

        poster_url = None
        try:
            poster_url = await self.c.tmdb.poster_for(ctx.request.anime_title)
        except Exception as exc:  # noqa: BLE001
            ctx.notes.append(f"thumbnail: tmdb lookup failed ({exc})")
        if poster_url and await self._fit_thumb(poster_url, thumb):
            ctx.notes.append("thumbnail: TMDB poster")
            await _push_stage_progress(self.c, ctx, "Fetching Poster", 100.0)
            return

        # Fallback: a frame from the first file, fit to the same thumbnail box.
        rc, err = await _run(
            "ffmpeg", "-y", "-ss", "00:00:30", "-i", first.local_path, "-vframes", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease", str(thumb),
        )
        if rc != 0:
            ctx.notes.append(f"thumbnail: {err.strip() or 'ffmpeg unavailable'}")
        await _push_stage_progress(self.c, ctx, "Fetching Poster", 100.0)

    @staticmethod
    async def _fit_thumb(src: str, dest: Path) -> bool:
        """Pull ``src`` (URL or path) into a Telegram-legal thumbnail: JPEG, fit
        within 320×320. Returns True only if the file was actually written."""
        rc, _ = await _run(
            "ffmpeg", "-y", "-i", src,
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v", "5", str(dest),
        )
        return rc == 0 and dest.exists() and dest.stat().st_size > 0


class EncodeStage(Stage):
    """Derive lower-resolution renditions (720p / 480p) from each torrent file.

    Torrents deliver one file per episode (we download only the 1080p variant),
    so to ship the standard three-quality packs we transcode the lower tiers
    ourselves. Runs AFTER rename/brand/metadata/watermark so every rendition
    inherits the final name and branding, and BEFORE store so the new files are
    marked processed and uploaded alongside the source. Video is re-encoded
    (x264 CRF); all audio tracks, subtitles and metadata are copied, so
    dual-audio and branding survive. Streaming sources are skipped entirely.
    """

    stage = ProcessingStage.ENCODE

    def enabled(self) -> bool:
        return self.c.config.processing.encode

    async def process(self, ctx: StageContext) -> None:
        if (ctx.request.source or "").lower() not in _TORRENT_SOURCES:
            ctx.notes.append(f"encode: skipped (source {ctx.request.source})")
            return

        from sqlalchemy import inspect as _sa_inspect

        from nekofetch.infrastructure.database.postgres.models import MediaFile
        from nekofetch.sources._transcode import _encode

        heights = [h for h in self.c.config.processing.encode_heights if h > 0]
        if not heights:
            return

        # Only encode files that are actually present on disk this pass. To stay
        # safe across reprocess passes (where rows are reloaded from the DB and
        # the in-memory ``_is_rendition`` marker is gone), encode ONLY the
        # highest-resolution files on disk — derived lower tiers are never a
        # source, so we can't re-encode our own outputs.
        on_disk = [
            f for f in list(ctx.files)
            if f.local_path and Path(f.local_path).exists()
        ]
        if not on_disk:
            return

        def _res_h(f) -> int:
            r = (f.resolution or "").rstrip("p")
            return int(r) if r.isdigit() else 0

        max_h = max((_res_h(f) for f in on_disk), default=0)
        sources = [f for f in on_disk if _res_h(f) == max_h and max_h > 0]
        if not sources:
            return

        session = None
        try:
            session = _sa_inspect(sources[0]).session
        except Exception:  # noqa: BLE001
            session = None

        n = len(sources)
        # Resolve encode tuning from config (fast preset + per-tier CRF), with
        # safe fallbacks so an older config without these keys still works.
        preset = getattr(self.c.config.processing, "encode_preset", "veryfast")
        crf_map = getattr(self.c.config.processing, "encode_crf", None) or _ENCODE_CRF
        # Every (file, height) pair is one encode — count them all so the bar
        # advances per rendition, not per file (a single file with 2 tiers used
        # to jump 0→50→100 with a long silent gap between).
        total_units = sum(
            1 for f in sources for h in heights if f"{h}p" != (f.resolution or "1080p")
        ) or 1
        done_units = 0
        await _push_stage_progress(self.c, ctx, "Encoding", 0.0, file_index=0, file_total=n)
        new_rows: list = []
        for i, f in enumerate(sources):
            src = Path(f.local_path)
            src_res = f.resolution or "1080p"
            # Rename put the resolution token in the stem (e.g.
            # "... [1080p] ..."); we swap it per rendition so names stay correct.
            stem = src.stem
            for height in heights:
                label = f"{height}p"
                if label == src_res:
                    continue  # never "downscale" to the same tier
                await _push_stage_progress(
                    self.c, ctx, f"Encoding {label}",
                    (done_units / total_units) * 100,
                    file_index=i + 1, file_total=n,
                )
                out_stem = _swap_resolution(stem, src_res, label)
                out_path = src.with_name(f"{out_stem}{src.suffix}")
                try:
                    await _encode(src, out_path, height,
                                  crf_map.get(height, 23), preset=preset)
                except Exception as exc:  # noqa: BLE001 - a failed tier is a note, not a job failure
                    ctx.notes.append(f"encode {label}: {exc}")
                    done_units += 1
                    continue
                done_units += 1
                if not (out_path.exists() and out_path.stat().st_size > 0):
                    ctx.notes.append(f"encode {label}: empty output")
                    continue
                row = MediaFile(
                    job_id=f.job_id, anime_doc_id=f.anime_doc_id,
                    season=f.season, season_part=f.season_part, episode=f.episode,
                    resolution=label, audio=f.audio,
                    original_name=f.original_name,
                    final_name=out_path.name, local_path=str(out_path),
                    size_bytes=out_path.stat().st_size, container=f.container,
                    verified=True, processed=False, published=False,
                )
                new_rows.append(row)
                if session is not None:
                    session.add(row)
            await _push_stage_progress(
                self.c, ctx, "Encoding", (done_units / total_units) * 100,
                file_index=i + 1, file_total=n,
            )

        # Make the renditions visible to STORE (and the storage uploader, which
        # reads MediaFile rows for the job) in this same pass.
        ctx.files.extend(new_rows)
        if new_rows:
            ctx.notes.append(f"encode: {len(new_rows)} rendition(s) created")


# Derived-rendition CRFs (mirrors _transcode._CRF; kept local so the stage owns
# its quality knobs).
_ENCODE_CRF = {1080: 21, 720: 21, 480: 22}


def _swap_resolution(stem: str, old_res: str, new_res: str) -> str:
    """Replace the resolution token in a filename stem, tolerating case/format.

    "Takopi S01E01 [1080p] [Dual] - Anime Weebs" → "... [720p] ..." . Falls back
    to appending the new resolution when the old token isn't found, so the two
    renditions never collide on the same name.
    """
    if old_res and old_res in stem:
        return stem.replace(old_res, new_res)
    low = stem.lower()
    if old_res and old_res.lower() in low:
        idx = low.index(old_res.lower())
        return stem[:idx] + new_res + stem[idx + len(old_res):]
    return f"{stem} [{new_res}]"


class StoreStage(Stage):
    stage = ProcessingStage.STORE

    def enabled(self) -> bool:
        return True

    async def process(self, ctx: StageContext) -> None:
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Storing", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            f.processed = True
            pct = (i + 1) / n * 100
            await _push_stage_progress(self.c, ctx, "Storing", pct, file_index=i + 1, file_total=n)


def default_stages(container) -> list[Stage]:
    return [
        VerifyStage(container),
        RenameStage(container),
        MetadataStage(container),
        BrandingStage(container),
        WatermarkStage(container),
        ThumbnailStage(container),
        EncodeStage(container),
        StoreStage(container),
    ]
