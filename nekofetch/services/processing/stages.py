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


class AbortSource(BaseException):
    """Raised inside a processing stage when the admin hit 'Change source'.

    A ``BaseException`` (not ``Exception``) so a stage's broad ``except
    Exception`` can't swallow it — it must unwind straight out of the pipeline to
    the worker, which tears down the source attempt (files + DB rows). Mirrors
    the download loop's ``_AbortSourceAttempt``; kept here so both the pipeline
    and the per-file stage loops can raise it without a circular import.
    """


async def source_abort_requested(container, job_id: int) -> bool:
    """True when 'Change source' was requested for ``job_id`` (Redis flag).

    Same key the download loop uses (``nf:job:<id>:source_abort``) so a change
    requested during processing is seen here too. Best-effort: a Redis blip
    reads as 'not requested' rather than blocking the pipeline."""
    redis = getattr(container, "redis", None)
    if redis is None:
        return False
    try:
        return bool(await redis.get(f"nf:job:{job_id}:source_abort"))
    except Exception:  # noqa: BLE001
        return False


def release_key(f):
    """The encode release-order contract: season → season_part → episode.

    Combined torrents can otherwise run out of order (S1E03 before S1E01, or
    S1P2 before S1P1). Every encode pass sorts its files by this key first;
    named here so the contract is importable and unit-testable against the
    real sort instead of a shadow copy.
    """
    return (
        f.season if f.season is not None else 10_000,
        f.season_part if f.season_part is not None else 0,
        f.episode if f.episode is not None else 0,
    )


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

    Titles up to 4 words are kept whole (operator rule: "up to four words is
    fine"); 5+ words get shortened. Priority:
      1. An English/Latin-script synonym from ``franchise_data`` that is shorter
         than the original title and at least 3 characters long.
      2. The "main title" before a subtitle divider, when ≤ 4 words
         (e.g. "Tsukimichi: Moonlit Fantasy" → "Tsukimichi").
      3. An acronym generated from the title.
      4. The original title if all else fails.
    """
    if not title:
        return ""

    # 1. Check AniList synonyms (stored in franchise_data) — English/Latin script
    #    only (AniList mixes in Filipino/Thai/Korean synonyms we must never use).
    if franchise_data:
        from nekofetch.services.bot_naming import is_latin_script
        synonyms = franchise_data.get("synonyms", [])
        if synonyms:
            # Pick the shortest synonym that's still recognizable (>= 3 chars),
            # shorter than the original title, and written in English script.
            candidates = [
                s for s in synonyms
                if len(s) >= 3 and len(s) < len(title) and is_latin_script(s)
            ]
            if candidates:
                return min(candidates, key=len)

    # 2. Prefer the main title before a subtitle divider when it's concise
    #    (<= 4 words). This handles the very common "Name: Tagline" pattern
    #    without collapsing a genuinely multi-word name into an acronym.
    main = _main_title(title)
    if main != title:
        main_words = [w for w in re.split(r"[\s\-]+", main.strip()) if w]
        if len(main_words) <= 4:
            return main

    # 3. For short titles (<= 4 words), keep the original — no acronym needed.
    #    (Operator rule: up to four words is fine; five+ get shortened.)
    words = re.split(r"[\s\-]+", title.strip())
    words = [w for w in words if w]
    if len(words) <= 4:
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


def _audio_str(audio) -> str | None:
    """The audio enum's string value (``"sub"``/``"dub"``/``"dual"``…), or None.
    Used to tag per-file progress snapshots so the card shows the audio badge."""
    if audio is None:
        return None
    return audio.value if hasattr(audio, "value") else str(audio)


async def _push_stage_progress(
    c, ctx: StageContext, stage_name: str, progress: float,
    *, file_index: int | None = None, file_total: int | None = None,
    episode: int | None = None, season: int | None = None,
    resolution: str | None = None, audio: str | None = None,
) -> None:
    """Push a ProgressSnapshot for the current processing stage so the log channel
    shows bars even during compression/watermarking. Falls back silently if Redis
    is unavailable — cosmetic telemetry must never break the actual job.

    ``episode``/``season``/``resolution``/``audio`` tag the snapshot with the
    CURRENT FILE's identity so per-file stages (encode, watermark) render an
    episode header + transfer panel exactly like the per-episode download card,
    instead of one anonymous batch bar."""
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
            current_episode=episode,
            season=season,
            resolution=resolution,
            audio=audio,
        )
        await store.set(snap, ttl=600)
    except Exception:  # noqa: BLE001
        pass
    # Also mirror the coarse stage NAME onto the job row so the dashboard has a
    # truthful fallback if the Redis snapshot ever lapses (its TTL, an eviction,
    # a Redis restart). Without this the fallback showed "Downloading 0%" for a
    # job that was really mid-encode. Only write on a stage-NAME change — the
    # per-second heartbeat must not hammer Postgres.
    coarse = (stage_name or "").split()[0] if stage_name else ""
    if coarse and _LAST_JOB_STAGE.get(ctx.job_id) != coarse:
        _LAST_JOB_STAGE[ctx.job_id] = coarse
        try:
            from nekofetch.infrastructure.database.postgres.models import DownloadJob
            from nekofetch.infrastructure.database.postgres.session import session_scope

            async with session_scope(c.pg_sessionmaker) as session:
                job = await session.get(DownloadJob, ctx.job_id)
                if job is not None:
                    state = dict(job.resume_state or {})
                    state["stage"] = stage_name
                    job.resume_state = state
        except Exception:  # noqa: BLE001 — telemetry mirror, never fatal
            pass


# Per-job last coarse stage name written to the DB, so the heartbeat only
# persists on an actual stage transition (not every progress tick).
_LAST_JOB_STAGE: dict[int, str] = {}


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


async def _audio_tracks(ffprobe: str, path: Path) -> list[dict]:
    """Probe every audio track: ``{lang, title}`` in order (``""`` when unset).

    Mirrors :func:`_sub_tracks` for audio. :class:`BrandingStage` uses ``title``
    to honour a MEANINGFUL source track name (torrent rule) and falls back to the
    language when the title is a generic layout word like "Stereo"/"5.1".
    """
    import json

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream_tags=language,title",
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
    for s in data.get("streams", []):
        tags = s.get("tags", {}) or {}
        tracks.append({
            "lang": tags.get("language", "") or "",
            "title": tags.get("title", "") or "",
        })
    return tracks


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
        from sqlalchemy import inspect as _sa_inspect

        from nekofetch.core.exceptions import ProcessingError
        from nekofetch.domain.enums import AudioType
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
                # Delete the broken file AND its DB row so the next run doesn't
                # keep tripping over it. Without this, ``_already_have`` sees the
                # row + on-disk file and skips the re-download forever, and the
                # encode-resume guard would re-adopt a bad rendition. Dropping
                # both lets the source be re-downloaded (or the tier re-encoded)
                # from scratch on the retry — a clean resume, not a stuck loop.
                try:
                    path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("verify.corrupt.unlink_failed",
                                file=path.name, error=str(exc))
                try:
                    _sess = _sa_inspect(f).session
                    if _sess is not None:
                        _sess.delete(f)
                except Exception as exc:  # noqa: BLE001
                    log.warning("verify.corrupt.row_drop_failed",
                                file=path.name, error=str(exc))
                log.info("verify.corrupt.dropped", file=path.name, reason=reason)
            # ── Ground-truth audio: the file's OWN tracks, not the title guess ──
            # Title/magnet-name parsing lies (a bare request title has no "Dual
            # Audio" in it → SUBBED). The container never lies: 2 audio tracks =
            # Dual, 3+ = Multi. Correct the recorded audio here so naming, packs
            # and the channel title all reflect reality.
            if ok and ffprobe:
                await self._correct_audio_from_tracks(ctx, f, ffprobe, path, AudioType)
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Verifying", pct, file_index=i + 1, file_total=n)
        # Corrupt files must never reach the database channel — fail the whole job
        # so it's surfaced and can be retried, rather than silently shipping garbage.
        if corrupt:
            raise ProcessingError("corrupt file(s): " + "; ".join(corrupt[:5]))

    async def _correct_audio_from_tracks(
        self, ctx: StageContext, f, ffprobe: str, path: Path, AudioType,
    ) -> None:
        """Set ``f.audio`` from the actual audio-track count in the file.

        Only ever UPGRADES toward dual/multi — a genuine single-track sub/dub
        release keeps its label. Distinct language tags decide multi (3+ langs)
        vs dual (2 langs / 2 tracks); an untagged 2-track file is still dual.
        """
        langs = await _track_langs(ffprobe, path, "a")
        n_tracks = len(langs)
        if n_tracks < 2:
            return  # single audio → trust the existing label
        distinct = {l.lower() for l in langs if l}
        new_audio = AudioType.MULTI if len(distinct) >= 3 else AudioType.DUAL_AUDIO
        if f.audio != new_audio:
            old = getattr(f.audio, "value", f.audio)
            f.audio = new_audio
            ctx.notes.append(
                f"audio: {path.name} has {n_tracks} audio track(s) "
                f"({','.join(sorted(distinct)) or 'untagged'}) → "
                f"{new_audio.value} (was {old})"
            )
            log.info("verify.audio_corrected", file=path.name,
                     tracks=n_tracks, langs=sorted(distinct),
                     audio=new_audio.value, was=str(old))


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

    async def _confirmed_name_title(self, job_id: int) -> str:
        """The admin-confirmed title override for this job, if any (set by the
        pre-rename confirm card into DownloadJob.resume_state["name_title"])."""
        try:
            from nekofetch.infrastructure.database.postgres.models import DownloadJob
            from nekofetch.infrastructure.database.postgres.session import session_scope
            async with session_scope(self.c.pg_sessionmaker) as session:
                job = await session.get(DownloadJob, job_id)
                if job is not None:
                    return (job.resume_state or {}).get("name_title") or ""
        except Exception:  # noqa: BLE001 — override is optional
            pass
        return ""

    async def process(self, ctx: StageContext) -> None:
        branding = BrandingService(self.c)
        cfg = self.c.config.rename
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Renaming", 0.0, file_index=0, file_total=n)

        # Pre-compute short title from AniList synonyms or acronym fallback.
        # An admin-confirmed filename override (from the pre-rename confirm card)
        # replaces the title verbatim — the operator edited the example, so honour
        # their exact title for every file's name.
        anime_title = ctx.request.anime_title
        franchise_data = ctx.request.franchise_data or {}
        override_title = await self._confirmed_name_title(ctx.job_id)
        if override_title:
            anime_title = override_title
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
# so both paths produce identical labels. Unknown/blank tags map to "" so the
# audio/subtitle brand helpers fall back to their placeholder form.
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

    Returns "" for unknown/untagged languages so the audio/subtitle brand helpers
    fall back to their placeholder form instead of an ugly raw code.
    """
    c = (lang or "").lower().split("-")[0].strip()
    if c in ("", "und", "unk", "mis", "zxx"):
        return ""
    return _LANG_DISPLAY.get(c, "")


# Sources whose files arrive as a single high-quality torrent download. These
# need us to (a) derive the lower resolution tiers (EncodeStage), and (b) brand
# subtitle CONTENT + keep original-title track names (BrandingStage). Streaming
# sources acquire each quality natively and are mux-branded at assembly time.
# DDL joins this set: archives arrive as loose files that need the same
# derive-tiers + subtitle-branding treatment as a torrent download.
_TORRENT_SOURCES = frozenset({"nyaa", "ddl"})


# Sources that need release-site (MoviesMod) cruft stripped from subtitle content
# and track titles. DDL packs carry it; torrents (fansub releases) never do, so
# this is DDL-only — a torrent's fansub cues and titles are preserved verbatim.
_DOMAIN_STRIP_SOURCES = frozenset({"ddl"})


# Corner position expressions, parameterised by per-edge margin (px). ``m`` is
# the configured ``watermark.margin``.
def _corner_overlay(corner: str, m: int) -> str:
    return {
        "top_left": f"{m}:{m}",
        "top_right": f"main_w-overlay_w-{m}:{m}",
        "bottom_left": f"{m}:main_h-overlay_h-{m}",
        "bottom_right": f"main_w-overlay_w-{m}:main_h-overlay_h-{m}",
    }.get(corner, f"main_w-overlay_w-{m}:main_h-overlay_h-{m}")


def _corner_text(corner: str, m: int) -> str:
    return {
        "top_left": f"x={m}:y={m}",
        "top_right": f"x=w-tw-{m}:y={m}",
        "bottom_left": f"x={m}:y=h-th-{m}",
        "bottom_right": f"x=w-tw-{m}:y=h-th-{m}",
    }.get(corner, f"x=w-tw-{m}:y=h-th-{m}")


# Directory holding the shipped watermark fonts (resources/fonts/).
_FONTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "fonts"


def _resolve_font_path(font: str) -> str:
    """Absolute path to a watermark font under resources/fonts/, or "" if unset
    or missing (caller then omits fontfile → ffmpeg default)."""
    name = (font or "").strip()
    if not name:
        return ""
    p = _FONTS_DIR / name
    return str(p) if p.exists() else ""


class BrandingStage(Stage):
    stage = ProcessingStage.BRANDING

    def enabled(self) -> bool:
        return self.c.config.processing.branding and self.c.config.branding.enabled

    async def process(self, ctx: StageContext) -> None:
        """Brand a torrent's already-muxed MKV with the canonical per-stream
        bracket styles — matching the labels the streaming mux path writes via
        ``_branding.py`` — with a torrent-specific subtitle rule.

          * Container title → ``"AnimeName〢@AniXWeebs"``
          * Audio track     → ``"English『 @AniXWeebs 』"`` (meaningful source
            title if present, else language; ``"Audio Track 〢N『 @AniXWeebs 』"``
            when neither is available)
          * Subtitle track  → torrent-specific: keep the ORIGINAL track title
            (e.g. "Signs & Songs") + ``〘 @AniXWeebs 〙`` (fansub subs carry
            meaningful names — Full / Signs&Songs / Dialogue — that we preserve),
            and INJECT a ``Telegram: @AniXWeebs`` cue into the subtitle content of
            every episode. Streaming sources instead name subs by language and are
            already content-branded at mux time by ``_subs``.
          * Video credit    → container-level ``ENCODED_BY`` (set in the mux/remux
            builders, not here — mkvpropedit can't set an arbitrary global tag).

        Streaming sources arrive branded (mux path), so this stage only re-affirms
        their metadata cheaply via ``mkvpropedit`` (no content remux). Torrents get
        the full extract → inject-cue → remux pass for their subtitle CONTENT, then
        the original-title track names. MKV-only, best-effort: a missing binary /
        unsupported container records a note and moves on rather than failing.
        """
        from nekofetch.sources._branding import (
            brand_audio_title,
            brand_container_title,
            brand_subtitle_title,
            is_meaningful_track_name,
            is_moviesmod_title,
        )
        from nekofetch.sources._hls import find_ffprobe

        if not self.c.config.branding.enabled:
            return
        is_torrent = (ctx.request.source or "").lower() in _TORRENT_SOURCES
        strip_domain = (ctx.request.source or "").lower() in _DOMAIN_STRIP_SOURCES
        ffprobe = find_ffprobe()
        anime_title = (ctx.request.anime_title or "").strip()
        branded_title = brand_container_title(anime_title) if anime_title else ""
        n = len(ctx.files)
        await _push_stage_progress(self.c, ctx, "Branding", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(ctx.files):
            if await source_abort_requested(self.c, ctx.job_id):
                raise AbortSource()  # 'Change source' — bail mid-branding
            if not f.local_path or (f.container or "").lower() != "mkv":
                if f.local_path and (f.container or "").lower() != "mkv":
                    ctx.notes.append(f"branding: skipped non-mkv {f.container}")
                continue
            path = Path(f.local_path)

            # ── Torrent path: inject the Telegram cue into subtitle CONTENT and
            #    keep original-title track names via a single remux. ──
            if is_torrent and ffprobe:
                did = await self._brand_torrent_file(
                    ctx, path, branded_title, brand_subtitle_title, strip_domain,
                )
                if did:
                    pct = ((i + 1) / n) * 100
                    await _push_stage_progress(
                        self.c, ctx, "Branding", pct, file_index=i + 1, file_total=n)
                    continue
                # Remux unavailable/failed → fall through to metadata-only branding
                # so the file is at least title/track branded.

            audio = await _audio_tracks(ffprobe, path) if ffprobe else []
            subs = await _sub_tracks(ffprobe, path) if ffprobe else []

            tags: list[str] = []
            # Container title — branded + idempotent (the helper no-ops if already
            # branded), so re-running the stage never double-appends the handle.
            if branded_title:
                tags += ["--edit", "info", "--set", f"title={branded_title}"]
            # Audio: keep a MEANINGFUL source title (torrent rule), else the
            # language, else "Audio Track 〢N". On DDL a MoviesMod site title is
            # dropped so it falls back to the language. Fall back to one track when
            # the probe found none (so a container ffprobe can't read still gets a name).
            audio_count = len(audio) or 1
            for t in range(1, audio_count + 1):
                tr = audio[t - 1] if t - 1 < len(audio) else {}
                a_title = tr.get("title", "")
                if strip_domain and is_moviesmod_title(a_title):
                    a_title = ""
                name = brand_audio_title(
                    a_title, t,
                    fallback_lang=_lang_display(tr.get("lang", "")))
                tags += ["--edit", f"track:a{t}", "--set", f"name={name}"]
            # Subtitles: only name tracks we actually detected — never invent a
            # phantom subtitle track. Keep the original title (fansub "Full Subs"),
            # else the language, else the bare "〘 By @AniXWeebs 〙". On DDL a
            # MoviesMod site title is dropped so it falls back to the language.
            for t in range(1, len(subs) + 1):
                tr = subs[t - 1]
                s_title = tr.get("title", "")
                usable = is_meaningful_track_name(s_title) and not (
                    strip_domain and is_moviesmod_title(s_title))
                sub_name = s_title if usable else _lang_display(tr.get("lang", ""))
                name = brand_subtitle_title(sub_name, t)
                tags += ["--edit", f"track:s{t}", "--set", f"name={name}"]

            rc, err = await _run("mkvpropedit", f.local_path, *tags)
            if rc != 0:
                ctx.notes.append(f"branding: {err.strip() or 'mkvpropedit unavailable'}")
            else:
                ctx.notes.append(
                    f"branding: title + {audio_count} audio + {len(subs)} "
                    "subtitle track(s) branded"
                )
            pct = ((i + 1) / n) * 100
            await _push_stage_progress(self.c, ctx, "Branding", pct, file_index=i + 1, file_total=n)

    async def _brand_torrent_file(
        self, ctx: StageContext, path: Path, branded_title: str, brand_subtitle_title,
        strip_domain: bool = False,
    ) -> bool:
        """Torrent subtitle branding for one file: inject the Telegram cue into
        subtitle content + keep original-title track names, via one remux.

        Returns True when the remux replaced the file, False when there's nothing
        to do or the remux couldn't run (caller falls back to metadata-only).
        Audio-track names + container title are applied afterwards via mkvpropedit
        (fast, no second remux) so this method owns ONLY the subtitle work.

        ``strip_domain`` (DDL only) removes MoviesMod release-site cruft from the
        subtitle content + track titles inside the remux.
        """
        from nekofetch.sources._branding import brand_audio_title
        from nekofetch.sources._hls import find_ffprobe
        from nekofetch.sources._torrent_subs import brand_torrent_subtitles

        ffprobe = find_ffprobe()
        sub_tracks = await _sub_tracks(ffprobe, path) if ffprobe else []
        if not sub_tracks:
            return False  # no subs → nothing torrent-specific to do here

        # Audio tracks are branded IN the remux below (not just the later
        # mkvpropedit pass), so a missing mkvpropedit can no longer leave audio
        # plain while subs come out branded.
        audio_pre = await _audio_tracks(ffprobe, path) if ffprobe else []
        video_ms = await _probe_duration_ms(ffprobe, path)
        tmp_out = path.with_name(path.stem + ".brand.mkv")
        try:
            manifest = await brand_torrent_subtitles(
                path, tmp_out, sub_tracks=sub_tracks, video_ms=video_ms,
                container_title=branded_title or None,
                brand_subtitle_title=brand_subtitle_title,
                audio_tracks=audio_pre, brand_audio_title=brand_audio_title,
                lang_display=_lang_display,
                normalize_dialogue=getattr(
                    self.c.config.processing, "subtitle_dialogue_normalize", True),
                strip_domain=strip_domain,
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

        # Audio track names + container title were already set in the remux above.
        # This mkvpropedit pass re-affirms them (matroska-native track names) and
        # is now purely a nice-to-have — if mkvpropedit is unavailable the audio is
        # STILL branded from the remux, so we just note it instead of losing the
        # branding silently.
        audio = await _audio_tracks(ffprobe, path)
        tags: list[str] = []
        if branded_title:
            tags += ["--edit", "info", "--set", f"title={branded_title}"]
        audio_count = len(audio) or 1
        for t in range(1, audio_count + 1):
            tr = audio[t - 1] if t - 1 < len(audio) else {}
            name = brand_audio_title(
                tr.get("title", ""), t,
                fallback_lang=_lang_display(tr.get("lang", "")))
            tags += ["--edit", f"track:a{t}", "--set", f"name={name}"]
        rc, err = await _run("mkvpropedit", str(path), *tags)
        if rc != 0:
            ctx.notes.append(
                "branding(torrent): audio names set in remux; mkvpropedit "
                f"re-affirm skipped ({err.strip() or 'mkvpropedit unavailable'})"
            )
        ctx.notes.append(
            f"branding(torrent): {manifest['branded_tracks']} sub track(s) content-branded "
            f"({manifest['total_cues']} cues) + {audio_count} audio named"
        )
        return True


class WatermarkStage(Stage):
    """Optional video watermark overlay (text or image) via ffmpeg.

    Gated by ``watermark.mode``: ``off`` never runs, ``always`` burns the mark on
    the highest-res source, and ``auto`` (default) burns it ONLY when subtitle
    branding wasn't possible — i.e. a file with no brandable TEXT subtitle track
    (PGS/image subs, or no subs). Honors corner, opacity, and scale. Re-encodes
    video; falls back to a note (not a failure) when ffmpeg is missing.
    """

    stage = ProcessingStage.BRANDING

    # Shares the BRANDING enum with BrandingStage but is a DIFFERENT operation —
    # give it its own label so logs/events/the live card read "watermarking".
    @property
    def log_name(self) -> str:
        return "watermarking"

    def _mode(self) -> str:
        """Resolved watermark mode. Honors the new ``mode`` field; falls back to
        the legacy ``enabled`` bool (True → "always") for old configs."""
        w = self.c.config.watermark
        mode = (getattr(w, "mode", "") or "").strip().lower()
        if mode in ("off", "always", "auto"):
            return mode
        # Legacy config without ``mode``: honour the old enabled bool.
        return "always" if getattr(w, "enabled", False) else "off"

    def enabled(self) -> bool:
        return self._mode() != "off"

    def _filter(self, w) -> tuple[str, list[str]]:
        """Build the ffmpeg filter and any extra input args for the configured watermark."""
        margin = int(getattr(w, "margin", 16) or 0)
        if w.type == "image" and w.image_path:
            pos = _corner_overlay(w.corner, margin)
            # scale watermark to a fraction of video width, apply opacity, overlay
            flt = (
                f"[1:v]format=rgba,colorchannelmixer=aa={w.opacity},"
                f"scale=iw*{w.scale}:-1[wm];[0:v][wm]overlay={pos}"
            )
            return flt, ["-i", w.image_path]
        # text watermark
        pos = _corner_text(w.corner, margin)
        # drawtext is picky: escape the ffmpeg-special chars in the user's text.
        text = (
            (w.text or "")
            .replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
            .replace("%", r"\%")
        )
        # Size as a fraction of frame HEIGHT (0.03 ≈ a small, tasteful corner mark).
        fontsize = f"h*{max(float(w.scale or 0.03), 0.01)}"
        font_path = _resolve_font_path(getattr(w, "font", ""))
        font_clause = ""
        if font_path:
            # Backslash-escape the path for the drawtext option string.
            esc = font_path.replace("\\", r"\\").replace(":", r"\:")
            font_clause = f"fontfile='{esc}':"
        # Clean semi-transparent white with a soft shadow for legibility on light
        # frames (no heavy box — matches the reference corner-mark look).
        flt = (
            f"drawtext={font_clause}text='{text}':fontcolor=white@{w.opacity}:"
            f"fontsize={fontsize}:{pos}:"
            f"shadowcolor=black@0.45:shadowx=2:shadowy=2"
        )
        return flt, []

    async def process(self, ctx: StageContext) -> None:
        from nekofetch.sources._hls import find_ffmpeg, find_ffprobe
        from nekofetch.sources._torrent_subs import is_text_sub
        from nekofetch.sources._transcode import (
            build_watermark_encode_args, is_10bit, probe_duration_s_async,
            probe_pixel_format, run_watermark,
        )

        w = self.c.config.watermark
        ffmpeg = find_ffmpeg()
        mode = self._mode()
        # In "auto" mode we only watermark files whose subtitle branding wasn't
        # possible — so we need ffprobe to check for a brandable text sub track.
        ffprobe = find_ffprobe() if mode == "auto" else None
        # A watermark burn is a full re-encode. Keep it FAST — a corner mark's
        # quality is dominated by the source, so a quicker preset costs nothing
        # visible but saves large wall-clock. Config wins; floor at "faster".
        preset = getattr(self.c.config.processing, "encode_preset", "faster") or "faster"
        crf = (getattr(self.c.config.processing, "encode_crf", None)
               or {}).get(1080, 21)
        # Fair CPU slice so N admins can watermark at once without thrashing:
        # cores / concurrent-jobs, floor 2. Config override wins.
        import os as _os
        _cfg_threads = getattr(self.c.config.processing, "encode_threads", 0)
        if _cfg_threads and _cfg_threads > 0:
            wm_threads = _cfg_threads
        else:
            cores = _os.cpu_count() or 4
            jobs = max(1, getattr(self.c.config.downloads, "concurrent_downloads", 5))
            wm_threads = max(2, cores // jobs)
        # Watermark ONLY the highest-resolution files on disk (the torrent
        # source, typically 1080p). EncodeStage runs AFTER this and derives the
        # 720p/480p tiers FROM the already-watermarked source, so those tiers
        # inherit the mark for free and must never be watermarked again. Marking
        # only the source also means one burn per episode, not one per tier.
        def _res_h(mf) -> int:
            r = (mf.resolution or "").rstrip("p")
            return int(r) if r.isdigit() else 0

        on_disk = [f for f in ctx.files
                   if f.local_path and Path(f.local_path).exists()]
        max_h = max((_res_h(f) for f in on_disk), default=0)
        targets = [f for f in on_disk if _res_h(f) == max_h and max_h > 0] or on_disk
        n = len(targets)
        await _push_stage_progress(self.c, ctx, "Watermarking", 0.0, file_index=0, file_total=n)
        for i, f in enumerate(targets):
            if await source_abort_requested(self.c, ctx.job_id):
                raise AbortSource()  # 'Change source' — bail mid-watermark
            if not f.local_path:
                continue
            src = Path(f.local_path)
            # ── "auto" mode: skip the watermark ONLY when subtitle branding
            #    fully covered this file — i.e. EVERY subtitle track is a
            #    brandable TEXT track (ASS/SRT/VTT/…) that carries our channel
            #    cue. If ANY track is a non-brandable image sub (PGS/DVBSUB) — or
            #    there are no subs at all — branding is only partial, so the file
            #    still needs the visible burn. (Previously used any(), which
            #    skipped the burn as soon as a single text sub existed, leaving
            #    files with mixed text+PGS subs unbranded on the image tracks.)
            if mode == "auto" and ffprobe:
                subs = await _sub_tracks(ffprobe, src)
                fully_branded = bool(subs) and all(
                    is_text_sub(t.get("codec", "")) for t in subs)
                if fully_branded:
                    log.info("watermark.auto_skip", file=src.name,
                             reason="all_subs_text_branded",
                             sub_tracks=len(subs))
                    ctx.notes.append(
                        f"watermark: skipped {src.name} (auto — all subs branded)")
                    continue
                n_text = sum(1 for t in subs if is_text_sub(t.get("codec", "")))
                log.info("watermark.auto_burn", file=src.name,
                         reason="partial_or_no_text_subtitle",
                         sub_tracks=len(subs), text_subs=n_text)
            # Tag the card with THIS file's identity + "file i of n" as we start
            # it, so the watermark pass walks episode-by-episode like downloads.
            await _push_stage_progress(
                self.c, ctx, "Watermarking", 0.0,
                file_index=i + 1, file_total=n,
                episode=f.episode, season=f.season,
                resolution=f.resolution, audio=_audio_str(f.audio),
            )
            out = src.with_name(src.stem + ".wm" + src.suffix)
            flt, extra_inputs = self._filter(w)
            # Burning a watermark forces a full video re-encode. Build a ROBUST
            # encoder ladder (best→safest) so ANY source — h264/hevc/av1/vp9/…,
            # 8-bit or 10-bit — gets watermarked and never silently skipped:
            #   • fast (default): hardware encoder matched to the source bit depth
            #     (hevc_nvenc for 10-bit, h264_nvenc for 8-bit) → software x265/
            #     x264 fallback. GPU-fast when available, always succeeds.
            #   • quality: codec-matched software → libx264 last resort.
            # The source's real pixel format decides the ladder — a 10-bit HEVC
            # anime source stays 10-bit (via hevc_nvenc / libx265) instead of
            # being force-degraded to 8-bit or failing on an 8-bit-only encoder.
            pix_fmt = await probe_pixel_format(src)
            src_10 = is_10bit(pix_fmt)
            candidates = await build_watermark_encode_args(
                ffmpeg or "", src, out, flt, extra_inputs,
                crf=crf, preset=preset, threads=wm_threads,
                fast=bool(getattr(w, "fast", True)), is_10bit=src_10,
            )
            if not candidates:
                ctx.notes.append("watermark: ffmpeg unavailable")
                out.unlink(missing_ok=True)
                continue

            # Live per-file heartbeat: stream ffmpeg's -progress so THIS file's
            # own 0→100 fraction drives the bar (like the download/encode cards)
            # AND the snapshot TTL stays fresh through a long burn — without this
            # a single >10-min file let the snapshot lapse and the card froze on
            # "Downloading 0%". Throttled to ~2s.
            dur_s = await probe_duration_s_async(src)
            _hb = {"t": 0.0}

            async def _wm_progress(frac, _ep=f.episode, _sea=f.season,
                                   _res=f.resolution, _aud=_audio_str(f.audio),
                                   _i=i):  # noqa: ANN001
                import time as _t
                now = _t.monotonic()
                if now - _hb["t"] < 2.0:
                    return
                _hb["t"] = now
                pct = min(99.0, max(0.0, (frac if frac is not None else 0.0) * 100))
                await _push_stage_progress(
                    self.c, ctx, "Watermarking", pct,
                    file_index=_i + 1, file_total=n,
                    episode=_ep, season=_sea, resolution=_res, audio=_aud,
                )

            # Each ladder candidate that fails logs its encoder + ffmpeg error
            # tail before falling through to the next, so a rejected hardware
            # flag or bad filter is diagnosable rather than silently skipped.
            async def _on_cand_fail(encoder, exc, _src=src):  # noqa: ANN001
                log.warning(
                    "watermark.candidate_failed",
                    job_id=getattr(ctx, "job_id", None),
                    file=_src.name, encoder=encoder,
                    error=str(exc)[:400],
                )

            try:
                used = await run_watermark(
                    candidates, on_progress=_wm_progress, duration_s=dur_s,
                    on_candidate_fail=_on_cand_fail,
                )
                log.info("watermark.encoded", file=src.name, encoder=used,
                         source_10bit=src_10)
            except Exception as exc:  # noqa: BLE001 — whole ladder exhausted
                # LOUD: every candidate failed. A silently-skipped watermark
                # reads as "watermarking never happened" (stage flips straight to
                # thumbnail) — surface the final error + pixel format so the cause
                # is diagnosable.
                log.warning(
                    "watermark.file_failed",
                    job_id=getattr(ctx, "job_id", None),
                    file=src.name, pix_fmt=pix_fmt or "?",
                    candidates=[c[0] for c in candidates],
                    error=str(exc)[:400],
                )
                ctx.notes.append(f"watermark: {str(exc)[:200] or 'ffmpeg unavailable'}")
                out.unlink(missing_ok=True)
                continue
            try:
                out.replace(src)  # swap in the watermarked file
            except OSError as exc:
                ctx.notes.append(f"watermark swap failed: {exc}")
            await _push_stage_progress(
                self.c, ctx, "Watermarking", 100.0,
                file_index=i + 1, file_total=n,
                episode=f.episode, season=f.season,
                resolution=f.resolution, audio=_audio_str(f.audio),
            )


# Name of the shared poster thumbnail dropped in a request's work folder. The
# uploader looks for this sibling and attaches it to every file in the pack.
POSTER_THUMB_NAME = "poster.jpg"


def _log_thumb_live_fallback(ctx: StageContext) -> None:
    """Loud marker that the file thumbnail had to hit TMDB live — reached ONLY
    when neither an AniList cover nor a cached TMDB poster was prefetched. The
    operator wants file thumbnails on AniList posters, so any occurrence of this
    log points at a prefetch gap (metadata folder missing / AniList had no
    cover) rather than a normal code path."""
    log.warning(
        "thumbnail.tmdb.live_fallback",
        code=getattr(ctx.request, "code", None),
        anime=getattr(ctx.request, "anime_title", None),
        hint="no prefetched AniList/TMDB cover — check metadata prefetch",
    )


class ThumbnailStage(Stage):
    stage = ProcessingStage.THUMBNAIL

    def enabled(self) -> bool:
        return self.c.config.features.thumbnail_generation and self.c.config.thumbnail.enabled

    async def process(self, ctx: StageContext) -> None:
        """Produce one poster.jpg for the whole request that the storage uploader
        attaches as the Telegram document thumbnail for every file.

        Source priority — the prefetched cache FIRST so we never re-hit the
        network for artwork we already downloaded at acceptance:
          1. The request's own AniList cover (local file, else hosted backup)
             from ``anilist_images.json``.
          2. A prefetched TMDB poster (local, else hosted backup) from
             ``assets.json``.
          3. A LIVE TMDB lookup (only when nothing was prefetched).
          4. A frame-grab from the first file.

        The image is fit within 320×320 JPEG (Telegram's thumbnail limit)."""
        first = next((f for f in ctx.files if f.local_path), None)
        if first is None:
            return
        thumb = Path(first.local_path).with_name(POSTER_THUMB_NAME)
        await _push_stage_progress(self.c, ctx, "Fetching Poster", 0.0)

        # ── 1 & 2: prefetched cache (AniList cover, else cached TMDB poster) ──
        # anilist_id of THIS request's entry, so the shared poster still prefers
        # the correct installment's cover when one was cached.
        aid = None
        try:
            fd = ctx.request.franchise_data or {}
            raw_aid = fd.get("anilist_id") or fd.get("season_anilist_id")
            aid = int(raw_aid) if raw_aid is not None else None
        except (TypeError, ValueError):
            aid = None
        try:
            from nekofetch.services.download_service import _safe_anime_doc_id
            from nekofetch.services.metadata_prefetch import resolve_cached_cover

            code = ctx.request.code
            anime_doc_id = _safe_anime_doc_id(ctx.request)
            # 1: AniList cover FIRST — pass no ``kind`` so resolve_cached_cover
            # returns an AniList cover (this entry's, else the root cover) and
            # does NOT substitute a TMDB poster on an AniList miss. This is what
            # keeps the file thumbnail on the AniList poster whenever ANY AniList
            # cover was prefetched.
            cached = await resolve_cached_cover(
                self.c, code, anilist_id=aid, anime_doc_id=anime_doc_id,
            )
            if cached and await self._fit_thumb(cached, thumb):
                ctx.notes.append("thumbnail: AniList cover (prefetch)")
                await _push_stage_progress(self.c, ctx, "Fetching Poster", 100.0)
                return
            # 2: cached TMDB poster — only reached when AniList had nothing.
            cached_tmdb = await resolve_cached_cover(
                self.c, code, kind="poster", anime_doc_id=anime_doc_id,
            )
            if cached_tmdb and await self._fit_thumb(cached_tmdb, thumb):
                ctx.notes.append("thumbnail: cached TMDB poster (prefetch)")
                await _push_stage_progress(self.c, ctx, "Fetching Poster", 100.0)
                return
        except Exception as exc:  # noqa: BLE001 - cache miss → live path below
            ctx.notes.append(f"thumbnail: cache lookup failed ({exc})")

        # ── 3: live TMDB (only reached on a full AniList + TMDB cache miss) ──
        poster_url = None
        try:
            poster_url = await self.c.tmdb.poster_for(ctx.request.anime_title)
        except Exception as exc:  # noqa: BLE001
            ctx.notes.append(f"thumbnail: tmdb lookup failed ({exc})")
        if poster_url and await self._fit_thumb(poster_url, thumb):
            ctx.notes.append("thumbnail: TMDB poster (live)")
            _log_thumb_live_fallback(ctx)
            await _push_stage_progress(self.c, ctx, "Fetching Poster", 100.0)
            return

        # ── 4: frame from the first file, fit to the same thumbnail box ──
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
    """Derive only the lower-resolution renditions a source did NOT already ship.

    Sources now download every quality they provide (nyaa/ddl no longer collapse
    to 1080p-only), so encoding is a gap-filler, not a blanket down-transcode:
    for each (season, episode, audio) unit we skip any ``encode_heights`` tier
    already present on disk and derive only the genuinely-missing ones from the
    highest present file. Net: 1080+720 shipped → encode 480 only; 720 only →
    encode 480; all three shipped → encode nothing. Runs AFTER
    rename/brand/metadata/watermark so every rendition inherits the final name
    and branding, and BEFORE store so the new files are marked processed and
    uploaded alongside the source. Video is re-encoded (x264 CRF); all audio
    tracks, subtitles and metadata are copied, so dual-audio and branding
    survive. Streaming sources are skipped entirely.
    """

    stage = ProcessingStage.ENCODE

    def enabled(self) -> bool:
        return self.c.config.processing.encode

    async def process(self, ctx: StageContext) -> None:
        if (ctx.request.source or "").lower() not in _TORRENT_SOURCES:
            ctx.notes.append(f"encode: skipped (source {ctx.request.source})")
            return

        from sqlalchemy import inspect as _sa_inspect

        from nekofetch.core.exceptions import ProcessingError
        from nekofetch.infrastructure.database.postgres.models import MediaFile
        from nekofetch.sources._hls import find_ffmpeg, find_ffprobe
        from nekofetch.sources._transcode import _encode

        heights = [h for h in self.c.config.processing.encode_heights if h > 0]
        if not heights:
            return

        # Release order is part of the encode contract. Normalize before
        # selecting source files so combined torrents cannot make processing run
        # S1E03 → S1E01 or S1P2 before S1P1.
        ctx.files.sort(key=release_key)

        # Only encode files that are actually present on disk this pass. To stay
        # safe across reprocess passes (where rows are reloaded from the DB and
        # the in-memory ``_is_rendition`` marker is gone), encode ONLY the
        # highest-resolution files on disk — derived lower tiers are never a
        # source, so we can't re-encode our own outputs.
        on_disk = [
            f for f in ctx.files
            if f.local_path and Path(f.local_path).exists()
        ]
        if not on_disk:
            return

        def _res_h(f) -> int:
            r = (f.resolution or "").rstrip("p")
            return int(r) if r.isdigit() else 0

        # Resolution-reversal: sources now DOWNLOAD every quality they provide, so
        # a tier we already have on disk must NOT be re-derived by encoding — that
        # was the "great mistake" (encode 720/480 from 1080 even when the release
        # already shipped them). Build the set of heights already present for each
        # (season, episode, audio) unit; the encode loop skips any target tier
        # that's in it. Net: 1080+720 present → encode 480 only; 720 only → encode
        # 480; all three present → encode nothing.
        present: dict[tuple, set[int]] = {}
        for f in on_disk:
            h = _res_h(f)
            if h > 0:
                present.setdefault(
                    (f.season, f.season_part, f.episode, f.audio),
                    set(),
                ).add(h)

        max_h = max((_res_h(f) for f in on_disk), default=0)
        sources = [f for f in on_disk if _res_h(f) == max_h and max_h > 0]
        if not sources:
            return

        # A target tier counts as "already provided" when the exact height is on
        # disk OR an acceptable substitute for that slot shipped instead — the
        # operator's rule: a 540p/360p file fills the 480p slot, so we must NOT
        # also derive a 480p (that would duplicate the SD tier). Mirrors the
        # download-side ``resolution_fallbacks`` ladder. Shared with the DDL/torrent
        # mapping card via ``tier_gapfill`` so the preview can't drift from here.
        from nekofetch.services.tier_gapfill import parse_fallbacks, tier_satisfied

        _acq = getattr(self.c.config, "acquisition", None)
        _fallbacks_cfg = getattr(_acq, "resolution_fallbacks", None) or {}
        _subs = parse_fallbacks(_fallbacks_cfg)

        def _tier_satisfied(unit_key: tuple, target_h: int) -> bool:
            return tier_satisfied(present.get(unit_key, set()), target_h, _subs)

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
        # Fair CPU slice per encode so N admins' jobs can encode at once without
        # thrashing: cores / concurrent-jobs, floor 2. Config override wins.
        import os as _os
        _cfg_threads = getattr(self.c.config.processing, "encode_threads", 0)
        if _cfg_threads and _cfg_threads > 0:
            enc_threads = _cfg_threads
        else:
            cores = _os.cpu_count() or 4
            jobs = max(1, getattr(self.c.config.downloads, "concurrent_downloads", 5))
            enc_threads = max(2, cores // jobs)
        # Every (file, height) pair is one encode — count them all so the bar
        # advances per rendition, not per file (a single file with 2 tiers used
        # to jump 0→50→100 with a long silent gap between). Skip any tier
        # already present on disk (the reversal: don't re-derive what we
        # downloaded).
        total_units = sum(
            1 for f in sources for h in heights
            if f"{h}p" != (f.resolution or "1080p")
            and h < max_h  # NEVER up-encode: only derive tiers below the source
            and not _tier_satisfied(
                (f.season, f.season_part, f.episode, f.audio), h
            )
        )
        # All requested tiers already shipped (e.g. a DDL pack that provided
        # 480/720/1080) → there is NOTHING to encode. Return BEFORE the
        # "Encoding" banner so the card doesn't falsely show an encode pass on a
        # ready release; the flow goes straight brand → watermark → thumbnail →
        # store/upload.
        if total_units == 0:
            ctx.notes.append("encode: all requested tiers already present — skipped")
            return
        done_units = 0
        await _push_stage_progress(self.c, ctx, "Encoding", 0.0, file_index=0, file_total=n)
        new_rows: list = []
        for i, f in enumerate(sources):
            if await source_abort_requested(self.c, ctx.job_id):
                raise AbortSource()  # 'Change source' — bail mid-encode
            src = Path(f.local_path)
            src_res = f.resolution or "1080p"
            ffmpeg = find_ffmpeg()
            # Derived 480p/720p tiers ALWAYS encode as libx264. At these small
            # target sizes x264 "faster" is dramatically quicker than libx265 /
            # SVT-AV1 for indistinguishable perceptual quality — the whole point
            # of the lower tiers is a small, fast, universally-playable file.
            # (The 1080p source stays untouched — we never re-encode it here — so
            # its original HEVC/AV1 efficiency is preserved; only the WATERMARK
            # path re-encodes 1080p and it keeps the codec-aware select_encoder.)
            encoder = "libx264"
            # Rename put the resolution token in the stem (e.g.
            # "... [1080p] ..."); we swap it per rendition so names stay correct.
            stem = src.stem
            for height in heights:
                label = f"{height}p"
                if label == src_res:
                    continue  # never "downscale" to the same tier
                if height >= max_h:
                    continue  # NEVER up-encode a tier at/above the source height
                # Reversal: this tier (or an acceptable substitute — 540p/360p for
                # the 480p slot) was already provided for this unit — skip encoding
                # it (that's the whole point of downloading every provided quality
                # instead of transcoding down from 1080p).
                if _tier_satisfied(
                    (f.season, f.season_part, f.episode, f.audio), height
                ):
                    ctx.notes.append(f"encode {label}: already provided — skipped")
                    continue
                await _push_stage_progress(
                    self.c, ctx, f"Encoding {label}", 0.0,
                    file_index=i + 1, file_total=n,
                    episode=f.episode, season=f.season, resolution=label,
                    audio=_audio_str(f.audio),
                )
                out_stem = _swap_resolution(stem, src_res, label)
                out_path = src.with_name(f"{out_stem}{src.suffix}")

                # ── Resume guard: skip a rendition that's already good ──
                # A crash mid-job can leave earlier renditions fully encoded on
                # disk. Re-encoding them wastes hours. If the output exists and
                # decode-probes clean, adopt it (register a MediaFile row if the
                # DB doesn't have one yet) and move on. A partial/corrupt file
                # from a crash mid-encode fails the probe → delete + re-encode.
                if out_path.exists() and out_path.stat().st_size > 0:
                    ffprobe = find_ffprobe() or "ffprobe"
                    ok, reason = await _ffprobe_ok(ffprobe, out_path)
                    if ok:
                        done_units += 1
                        # Is this rendition already known (loaded from DB into
                        # ctx.files, or added earlier this pass)? Match on the
                        # natural key so we don't insert a duplicate row.
                        existing = next(                             (r for r in list(ctx.files) + new_rows
                              if r.season == f.season
                              and r.season_part == f.season_part
                              and r.episode == f.episode
                              and r.resolution == label and r.audio == f.audio),
                            None,
                        )
                        if existing is None:
                            row = MediaFile(
                                job_id=f.job_id, anime_doc_id=f.anime_doc_id,
                                season=f.season, season_part=f.season_part,
                                episode=f.episode, resolution=label,
                                audio=f.audio, original_name=f.original_name,
                                final_name=out_path.name,
                                local_path=str(out_path),
                                size_bytes=out_path.stat().st_size,
                                container=f.container, verified=True,
                                processed=False, published=False,
                            )
                            new_rows.append(row)
                            if session is not None:
                                session.add(row)
                        ctx.notes.append(f"encode {label}: reused existing (resume)")
                        log.info("encode.resume.reuse", label=label,
                                 file=out_path.name)
                        continue
                    # Corrupt/partial — drop it and encode fresh.
                    ctx.notes.append(
                        f"encode {label}: existing file broken ({reason}) — re-encoding"
                    )
                    out_path.unlink(missing_ok=True)

                # Retry a failing encode a few times (transient ffmpeg/disk
                # hiccups happen). If it STILL fails after _ENCODE_MAX_ATTEMPTS,
                # raise so the whole job goes to the recovery card — the operator
                # can then switch to a different torrent rather than shipping an
                # incomplete quality set.
                last_exc: Exception | None = None
                # Live heartbeat for THIS rendition: the bar shows the CURRENT
                # file's own encode fraction (0→100 per file), tagged with its
                # episode/resolution, so the card walks file-by-file like the
                # download card instead of one batch-global bar. The 2s throttle
                # also keeps the snapshot TTL fresh through a long encode (else
                # the card goes stale → falls back to "Downloading").
                _hb = {"t": 0.0}
                _aud = _audio_str(f.audio)

                async def _enc_progress(frac, _ep=f.episode, _sea=f.season,
                                        _lab=label, _aud=_aud, _i=i):  # noqa: ANN001
                    import time as _t
                    now = _t.monotonic()
                    if now - _hb["t"] < 2.0:
                        return
                    _hb["t"] = now
                    pct = min(100.0, max(0.0, (frac if frac is not None else 0.0) * 100))
                    await _push_stage_progress(
                        self.c, ctx, f"Encoding {_lab}", pct,
                        file_index=_i + 1, file_total=n,
                        episode=_ep, season=_sea, resolution=_lab, audio=_aud,
                    )

                for attempt in range(1, _ENCODE_MAX_ATTEMPTS + 1):
                    try:
                        await _encode(src, out_path, height,
                                      crf_map.get(height, 23), preset=preset,
                                      threads=enc_threads, encoder=encoder,
                                      on_progress=_enc_progress)
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        out_path.unlink(missing_ok=True)  # drop any partial file
                        ctx.notes.append(
                            f"encode {label}: attempt {attempt}/"
                            f"{_ENCODE_MAX_ATTEMPTS} failed ({exc})"
                        )
                        log.warning("encode.retry", label=label, attempt=attempt,
                                    error=str(exc))
                if last_exc is not None:
                    raise ProcessingError(
                        f"encode {label} failed after {_ENCODE_MAX_ATTEMPTS} "
                        f"attempts: {last_exc}"
                    )
                done_units += 1
                if not (out_path.exists() and out_path.stat().st_size > 0):
                    ctx.notes.append(f"encode {label}: empty output")
                    continue
                # Re-point an existing row for this rendition instead of adding a
                # duplicate. A resume after a rolled-back VerifyStage failure can
                # leave a stale row (its file was unlinked, but the row's delete
                # was rolled back with the failing transaction); re-encoding must
                # UPDATE that row's path/size, not stack a second row that would
                # upload the same episode twice.
                existing = next(
                    (r for r in list(ctx.files) + new_rows
                     if r.season == f.season and r.episode == f.episode
                     and r.resolution == label and r.audio == f.audio),
                    None,
                )
                if existing is not None:
                    existing.final_name = out_path.name
                    existing.local_path = str(out_path)
                    existing.size_bytes = out_path.stat().st_size
                    existing.verified = True
                    existing.processed = False
                    existing.published = False
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
                self.c, ctx, "Encoding", 100.0,
                file_index=i + 1, file_total=n,
                episode=f.episode, season=f.season,
                resolution=src_res, audio=_audio_str(f.audio),
            )

        # Make the renditions visible to STORE (and the storage uploader, which
        # reads MediaFile rows for the job) in this same pass.
        ctx.files.extend(new_rows)
        if new_rows:
            ctx.notes.append(f"encode: {len(new_rows)} rendition(s) created")


# Derived-rendition CRFs (mirrors _transcode._CRF; kept local so the stage owns
# its quality knobs). Higher CRF on the small tiers = smaller files at quality
# that still reads "natural" for that resolution: 480p CRF 26 lands well under
# 100 MB, 720p CRF 24 ~100–200 MB from a ~300 MB source. 1080p is never derived
# here (source passthrough); the key exists only for the watermark re-encode.
_ENCODE_CRF = {1080: 21, 720: 24, 480: 26}

# How many times to retry a single failing tier encode before failing the whole
# job (which routes to the recovery card so the operator can switch torrents).
_ENCODE_MAX_ATTEMPTS = 3


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
