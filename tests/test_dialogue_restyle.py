"""Dialogue normalization restyles ONLY plain dialogue — never signs/songs.

The owner wanted Sonny Boy's readable, bold, embedded dialogue look applied to
every release, but was (rightly) afraid it would leak onto signs, songs, OP/ED,
and karaoke and ruin the typesetting. The classifier keys off per-line evidence
(positioning/rotation/drawing/karaoke tags, non-bottom alignment, sign/song
style names) so a positioned or karaoke line is NEVER re-fonted, even when it
shares the ``Default`` style with real dialogue.

These tests pin that contract: plain dialogue flips to ``AXWDialog`` (scaled to
the script's PlayResY, injected exactly once, idempotent); everything else keeps
its original style; and ``brand_ass_text`` still injects the brand AND restyles.
"""

from __future__ import annotations

from nekofetch.sources._torrent_subs import (
    _DIALOG_BASE_FONTSIZE,
    _DIALOG_REF_RES_Y,
    _DIALOG_STYLE_NAME,
    _is_dialogue_line,
    brand_ass_text,
    restyle_dialogue,
)


def _dialogue(style: str, text: str, start="0:00:01.00", end="0:00:03.00") -> str:
    return f"Dialogue: 0,{start},{end},{style},,0,0,0,,{text}"


def _style_field(dialogue_line: str) -> str:
    # Dialogue: Layer,Start,End,Style,Name,ML,MR,MV,Effect,Text → field index 3
    return dialogue_line.split(",", 9)[3].strip()


def _script(play_res_y: int | None = 1080, events: tuple[str, ...] = ()) -> str:
    head = ["[Script Info]", "PlayResX: 1920"]
    if play_res_y is not None:
        head.append(f"PlayResY: {play_res_y}")
    head += [
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour",
        "Style: Default,Arial,60,&H00FFFFFF",
        "Style: Sign,Arial,60,&H00FFFFFF",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
    ]
    return "\n".join(head)


# ── classifier ──────────────────────────────────────────────────────────────

def test_plain_dialogue_is_dialogue():
    assert _is_dialogue_line("Hello there, general.", "Default") is True
    # Inline italics / colour are per-line intent, still plain dialogue.
    assert _is_dialogue_line(r"{\i1}whispered{\i0}", "Default") is True
    # Bottom alignments are dialogue.
    assert _is_dialogue_line(r"{\an2}centered bottom", "Default") is True


def test_positioned_and_drawn_lines_are_not_dialogue():
    assert _is_dialogue_line(r"{\pos(960,200)}A shop sign", "Default") is False
    assert _is_dialogue_line(r"{\move(0,0,100,100)}sliding", "Default") is False
    assert _is_dialogue_line(r"{\frz45}tilted", "Default") is False
    assert _is_dialogue_line(r"{\p1}m 0 0 l 10 10{\p0}", "Default") is False
    assert _is_dialogue_line(r"{\clip(0,0,10,10)}masked", "Default") is False


def test_karaoke_is_not_dialogue():
    assert _is_dialogue_line(r"{\k30}la{\k30}la", "Default") is False
    assert _is_dialogue_line(r"{\kf50}sung", "Default") is False


def test_non_bottom_alignment_is_not_dialogue():
    for an in (4, 5, 6, 7, 8, 9):
        assert _is_dialogue_line(rf"{{\an{an}}}up top", "Default") is False


def test_sign_song_style_names_are_not_dialogue():
    for name in ("Sign", "OP", "ED", "Song", "Title", "Romaji", "Kanji",
                 "Insert Song", "Caption", "Typeset"):
        assert _is_dialogue_line("plain text", name) is False


# ── restyle_dialogue ──────────────────────────────────────────────────────────

