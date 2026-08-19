"""Single source of truth for turning storage packs into a language label.

Historically every user-facing "languages" string in KuroSoden re-derived the
languages from the lossy :class:`AudioType` enum via its own hardcoded map that
assumed ``MULTI = English/Japanese/Hindi`` (and ``DUAL = English/Japanese``).
Four such maps had drifted out of sync — ``bot_factory`` even dropped MULTI
entirely — so a genuine English/Japanese/Korean release was mislabelled
"English, Japanese & Hindi" on some surfaces and something else on others.

The processing pipeline probes the *real* per-stream languages from ffprobe and
(as of the multi-audio patch) persists them on ``StoragePack.audio_langs`` /
``MediaFile.audio_langs`` as ISO codes. This module is the one place that:

  * knows the enum → language fallback (``_AUDIO_LANG_FALLBACK``), and
  * resolves a set of packs to a language set, preferring the real probed
    languages per pack and falling back to the enum map only when a pack has
    none stored (old content, or genuinely untagged media).

Every render site funnels through here, so a mix of new (probed) and old (enum)
packs composes into one correct label, and there is exactly one map to change.

Owner decisions baked in: keep the Eng/Jpn/Hin enum fallback for packs with no
stored languages (no re-probe/backfill), and assume Eng/Jpn/Hin when a
multi-audio pack's real languages are genuinely unknown.
"""

from __future__ import annotations

from nekofetch.domain.enums import AudioType
from nekofetch.services.bot_naming import language_label

# The canonical enum → language fallback, in canonical full-name (lowercase)
# form. Used only when a pack has no real probed ``audio_langs``. Dub = English
# track, Sub = Japanese track (with English subs), Dual = both, Multi adds Hindi.
_AUDIO_LANG_FALLBACK: dict[AudioType, set[str]] = {
    AudioType.DUBBED: {"english"},
    AudioType.SUBBED: {"japanese"},
    AudioType.DUAL_AUDIO: {"english", "japanese"},
    AudioType.MULTI: {"english", "japanese", "hindi"},
}

# ISO 639-1 (2-letter) and 639-2 (3-letter) codes AND full names → canonical
# full name. ffprobe emits 639-2/B codes ("eng", "jpn"); older call sites feed
# full names or 2-letter codes. Normalising both forms to ONE canonical name
# means a union of a probed pack ("en") and an enum-fallback pack ("english")
# dedupes to a single "English" instead of rendering "English & English".
_CANON: dict[str, str] = {
    "english": "english", "en": "english", "eng": "english",
    "japanese": "japanese", "ja": "japanese", "jpn": "japanese", "jp": "japanese",
    "hindi": "hindi", "hi": "hindi", "hin": "hindi",
    "korean": "korean", "ko": "korean", "kor": "korean",
    "chinese": "chinese", "zh": "chinese", "zho": "chinese", "chi": "chinese",
    "spanish": "spanish", "es": "spanish", "spa": "spanish",
}

# Canonical full name → 3-letter display code for the space-constrained entry
# card. Unknown languages fall back to their first three letters, title-cased.
_COMPACT: dict[str, str] = {
    "english": "Eng", "japanese": "Jpn", "hindi": "Hin",
    "korean": "Kor", "chinese": "Chi", "spanish": "Spa",
}

# Reading-order priority: English first, then Japanese, then the rest
# alphabetically — matches bot_naming.language_label so the compact card and the
# full labels agree on ordering ("Eng, Jpn & Kor", never "Eng, Kor & Jpn").
_PRIORITY: dict[str, int] = {"english": 0, "japanese": 1}


def _canon(lang: str) -> str:
    """Normalise one language token (ISO code or name) to its canonical name."""
    key = str(lang).strip().lower()
    return _CANON.get(key, key)


def pack_languages(packs) -> set[str]:
    """Union the real audio languages across ``packs`` → canonical full names.

    Per-pack resolution: a pack with stored ``audio_langs`` contributes those
    (normalised); a pack without contributes its ``AudioType`` enum fallback. So
    a set mixing freshly-probed packs and legacy enum-only packs composes into
    one clean language set with no duplicates and no double-counting.
    """
    out: set[str] = set()
    for pack in packs or []:
        stored = getattr(pack, "audio_langs", None)
        if stored:
            out.update(_canon(x) for x in stored if x and str(x).strip())
        else:
            audio = getattr(pack, "audio", None)
            out.update(_AUDIO_LANG_FALLBACK.get(audio, set()))
    return out


def language_summary(packs) -> str:
    """Full human-readable language line for a set of packs, or ``"—"`` if none.

    e.g. ``"English, Japanese & Korean"``. Delegates ordering + Oxford join to
    :func:`bot_naming.language_label`, so this stays the single resolver while
    that stays the single renderer.
    """
    return language_label(pack_languages(packs)) or "—"


def compact_label(langs) -> str:
    """3-letter Oxford-joined codes for the space-constrained entry card.

    ``{"english","japanese","korean"}`` → ``"Eng, Jpn & Kor"``; a single
    language renders bare (``"Eng"``). Same reading order as
    :func:`language_label`. Accepts ISO codes or full names.
    """
    canon = {_canon(l) for l in (langs or set()) if l and str(l).strip()}
    if not canon:
        return ""
    ordered = sorted(canon, key=lambda l: (_PRIORITY.get(l, 2), l))
    codes = [_COMPACT.get(l, l[:3].title()) for l in ordered]
    if len(codes) == 1:
        return codes[0]
    return " & ".join([", ".join(codes[:-1]), codes[-1]])


def caption_langs(langs) -> str:
    """Uppercase ``+``-joined codes for the storage-pack caption line.

    ``{"english","japanese","hindi"}`` → ``"ENG + JPN + HIN"`` — the exact form
    ``bot_naming._AUDIO_CAPTION`` hardcoded for DUAL/MULTI, but derived from the
    REAL languages. Same reading order as the other labels. Accepts ISO codes or
    full names; returns ``""`` when there's nothing to show.
    """
    canon = {_canon(l) for l in (langs or set()) if l and str(l).strip()}
    if not canon:
        return ""
    ordered = sorted(canon, key=lambda l: (_PRIORITY.get(l, 2), l))
    return " + ".join(_COMPACT.get(l, l[:3].title()).upper() for l in ordered)
