"""Unit coverage for the per-stream track-title brand helpers (_branding.py).

Each stream type gets its OWN bracket style — and they must never bleed into
each other (the "Stereo《 Anime Weebs 》" / subtitle-styling-on-audio bug):

    * Audio     → ``Name『 @AniXWeebs 』``   (generic name → language → ordinal)
    * Subtitle  → ``Name〘 @AniXWeebs 〙``   (title → language → bare "By")
    * Video     → container ``ENCODED_BY = Anime Weebs〔 @AniXWeebs 〕``
    * Container → ``AnimeName〢@AniXWeebs`` (UNCHANGED — the file title)
"""

from __future__ import annotations

from nekofetch.sources._branding import (
    ENCODED_BY_TAG,
    brand_audio_title,
    brand_container_title,
    brand_subtitle_title,
    is_meaningful_track_name,
)


# ── is_meaningful_track_name ────────────────────────────────────────────────
def test_generic_layout_words_are_not_meaningful():
    for junk in ("", "  ", "Stereo", "stereo", "5.1", "2.0", "Mono", "AAC",
                 "Dual Audio", "default", "und"):
        assert not is_meaningful_track_name(junk), junk


def test_real_titles_are_meaningful():
    for name in ("Full Subs(GJM)", "Signs and Songs", "Commentary", "English"):
        assert is_meaningful_track_name(name), name


# ── brand_audio_title ───────────────────────────────────────────────────────
def test_audio_keeps_meaningful_name():
    assert brand_audio_title("Commentary", 1) == "Commentary『 @AniXWeebs 』"


def test_audio_generic_name_falls_back_to_language():
    # The exact Takopi bug: source title "Stereo" but the stream is eng/jpn.
    assert brand_audio_title("Stereo", 1, fallback_lang="English") == \
        "English『 @AniXWeebs 』"
    assert brand_audio_title("Stereo", 2, fallback_lang="Japanese") == \
        "Japanese『 @AniXWeebs 』"


def test_audio_no_name_no_lang_uses_ordinal():
    assert brand_audio_title("", 1) == "Audio Track 〢1『 @AniXWeebs 』"
    assert brand_audio_title("5.1", 3) == "Audio Track 〢3『 @AniXWeebs 』"


def test_audio_language_passed_as_name():
    # Scraping paths pass the language word directly as ``name``.
    assert brand_audio_title("English", 1) == "English『 @AniXWeebs 』"


# ── brand_subtitle_title ────────────────────────────────────────────────────
def test_subtitle_keeps_original_title():
    assert brand_subtitle_title("Full Subs(GJM)", 1) == \
        "Full Subs(GJM)〘 @AniXWeebs 〙"
    assert brand_subtitle_title("Signs and Songs(GJM)", 2) == \
        "Signs and Songs(GJM)〘 @AniXWeebs 〙"


def test_subtitle_language_name():
    assert brand_subtitle_title("English", 1) == "English〘 @AniXWeebs 〙"


def test_subtitle_no_name_is_bare_by():
    assert brand_subtitle_title("", 1) == "〘 By @AniXWeebs 〙"
    assert brand_subtitle_title("   ", 2) == "〘 By @AniXWeebs 〙"


# ── styles never collide ────────────────────────────────────────────────────
def test_audio_and_subtitle_styles_are_distinct():
    a = brand_audio_title("English", 1)
    s = brand_subtitle_title("English", 1)
    assert a != s
    assert "『" in a and "』" in a and "〘" not in a
    assert "〘" in s and "〙" in s and "『" not in s


# ── video credit + container (unchanged) ────────────────────────────────────
def test_encoded_by_tag():
    assert ENCODED_BY_TAG == "Anime Weebs〔 @AniXWeebs 〕"


def test_container_title_unchanged():
    # The file/container title the user explicitly wants preserved.
    assert brand_container_title("Takopi's Original Sin") == \
        "Takopi's Original Sin〢@AniXWeebs"
    # Idempotent — never double-brands.
    once = brand_container_title("Takopi's Original Sin")
    assert brand_container_title(once) == once
