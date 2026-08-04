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


# ── pooled subtitle-gap windows (multi-track) ───────────────────────────────
def _ass(dialogues: list[tuple[int, int]]) -> str:
    """Build a minimal ASS whose [Events] holds the given (start_ms,end_ms) cues."""
    from nekofetch.sources._subs import _ms_to_ass

    lines = [
        "[Script Info]", "Title: x", "",
        "[V4+ Styles]", "Format: Name", "Style: Default,Arial", "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for s, e in dialogues:
        lines.append(f"Dialogue: 0,{_ms_to_ass(s)},{_ms_to_ass(e)},Default,,0,0,0,,x")
    return "\n".join(lines)


def test_pooled_windows_avoid_gap_occupied_in_another_track():
    """A middle gap that's silent in track A but occupied in track B must NOT be
    chosen once the tracks' cues are pooled — the whole point of the multi-sub fix.
    """
    from nekofetch.sources._subs import Cue, branding_windows
    from nekofetch.sources._torrent_subs import extract_ass_cues

    video_ms = 600_000  # 10-minute episode
    # Track A: dialogue only early; its own middle 250-350s window is wide-open.
    track_a = _ass([(5_000, 8_000)])
    # Track B: fills that exact middle window with continuous dialogue.
    track_b = _ass([(250_000, 350_000)])

    # Per-track (the OLD behaviour) A would happily brand inside 250-350s.
    a_only = branding_windows(extract_ass_cues(track_a), video_ms)
    assert any(250_000 <= s < 350_000 for s, _e in a_only)

    # Pooled (the FIX): B's dialogue blocks that slot, so no window lands in it.
    pooled = extract_ass_cues(track_a) + extract_ass_cues(track_b)
    shared = branding_windows(pooled, video_ms)
    assert shared, "still expect early/mid/late windows from the union"
    for s, e in shared:
        assert not (s < 350_000 and e > 250_000), \
            f"window {(s, e)} overlaps track B's 250-350s dialogue"


def test_pooled_windows_are_stamped_identically_into_every_track():
    """Both tracks receive the SAME shared windows, so branding stays in lockstep."""
    from nekofetch.sources._subs import branding_windows
    from nekofetch.sources._torrent_subs import brand_ass_text, extract_ass_cues

    video_ms = 600_000
    track_a = _ass([(5_000, 8_000), (250_000, 253_000)])
    track_b = _ass([(9_000, 12_000), (400_000, 403_000)])
    shared = branding_windows(
        extract_ass_cues(track_a) + extract_ass_cues(track_b), video_ms
    )

    out_a, na = brand_ass_text(track_a, video_ms, windows=shared)
    out_b, nb = brand_ass_text(track_b, video_ms, windows=shared)
    assert na == nb == len(shared)
    # The injected brand Dialogue timestamps match between the two tracks.
    def _brand_stamps(text: str) -> list[str]:
        return sorted(
            ln.split(",", 3)[1] + "-" + ln.split(",", 3)[2]
            for ln in text.splitlines()
            if ln.startswith("Dialogue:") and "AXWBrand" in ln
        )
    assert _brand_stamps(out_a) == _brand_stamps(out_b)

