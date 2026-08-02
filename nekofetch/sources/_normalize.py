"""Release normalization — make every downloaded file carry OUR metadata.

Applied after download for any source. It:
  * extracts every embedded **text** subtitle track (ASS/SRT/VTT/…),
  * detects each track's language (existing tag first, else content analysis),
  * processes each with the shared subtitle pipeline (watermark strip, our
    styling, Telegram branding in the longest gap excluding the last 3 min),
  * relabels subtitle AND audio tracks with our naming policy:
        known language  ->  "<Language> - @AniXWeebs"
        subtitle unknown ->  "@AniXWeebs"
        audio unknown    ->  "Anime Weebs #1 - @AniXWeebs"
  * remuxes video + (relabeled) audio + ONLY our processed subtitles, and sets
    the container title to our brand — dropping the source's original subtitles
    and caption/title entirely.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.sources._branding import (
    BRAND_HANDLE,
    ENCODED_BY_TAG,
    brand_audio_title,
    brand_container_title,
    brand_subtitle_title,
)
from nekofetch.sources._hls import find_ffmpeg, find_ffprobe
from nekofetch.sources._mux import _is_english, iso639_2
from nekofetch.sources._subs import _TAG_RE, process_subtitle

log = get_logger(__name__)

# Subtitle codecs we can convert to text/VTT (image-based subs are skipped).
_TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}
_IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub"}

# canonical 2-letter -> display name
_LANG_NAME = {
    "en": "English", "ja": "Japanese", "hi": "Hindi", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "ru": "Russian", "ar": "Arabic", "ko": "Korean", "zh": "Chinese",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
    "tr": "Turkish", "pl": "Polish", "nl": "Dutch", "he": "Hebrew",
}
# 3-letter / locale tag -> canonical 2-letter
_TO_ISO2 = {
    "eng": "en", "jpn": "ja", "jp": "ja", "hin": "hi", "spa": "es", "fre": "fr",
    "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "por": "pt", "rus": "ru",
    "ara": "ar", "kor": "ko", "zho": "zh", "chi": "zh", "tha": "th", "vie": "vi",
    "ind": "id", "msa": "ms", "tur": "tr", "pol": "pl", "nld": "nl", "dut": "nl",
    "heb": "he",
}
# canonical 2-letter -> ISO 639-2 (for Matroska language metadata)
_TO_ISO3 = {
    "en": "eng", "ja": "jpn", "hi": "hin", "es": "spa", "fr": "fra", "de": "deu",
    "it": "ita", "pt": "por", "ru": "rus", "ar": "ara", "ko": "kor", "zh": "zho",
    "th": "tha", "vi": "vie", "id": "ind", "ms": "msa", "tr": "tur", "pl": "pol",
    "nl": "nld", "he": "heb",
}


def _iso3(code: str | None) -> str:
    """Canonical 2-letter -> ISO 639-2, falling back to the muxer's map."""
    if not code:
        return "und"
    return _TO_ISO3.get(code) or iso639_2(code)


def _norm_lang(tag: str | None) -> str | None:
    """Normalize a stream language tag to canonical 2-letter, or None if unknown."""
    if not tag:
        return None
    t = tag.lower().split("-")[0].strip()
    if t in ("und", "unknown", "", "mis", "zxx"):
        return None
    if t in _LANG_NAME:
        return t
    return _TO_ISO2.get(t)


# Script ranges that pin a language outright.
_SCRIPTS = [
    ("ja", re.compile(r"[぀-ヿ]")),       # hiragana/katakana
    ("ko", re.compile(r"[가-힣]")),        # hangul
    ("hi", re.compile(r"[ऀ-ॿ]")),        # devanagari
    ("ar", re.compile(r"[؀-ۿ]")),        # arabic
    ("he", re.compile(r"[֐-׿]")),        # hebrew
    ("th", re.compile(r"[฀-๿]")),        # thai
    ("ru", re.compile(r"[Ѐ-ӿ]")),        # cyrillic
    ("zh", re.compile(r"[一-鿿]")),        # CJK ideographs (no kana)
]
# Latin-script disambiguation by common function words.
_STOP = {
    "en": {"the", "and", "you", "that", "this", "with", "what", "have", "your", "not"},
    "es": {"que", "los", "una", "por", "con", "para", "esto", "pero", "como", "más"},
    "fr": {"les", "une", "des", "est", "pas", "vous", "pour", "que", "dans", "qui"},
    "pt": {"que", "não", "uma", "com", "para", "isso", "você", "como", "mais", "está"},
    "de": {"und", "der", "die", "das", "ich", "nicht", "ist", "wir", "sie", "ein"},
    "it": {"che", "non", "una", "per", "sono", "questo", "come", "più", "con", "del"},
    "id": {"yang", "dan", "ini", "itu", "tidak", "untuk", "dengan", "saya", "kamu"},
}


