"""Shared brand-tag helpers for audio / subtitle track + container titles.

Single source of truth for the chrome-bracket title style that's used across
the new-download mux path (``_mux.py``), the manual re-mux path
(``_normalize.py``), the cross-source dual-audio path (``_dualaudio.py``),
and the on-screen subtitle cue in ``_subs.py``.

Visual style — each stream type gets its OWN bracket set (so MediaInfo / VLC /
mpv display a distinct, on-brand label per stream, and the styles never bleed
into each other):

  * Audio track:   ``"Name『 @AniXWeebs 』"``   (U+300E / U+300F)
                   e.g. ``English『 @AniXWeebs 』`` · ``Japanese『 @AniXWeebs 』``
                   e.g. ``Audio Track 〢1『 @AniXWeebs 』`` (no usable name)
  * Subtitle:      ``"Name〘 @AniXWeebs 〙"``    (U+3018 / U+3019)
                   e.g. ``English〘 @AniXWeebs 〙`` · ``Signs & Songs〘 @AniXWeebs 〙``
                   e.g. ``〘 By @AniXWeebs 〙`` (no usable name)
  * Video credit:  ``ENCODED_BY = "Anime Weebs〔 @AniXWeebs 〕"``  (U+3014 / U+3015)
                   — a CONTAINER-level tag, not a per-stream title.
  * Container:     ``"AnimeName〢@AniXWeebs"`` (unchanged — see brand_container_title)

Each per-track bracket wraps the @HANDLE (not the channel name), while the
container title keeps the ``〢@AniXWeebs`` handle suffix.

The terminal-level ``subtitle on-screen cue`` is unaffected by this module;
``_subs.py`` keeps its own text template for the ASS stream.
"""

from __future__ import annotations

# Channel handle. Kept in sync with the form used by ``_subs.py`` (ASS on-screen
# cue), ``_normalize.py`` (transmux path — historically identical), and the
# branding block in ``core/constants.py`` / ``services/bot_factory.py``.
BRAND_HANDLE = "@AniXWeebs"

# Channel display name used in the video ENCODED_BY credit and the container
# title. Mirrors ``branding.channel_name`` in config.yaml; kept as a module
# constant so the pure track-title helpers stay config-free (like ``BRAND_HANDLE``).
BRAND_NAME = "Anime Weebs"

# The video-stream credit, written as a CONTAINER-level ``ENCODED_BY`` tag by
# every mux/remux/encode path (ffmpeg ``-metadata ENCODED_BY=…``). Not a
# per-stream title — MediaInfo surfaces it under the file's "Encoded by" field.
ENCODED_BY_TAG = f"{BRAND_NAME}〔 {BRAND_HANDLE} 〕"

# Generic audio "layout" words a source may put in a track's title (e.g. an HLS
# ``NAME="Stereo"`` rendition, or a muxer default). These carry NO information
# worth branding — we drop them and fall back to the language name instead, so a
# track never reads ``"Stereo『 @AniXWeebs 』"``. Compared case-insensitively.
_GENERIC_AUDIO_NAMES = {
    "", "stereo", "mono", "surround", "audio", "default", "track", "original",
    "und", "dual", "dual audio", "multi", "sub", "dub", "subbed", "dubbed",
    "1.0", "2.0", "2.1", "5.1", "7.1", "aac", "ac3", "eac3", "opus", "flac",
}


def is_meaningful_track_name(name: str | None) -> bool:
    """True when ``name`` is worth showing on a track label.

    False for empty/whitespace-only names and for generic audio layout words
    (``Stereo``, ``5.1``, ``AAC``, a bare release tag like ``Dual Audio``) — the
    caller then falls back to the language name (or an ordinal placeholder).
    """
    t = (name or "").strip()
    if not t:
        return False
    return t.lower() not in _GENERIC_AUDIO_NAMES