def test_only_plain_dialogue_flips_to_axwdialog():
    events = (
        _dialogue("Default", "Just some dialogue."),           # flip
        _dialogue("Default", r"{\pos(960,200)}A sign"),        # keep (positioned)
        _dialogue("Default", r"{\k40}ka{\k40}ra"),             # keep (karaoke)
        _dialogue("Sign", "Shop of Horrors"),                  # keep (style name)
        _dialogue("Default", r"{\i1}italic dialogue"),         # flip (inline ok)
    )
    new_text, count = restyle_dialogue(_script(1080, events))
    lines = [l for l in new_text.split("\n") if l.startswith("Dialogue:")]

    assert count == 2
    assert _style_field(lines[0]) == _DIALOG_STYLE_NAME     # plain
    assert _style_field(lines[1]) == "Default"              # \pos sign
    assert _style_field(lines[2]) == "Default"              # karaoke
    assert _style_field(lines[3]) == "Sign"                 # sign style
    assert _style_field(lines[4]) == _DIALOG_STYLE_NAME     # italic dialogue
    # Inline override survives the restyle.
    assert r"{\i1}italic dialogue" in lines[4]


def test_style_injected_exactly_once():
    events = (_dialogue("Default", "a"), _dialogue("Default", "b"))
    new_text, _ = restyle_dialogue(_script(1080, events))
    style_rows = [l for l in new_text.split("\n")
                  if l.strip().startswith(f"Style: {_DIALOG_STYLE_NAME},")]
    assert len(style_rows) == 1


def test_style_scaled_to_playresy():
    for res_y in (1080, 720, 480, 360, 288):
        new_text, _ = restyle_dialogue(_script(res_y, (_dialogue("Default", "x"),)))
        row = next(l for l in new_text.split("\n")
                   if l.strip().startswith(f"Style: {_DIALOG_STYLE_NAME},"))
        fs = int(row.split(":", 1)[1].split(",")[2])
        expected = max(8, int(_DIALOG_BASE_FONTSIZE * res_y / _DIALOG_REF_RES_Y))
        assert fs == expected, (res_y, fs, expected)


def test_idempotent():
    events = (_dialogue("Default", "once"),)
    first, c1 = restyle_dialogue(_script(1080, events))
    second, c2 = restyle_dialogue(first)
    assert c1 == 1
    assert c2 == 0
    assert second == first


def test_no_events_section_left_untouched():
    text = "[Script Info]\nPlayResY: 1080\n\n[V4+ Styles]\nStyle: Default,Arial,60\n"
    out, count = restyle_dialogue(text)
    assert out == text
    assert count == 0


# ── brand_ass_text integration ────────────────────────────────────────────────

def test_brand_ass_text_brands_and_restyles():
    events = (
        _dialogue("Default", "hello", start="0:00:01.00", end="0:00:02.00"),
        _dialogue("Default", r"{\pos(10,10)}sign", start="0:00:03.00", end="0:00:04.00"),
    )
    new_text, brand_count = brand_ass_text(
        _script(1080, events), video_ms=600_000, normalize_dialogue=True)

    assert brand_count > 0                       # brand cues injected
    assert "AXWBrand" in new_text                # brand style present
    assert _DIALOG_STYLE_NAME in new_text        # dialog style present
    dlines = [l for l in new_text.split("\n")
              if l.startswith("Dialogue:") and "AXWBrand" not in l]
    assert _style_field(dlines[0]) == _DIALOG_STYLE_NAME  # plain flipped
    assert _style_field(dlines[1]) == "Default"          # sign untouched


def test_brand_ass_text_normalize_off_leaves_dialogue_alone():
    events = (_dialogue("Default", "hello"),)
    new_text, _ = brand_ass_text(
        _script(1080, events), video_ms=600_000, normalize_dialogue=False)
    assert _DIALOG_STYLE_NAME not in new_text
    dlines = [l for l in new_text.split("\n")
              if l.startswith("Dialogue:") and "AXWBrand" not in l]
    assert _style_field(dlines[0]) == "Default"