def detect_language(text: str) -> str | None:
    """Best-effort language code from subtitle text (script + function words)."""
    sample = _TAG_RE.sub("", text)
    sample = re.sub(r"\d\d:\d\d:\d\d[.,]\d+|-->|WEBVTT|\bDialogue:\b", " ", sample)
    if len(sample.strip()) < 20:
        return None
    for code, rx in _SCRIPTS:
        if len(rx.findall(sample)) >= 3:
            return code
    words = re.findall(r"[a-zàâäéèêëïîôùûüçñ']+", sample.lower())
    if not words:
        return None
    wordset = set(words)
    scores = {lang: len(wordset & stops) for lang, stops in _STOP.items()}
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best
    # Latin script, no strong signal -> assume English (most common for anime subs)
    return "en" if len(words) > 30 else None


def detect_audio_config(langs: list[str | None]) -> tuple[str, bool]:
    """Derive the release audio config from the audio streams actually present.

    Returns ``(config, certain)``:
      * 3+ audio tracks            -> "Multi"  (certain)
      * 2 audio tracks             -> "Dual"   (certain)
      * 1 Japanese track           -> "Sub"    (certain)
      * 1 English/other-known track-> "Dub"    (certain)
      * 1 unknown / 0 tracks       -> "Sub"    (uncertain → caller should confirm)
    """
    n = len(langs)
    if n >= 3:
        return "Multi", True
    if n == 2:
        return "Dual", True
    if n == 1:
        lang = langs[0]
        if lang == "ja":
            return "Sub", True
        if lang:                      # English or any other identified dub
            return "Dub", True
        return "Sub", False           # single track, language unknown
    return "Sub", False               # no audio detected


def track_title(lang: str | None, ordinal: int) -> str:
    """Title for an AUDIO track (manual re-mux path).

    Delegates to :func:`nekofetch.sources._branding.brand_audio_title` so the
    audio bracket style (``『 @AniXWeebs 』``) stays identical across the
    new-download path, the manual re-mux path, and the dual-audio path.

    Known language → ``"Language『 @AniXWeebs 』"``.
    Unknown       → ``"Audio Track 〢N『 @AniXWeebs 』"`` where N is the 1-based
                    ordinal of this track among the audio streams.
    """
    return brand_audio_title(_LANG_NAME.get(lang) if lang else None, ordinal)


# Noise to strip when deriving a clean release title for the container.
_TITLE_NOISE = re.compile(
    r"(\[[^\]]*\]|\([^)]*\)|@\w+|\b\d{3,4}p\b|\b(?:dual|sub|subbed|dub|dubbed|"
    r"multi|x26[45]|hevc|av1|10bit|web|webrip|bluray|bd)\b|\.(mkv|mp4|avi))",
    re.IGNORECASE,
)


def release_title(name: str) -> str:
    """Derive a clean, human release title from a filename for the container's
    ``title`` tag (the container is the MKV wrapper, NOT a media stream)."""
    s = _TITLE_NOISE.sub(" ", name)
    s = re.sub(r"[\s._-]+", " ", s).strip(" -–—:|")
    return s or "Anime Weebs"


def _ffprobe_streams(path: Path) -> list[dict]:
    ffprobe = find_ffprobe()
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    try:
        return json.loads(r.stdout).get("streams", [])
    except json.JSONDecodeError:
        return []


def probe_audio_config(path: Path) -> tuple[str, bool]:
    """ffprobe ``path`` and return its (audio_config, certain) — Dual/Multi/Sub/Dub.

    Lets a caller put the right tag in the filename before normalizing.
    """
    langs = [_norm_lang(s.get("tags", {}).get("language"))
             for s in _ffprobe_streams(path) if s.get("codec_type") == "audio"]
    return detect_audio_config(langs)


def _duration_ms(path: Path) -> int | None:
    ffprobe = find_ffprobe()
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(float(r.stdout.strip()) * 1000)
    except (ValueError, AttributeError):
        return None


