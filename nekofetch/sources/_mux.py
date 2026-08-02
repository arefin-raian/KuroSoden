"""ffmpeg muxing: combine video + all audio tracks + all subtitle tracks.

Produces a single ``.mkv`` (Matroska handles arbitrary audio/subtitle tracks
with per-track language metadata, which mp4 does not). Subtitles are expected as
``.ass`` (already cleaned/styled/branded by :mod:`_subs`) so colours render
identically across players; the styling survives ``-c:s copy``.

Track titles and the container title go through :mod:`_branding` so audio,
subtitle, and container labels all carry the same chrome-bracket style across
the new-download path, the manual re-mux path (``_normalize.py``), and the
cross-source dual-audio path (``_dualaudio.py``):
    Audio track:   ``"Language〘 @AniXWeebs 〙"``
    Subtitle:      same chrome
    Container:     ``"AnimeName〢@AniXWeebs"``
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.sources._branding import (
    ENCODED_BY_TAG,
    brand_audio_title,
    brand_container_title,
    brand_subtitle_title,
    is_meaningful_track_name,
)
from nekofetch.sources._hls import find_ffmpeg, find_ffprobe
from nekofetch.sources._subs import process_subtitle

log = get_logger(__name__)


def _probe_duration_ms(path: Path) -> int | None:
    """Return media duration in ms via ffprobe, or None."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(float(r.stdout.strip()) * 1000)
    except (ValueError, AttributeError):
        return None

# ISO 639-1 → 639-2 for Matroska language tags.
_LANG3: dict[str, str] = {
    "ja": "jpn", "jp": "jpn", "en": "eng", "us": "eng", "es": "spa",
    "ko": "kor", "zh": "zho", "cn": "zho", "fr": "fra", "de": "deu",
    "it": "ita", "pt": "por", "ru": "rus", "ar": "ara", "hi": "hin",
    "th": "tha", "vi": "vie", "id": "ind", "ms": "msa", "tl": "tgl",
}


def iso639_2(code: str) -> str:
    if not code:
        return "und"
    c = code.lower().split("-")[0].strip()
    return _LANG3.get(c, c if len(c) == 3 else "und")


# Language code → display name for track labels (``English``, ``Japanese``).
# Local copy so this module stays import-cycle-free (``_normalize`` imports FROM
# ``_mux``). Accepts both 2-letter and 3-letter codes; unknown → "".
_LANG_DISPLAY: dict[str, str] = {
    "ja": "Japanese", "jpn": "Japanese", "jp": "Japanese",
    "en": "English", "eng": "English", "us": "English",
    "hi": "Hindi", "hin": "Hindi", "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fra": "French", "de": "German", "deu": "German",
    "it": "Italian", "ita": "Italian", "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian", "ko": "Korean", "kor": "Korean",
    "zh": "Chinese", "zho": "Chinese", "cn": "Chinese",
}


def lang_display(code: str | None) -> str:
    """Display name for a language code (``"English"``), or "" if unknown."""
    if not code:
        return ""
    c = code.lower().split("-")[0].strip()
    return _LANG_DISPLAY.get(c, "")


def _is_english(lang: str | None) -> bool:
    """True for any English language code/label (en, en-US, eng, english, …)."""
    c = (lang or "").lower().strip()
    return c.startswith("en") or c == "eng" or c == "english"


# The only audio tracks we ever keep, in priority order.
WANTED_AUDIO = ("ja", "en", "hi")
_AUDIO_ALIASES = {"jpn": "ja", "jp": "ja", "eng": "en", "us": "en", "hin": "hi"}


def normalize_audio_lang(code: str) -> str:
    """Map any audio language code/label to our canonical ja/en/hi (or '')."""
    c = (code or "").lower().split("-")[0].strip()
    c = _AUDIO_ALIASES.get(c, c)
    return c if c in WANTED_AUDIO else ""


def audio_label(langs) -> str:
    """Derive the release label from the set of *present* canonical audio langs.

    Per spec:
        {ja}            -> SUBBED
        {en}            -> DUBBED
        {ja, en}        -> Dual Audio
        {ja, en, hi}    -> Multi Audio

    Edge cases (documented): combinations outside the spec (e.g. {ja, hi},
    {en, hi}, {hi}) fall back by count — 3 langs -> Multi Audio, 2 -> Dual Audio,
    1 -> SUBBED if Japanese else DUBBED.
    """
    s = {x for x in langs if x in WANTED_AUDIO}
    if s == {"ja"}:
        return "SUBBED"
    if s == {"en"}:
        return "DUBBED"
    if s == {"ja", "en"}:
        return "Dual Audio"
    if s == {"ja", "en", "hi"}:
        return "Multi Audio"
    # fallbacks for unusual combinations
    if len(s) >= 3:
        return "Multi Audio"
    if len(s) == 2:
        return "Dual Audio"
    if s == {"ja"} or "ja" in s:
        return "SUBBED"
    return "DUBBED"


