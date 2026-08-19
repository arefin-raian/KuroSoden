"""Unit coverage for the shared audio-language resolver (nekofetch/services/audio_langs.py).

The resolver is the single source of truth that every "languages" label funnels
through. These tests pin the behaviour the multi-audio patch depends on:

  * real probed ``audio_langs`` (ISO codes) win and render the TRUE languages,
  * packs with no stored languages fall back to the AudioType enum map
    (legacy Eng/Jpn/Hin behaviour — the owner decision),
  * a mix of probed + enum-only packs composes into one deduped set,
  * ISO codes and full names normalise to the SAME canonical language (so a
    probed "en" pack and an enum "english" pack don't render "English & English"),
  * the compact entry-card label uses 3-letter codes in the same reading order.

Packs are plain SimpleNamespaces — the resolver only reads ``.audio`` and
``.audio_langs`` via getattr, so no DB row is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from nekofetch.domain.enums import AudioType
from nekofetch.services.audio_langs import (
    compact_label,
    language_summary,
    pack_languages,
)


def _pack(audio, audio_langs=None):
    return SimpleNamespace(audio=audio, audio_langs=audio_langs)


# ── pack_languages / language_summary ────────────────────────────────────────

def test_probed_langs_win_over_enum():
    """A pack that stored real languages renders those, NOT the enum default."""
    # MULTI would fall back to Eng/Jpn/Hin, but the real tracks are Eng/Jpn/Kor.
    pack = _pack(AudioType.MULTI, audio_langs=["en", "ja", "ko"])
    assert pack_languages([pack]) == {"english", "japanese", "korean"}
    assert language_summary([pack]) == "English, Japanese & Korean"


def test_enum_fallback_when_no_stored_langs():
    """A pack with no stored languages reads exactly as today (legacy behaviour)."""
    assert language_summary([_pack(AudioType.MULTI)]) == "English, Japanese & Hindi"
    assert language_summary([_pack(AudioType.DUAL_AUDIO)]) == "English & Japanese"
    assert language_summary([_pack(AudioType.SUBBED)]) == "Japanese"
    assert language_summary([_pack(AudioType.DUBBED)]) == "English"


def test_unknown_multi_assumes_eng_jpn_hin():
    """Empty stored langs (falsy) fall through to the enum map, not to blank."""
    # audio_langs=[] is falsy → treated as "unknown" → enum fallback.
    assert language_summary([_pack(AudioType.MULTI, audio_langs=[])]) == (
        "English, Japanese & Hindi"
    )


def test_mixed_probed_and_enum_packs_compose():
    """A probed pack (Korean) unions cleanly with a legacy enum pack (Eng/Jpn)."""
    packs = [
        _pack(AudioType.MULTI, audio_langs=["ja", "ko"]),  # real: Japanese, Korean
        _pack(AudioType.DUAL_AUDIO),                        # enum: English, Japanese
    ]
    assert pack_languages(packs) == {"english", "japanese", "korean"}
    assert language_summary(packs) == "English, Japanese & Korean"


def test_iso_and_full_name_dedupe_to_one_language():
    """A probed 'en' pack and an enum 'english' pack must NOT double-count."""
    packs = [
        _pack(AudioType.DUBBED, audio_langs=["en"]),  # probed English
        _pack(AudioType.DUBBED),                       # enum English
    ]
    assert pack_languages(packs) == {"english"}
    assert language_summary(packs) == "English"


def test_three_letter_iso_codes_from_ffprobe():
    """ffprobe emits 639-2/B ('eng','jpn','kor') — these must canonicalise too."""
    pack = _pack(AudioType.MULTI, audio_langs=["eng", "jpn", "kor"])
    assert pack_languages([pack]) == {"english", "japanese", "korean"}


def test_empty_packs_render_dash():
    assert language_summary([]) == "—"
    assert language_summary(None) == "—"


def test_reading_order_english_japanese_then_rest():
    """Order is English, then Japanese, then the rest alphabetically."""
    pack = _pack(AudioType.MULTI, audio_langs=["ko", "ja", "en", "es"])
    # English, Japanese, then Korean/Spanish alphabetical.
    assert language_summary([pack]) == "English, Japanese, Korean & Spanish"


# ── compact_label ─────────────────────────────────────────────────────────────

def test_compact_label_three_letter_codes():
    assert compact_label({"english", "japanese", "korean"}) == "Eng, Jpn & Kor"
    assert compact_label({"english", "japanese"}) == "Eng & Jpn"
    assert compact_label({"korean"}) == "Kor"


def test_compact_label_accepts_iso_codes():
    assert compact_label(["en", "ja", "hi"]) == "Eng, Jpn & Hin"


def test_compact_label_empty():
    assert compact_label(set()) == ""
    assert compact_label(None) == ""


def test_compact_label_unknown_language_first_three_letters():
    # A language with no explicit compact code falls back to Xxx (title-cased).
    assert compact_label({"portuguese"}) == "Por"