# Release-site brands stamped into DDL packs. Matched case-insensitively as a
# SUBSTRING so dotted / suffixed variants are all caught by one token:
#   "moviesmod"  → "MoviesMod.org", "Hindi - MoviesMod"
#   "vegamovies" → "VegaMovies", "VegaMovies.co.ru", "Tamil VegaMovies"
# Single source of truth for BOTH the title matcher (below) and the subtitle-cue
# stripper (``_torrent_subs.strip_release_brand_lines``). Add a new banned site
# here and every strip site — audio/subtitle track titles AND standalone credit
# cues — picks it up with no other change.
_RELEASE_BRAND_TOKENS = ("moviesmod", "vegamovies")


def is_release_brand_title(name: str | None) -> bool:
    """True when a track TITLE is a release-site watermark (MoviesMod, VegaMovies…).

    DDL packs stamp the release site into audio/subtitle track titles
    (e.g. ``"MoviesMod.org"``, ``"Hindi - VegaMovies"``, ``"VegaMovies.co.ru"``).
    That carries no legitimate stream meaning, so a DDL caller treats such a title
    as unusable and falls back to the (detected/tagged) language instead. Compared
    case-insensitively as a substring against :data:`_RELEASE_BRAND_TOKENS`.
    Torrent callers don't apply this (see ``strip_domain``).
    """
    low = (name or "").lower()
    return any(tok in low for tok in _RELEASE_BRAND_TOKENS)


# Back-compat alias: the historical name used at ~8 call sites. It now covers the
# whole brand-token set (MoviesMod + VegaMovies + any future addition), not just
# MoviesMod — kept so those imports don't need to churn.
is_moviesmod_title = is_release_brand_title


def brand_audio_title(name: str | None, ordinal: int,
                      *, fallback_lang: str | None = None) -> str:
    """Audio track label: ``"Name『 @AniXWeebs 』"``.

    Args:
        name: preferred display name — a language word (``"English"``) on
            scraping paths, or the source's original track title on torrent
            paths. Ignored when it isn't :func:`is_meaningful_track_name`.
        ordinal: 1-based position among the audio streams — used for the
            ``"Audio Track 〢N"`` placeholder when nothing usable is available.
        fallback_lang: language display name to prefer over the ordinal
            placeholder when ``name`` isn't meaningful (e.g. a torrent whose
            audio title is the generic word "Stereo" but whose stream is tagged
            ``eng`` → ``"English『 @AniXWeebs 』"``).

    Examples:
        ``English『 @AniXWeebs 』`` · ``Signs『 @AniXWeebs 』`` ·
        ``Audio Track 〢2『 @AniXWeebs 』``
    """
    if is_meaningful_track_name(name):
        base = name.strip()  # type: ignore[union-attr]
    elif fallback_lang and fallback_lang.strip():
        base = fallback_lang.strip()
    else:
        base = f"Audio Track 〢{ordinal}"
    return f"{base}『 {BRAND_HANDLE} 』"


def brand_subtitle_title(name: str | None, ordinal: int) -> str:
    """Subtitle track label: ``"Name〘 @AniXWeebs 〙"``.

    Args:
        name: the subtitle's display name — a language word (``"English"``) on
            scraping paths, or the fansub's original title (``"Signs & Songs"``)
            on torrent paths. When empty/whitespace-only the label is the bare
            ``"〘 By @AniXWeebs 〙"`` (the "language unavailable" form).
        ordinal: 1-based position among the subtitle streams (unused for now;
            kept for signature symmetry with :func:`brand_audio_title`).

    Examples:
        ``English〘 @AniXWeebs 〙`` · ``Full Subs(GJM)〘 @AniXWeebs 〙`` ·
        ``〘 By @AniXWeebs 〙``
    """
    base = name.strip() if name and name.strip() else ""
    if base:
        return f"{base}〘 {BRAND_HANDLE} 〙"
    return f"〘 By {BRAND_HANDLE} 〙"


def brand_container_title(title: str) -> str:
    """Build a stylish container title: ``"AnimeName〢@AniXWeebs"``.

    Idempotent: if the title is ALREADY branded with our separator + handle,
    it's returned unchanged. This guards against double-branding when a
    caller already pre-branded (e.g. ``_normalize.py`` adds the brand
    itself before calling ``mux_to_mkv``).
    """
    marker = f"〢{BRAND_HANDLE}"
    if marker in title:
        return title
    return f"{title}{marker}"