async def mux_to_mkv(
    video: Path,
    audios: list[tuple[Path, str, str]],
    subs: list[tuple[Path, str, str]],
    dest: Path,
    *,
    title: str | None = None,
    embedded_audio: tuple[str, str] | None = None,
) -> Path:
    """Mux into a single MKV.

    ``audios``/``subs`` are ``(path, display_name, lang_code)`` tuples for
    external audio/subtitle files. ``embedded_audio`` is ``(name, lang)`` for the
    audio carried inside ``video`` itself (separate-variant case); when given it
    becomes the first audio track. If both are empty, the video's embedded audio
    (if any) is kept untagged. Raises ``RuntimeError`` if ffmpeg is missing or the
    mux fails.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — cannot mux")

    out = dest.with_suffix(".mkv")
    inputs = [video] + [a[0] for a in audios] + [s[0] for s in subs]

    cmd: list[str] = [ffmpeg, "-y", "-loglevel", "error"]
    for p in inputs:
        cmd += ["-i", str(p)]

    cmd += ["-map", "0:v:0"]
    # Build the ordered list of output audio tracks as (name, lang).
    out_audio: list[tuple[str, str]] = []
    if embedded_audio is not None:
        cmd += ["-map", "0:a:0"]
        out_audio.append(embedded_audio)
    for i in range(len(audios)):
        cmd += ["-map", f"{1 + i}:a:0"]
        out_audio.append((audios[i][1], audios[i][2]))
    if not out_audio:
        cmd += ["-map", "0:a?"]  # keep embedded audio if present (untagged)
    sub_base = 1 + len(audios)
    for i in range(len(subs)):
        cmd += ["-map", f"{sub_base + i}:0"]

    # Stream copy everything; .ass subtitles copy losslessly into Matroska.
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]

    for i, (name, lang) in enumerate(out_audio):
        # Audio label: keep a meaningful source/display name, else fall back to
        # the language ("English") — never a generic layout word ("Stereo").
        cmd += [f"-metadata:s:a:{i}", f"language={iso639_2(lang)}",
                f"-metadata:s:a:{i}",
                f"title={brand_audio_title(name, i + 1, fallback_lang=lang_display(lang))}"]
    # Default audio: English is the default track when dual audio.
    # Falls back to the first track for all other configs.
    audio_langs = [lang for _n, lang in out_audio]
    default_audio = next(
        (i for i, lang in enumerate(audio_langs) if _is_english(lang)), 0
    ) if out_audio else 0
    for i in range(len(out_audio)):
        cmd += [f"-disposition:a:{i}", "default" if i == default_audio else "0"]
    for i, (_p, name, lang) in enumerate(subs):
        # Subtitle label: prefer a meaningful source name (fansub "Full Subs"),
        # else the language ("English"), else the bare "〘 By @AniXWeebs 〙".
        sub_name = name if is_meaningful_track_name(name) else lang_display(lang)
        cmd += [f"-metadata:s:s:{i}", f"language={iso639_2(lang)}",
                f"-metadata:s:s:{i}", f"title={brand_subtitle_title(sub_name, i + 1)}"]
    # English subtitles are the default (auto-selected on playback).
    # When no English sub exists, NO track is default — never force a
    # non-English sub onto the viewer.
    if subs:
        default_sub = next(
            (i for i, (_p, _n, lang) in enumerate(subs) if _is_english(lang)), None
        )
        for i in range(len(subs)):
            cmd += [f"-disposition:s:{i}", "default" if i == default_sub else "0"]
    if title:
        # Container title also goes through the brand helper so a caller-supplied
        # "AnimeName" reliably lands as "AnimeName〢@AniXWeebs". If the caller
        # already pre-branded (e.g. ``_normalize.py`` adding the marker itself),
        # the helper detects it and returns the title unchanged.
        cmd += ["-metadata", f"title={brand_container_title(title)}"]
    # Video credit: a container-level ENCODED_BY tag on every muxed release.
    cmd += ["-metadata", f"ENCODED_BY={ENCODED_BY_TAG}"]

    cmd += [str(out)]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            f"ffmpeg mux failed (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace')[-400:]}"
        )
    log.info("mux.ok", out=str(out), audios=len(audios), subs=len(subs),
             bytes=out.stat().st_size)
    return out


async def assemble_final(
    video: Path,
    audios: list[tuple[Path, str, str]],
    subtitles: list[tuple[str, str, Path]],
    dest: Path,
    *,
    title: str | None = None,
    cleanup: bool = True,
    embedded_audio: tuple[str, str] | None = None,
) -> tuple[Path, list[dict]]:
    """Clean/style/brand every subtitle, then mux video+audio+subs into one MKV.

    ``subtitles`` are ``(display_name, lang_code, vtt_path)``. Each VTT is run
    through :func:`process_subtitle` (watermark scrub + standardized styling +
    branding in the longest gap) which also emits an ``.ass`` used for muxing so
    the styling renders everywhere. Intermediate ``.ts`` parts are removed on
    success when ``cleanup`` is set. Returns ``(mkv_path, subtitle_meta)``.
    """
    # True video duration so branding can skip the final 3 minutes.
    video_ms = _probe_duration_ms(video)

    sub_inputs: list[tuple[Path, str, str]] = []
    sub_meta: list[dict] = []
    seen_sigs: dict[str, str] = {}  # signature -> label already kept
    for name, lang, vtt in subtitles:
        try:
            meta = process_subtitle(vtt, video_ms)
            sig = meta.get("signature")
            if sig and sig in seen_sigs:
                # Identical to an already-kept track -> drop the duplicate.
                meta.update(label=name, lang=lang, deduped_against=seen_sigs[sig])
                sub_meta.append(meta)
                Path(meta["ass"]).unlink(missing_ok=True)  # noqa: ASYNC240 - tiny cleanup
                continue
            if sig:
                seen_sigs[sig] = name
            meta.update(label=name, lang=lang)
            sub_meta.append(meta)
            sub_inputs.append((Path(meta["ass"]), name, lang))
        except Exception as exc:  # noqa: BLE001 - a bad sub shouldn't sink the mux
            log.warning("subtitle.process.failed", file=str(vtt), error=str(exc))

    mkv = await mux_to_mkv(
        video, audios, sub_inputs, dest, title=title, embedded_audio=embedded_audio
    )

    if cleanup:
        video.unlink(missing_ok=True)  # noqa: ASYNC240 - tiny local cleanup
        for p, _n, _l in audios:
            p.unlink(missing_ok=True)  # noqa: ASYNC240 - tiny local cleanup
    return mkv, sub_meta