async def normalize_release(src: Path, dest: Path, *, title: str | None = None,
                            audio_config: str | None = None,
                            franchise_title: str | None = None) -> dict:
    """Normalize ``src`` into ``dest`` (.mkv) with our metadata + processed subs.

    ``title`` sets the per-episode caption label; ``franchise_title`` (when
    given) is used for the container's title tag (the MKV wrapper itself).
    If ``franchise_title`` is omitted it falls back to ``title``, then the
    source filename. ``audio_config``
    (Dual/Multi/Sub/Dub) overrides auto-detection; otherwise it is detected from
    the audio streams and the report flags ``audio_config_certain``.
    """
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    if not (ffmpeg and ffprobe):
        raise RuntimeError("ffmpeg/ffprobe required for normalization")

    # Capture the container title — prefer franchise_title, then title, then filename.
    base_title = franchise_title or title or release_title(src.name)

    streams = _ffprobe_streams(src)
    video_ms = _duration_ms(src)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    work = dest.parent
    work.mkdir(parents=True, exist_ok=True)
    tmp: list[Path] = []
    seen_sigs: set[str] = set()   # dedup identical subtitle tracks
    report: dict = {"audio": [], "subtitles": [], "skipped": []}

    # ---- extract + process each text subtitle ----
    processed: list[tuple[Path, str, str | None]] = []  # (ass, title, lang)
    for n, s in enumerate(subs):
        codec = s.get("codec_name", "")
        if codec in _IMAGE_SUB_CODECS or codec not in _TEXT_SUB_CODECS:
            report["skipped"].append({"index": s["index"], "codec": codec,
                                      "reason": "non-text subtitle"})
            continue
        vtt = work / f".norm.{dest.stem}.{n}.vtt"
        proc = subprocess.run(  # noqa: ASYNC221 - quick per-track extract
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
             "-map", f"0:{s['index']}", "-c:s", "webvtt", str(vtt)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not vtt.exists() or vtt.stat().st_size == 0:
            report["skipped"].append({"index": s["index"], "codec": codec,
                                      "reason": "extract failed"})
            continue
        tmp.append(vtt)
        tag_lang = _norm_lang(s.get("tags", {}).get("language"))
        lang = tag_lang or detect_language(vtt.read_text(encoding="utf-8", errors="replace"))
        meta = process_subtitle(vtt, video_ms)
        ass = Path(meta["ass"])
        tmp.append(ass)
        sig = meta.get("signature")
        if sig and sig in seen_sigs:
            report["skipped"].append({"index": s["index"], "reason": "duplicate track"})
            continue
        if sig:
            seen_sigs.add(sig)
        # ordinal = this track's 1-based position among the kept subtitle streams
        title = brand_subtitle_title(_LANG_NAME.get(lang) if lang else None,
                                     len(processed) + 1)
        processed.append((ass, title, lang))
        report["subtitles"].append({"lang": lang, "title": title,
                                     "from_tag": bool(tag_lang)})

    # ---- audio relabel plan (ordinal = position among audio streams) ----
    audio_plan: list[tuple[str, str | None]] = []
    for i, s in enumerate(audio):
        lang = _norm_lang(s.get("tags", {}).get("language"))
        track = track_title(lang, i + 1)
        audio_plan.append((track, lang))
        report["audio"].append({"lang": lang, "title": track})

    # audio config: explicit override wins, else detect from the streams.
    if audio_config:
        config, certain = audio_config, True
    else:
        config, certain = detect_audio_config([lang for _t, lang in audio_plan])
    report["audio_config"] = config
    report["audio_config_certain"] = certain
    # Container title: franchise-style name with audio config + brand.
    # e.g. "Attack on Titan〢@AniXWeebs"
    container_title = brand_container_title(base_title)

    # ---- remux: video + relabeled audio + ONLY our subs ----
    out = dest.with_suffix(".mkv")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
    for ass, _t, _l in processed:
        cmd += ["-i", str(ass)]
    cmd += ["-map", "0:v"]
    cmd += ["-map", "0:a"]
    for i in range(len(processed)):
        cmd += ["-map", f"{i + 1}:0"]
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy",
            "-map_metadata", "-1", "-metadata", f"title={container_title}",
            "-metadata", f"ENCODED_BY={ENCODED_BY_TAG}"]
    for i, (title, lang) in enumerate(audio_plan):
        cmd += [f"-metadata:s:a:{i}", f"title={title}"]
        if lang:
            cmd += [f"-metadata:s:a:{i}", f"language={_iso3(lang)}"]
    # Default audio: when dual audio (ja+en), English is the default track.
    # Falls back to the first track for all other configs.
    audio_langs = [lang for _t, lang in audio_plan]
    default_audio = next(
        (i for i, lang in enumerate(audio_langs) if _is_english(lang)), 0
    )
    for i in range(len(audio_plan)):
        cmd += [f"-disposition:a:{i}", "default" if i == default_audio else "0"]
    # English subtitles are the default track (auto-selected on play).
    # When no English sub exists, NO track is default — never force a
    # non-English sub (e.g. Chinese in a dub) onto the viewer.
    default_sub = next(
        (i for i, (_a, _t, lang) in enumerate(processed) if _is_english(lang)), None
    )
    for i, (_ass, title, lang) in enumerate(processed):
        cmd += [f"-metadata:s:s:{i}", f"title={title}"]
        cmd += [f"-metadata:s:s:{i}", f"language={_iso3(lang) if lang else 'und'}"]
        cmd += [f"-disposition:s:{i}", "default" if i == default_sub else "0"]
    cmd += [str(out)]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    for p in tmp:
        p.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"normalize remux failed: {err.decode(errors='replace')[-300:]}")

    report.update(path=str(out), bytes=out.stat().st_size,
                  subtitle_tracks=len(processed), audio_tracks=len(audio_plan))
    return report
