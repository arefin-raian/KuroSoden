"""Phase-3 subtitle work: content-based language detection + MoviesMod stripping.

Two owner requests, for BOTH torrent and DDL where noted:

* A subtitle track with no usable title and no tagged language should be named by
  the language DETECTED from its cue content ("Japanese"), instead of falling all
  the way through to the anonymous "〘 By @AniXWeebs 〙" placeholder. Applies to
  torrent and DDL (both go through ``brand_torrent_subtitles``).
* DDL ONLY: MoviesMod release-site cruft is stripped — a "Downloaded from
  MoviesMod.org" cue is removed from the subtitle body ENTIRELY, and an
  audio/subtitle track TITLE that is the site name falls back to the language.

The pure helpers are tested directly; the end-to-end title resolution is asserted
by capturing the ffmpeg remux argv (the extraction subprocess is faked to write
real ``.ass`` content so detection genuinely runs).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nekofetch.sources import _torrent_subs
from nekofetch.sources._branding import (
    brand_audio_title,
    brand_subtitle_title,
    is_meaningful_track_name,
    is_moviesmod_title,
)
from nekofetch.sources._torrent_subs import (
    detect_subtitle_language,
    strip_moviesmod_lines,
)


def _ass(dialogue_lines: list[str]) -> str:
    """A minimal-but-real ``.ass`` with the given Dialogue TEXT fields."""
    head = (
        "[Script Info]\n"
        "Title: Sample\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,3,0,2,10,10,10,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    body = []
    for i, text in enumerate(dialogue_lines):
        s = i * 5
        body.append(
            f"Dialogue: 0,0:00:{s:02d}.00,0:00:{s + 4:02d}.00,Default,,0,0,0,,{text}"
        )
    return head + "\n".join(body) + "\n"


# ── detect_subtitle_language: the language matrix ─────────────────────────────

def test_detect_japanese_from_kana():
    ass = _ass([
        "おい、どこへ行くんだ？",
        "俺も一緒に行くぞ。危険すぎるからな。",
        "みんな、ありがとう。これが最後の戦いだ。",
    ])
    assert detect_subtitle_language(ass) == "Japanese"


def test_detect_japanese_survives_override_tags_and_headers():
    # Real cues carry {\i1}…{\i0}, \N breaks, and the file has ASCII headers that
    # would drown the kana if we detected on the WHOLE file. Dialogue-only wins.
    ass = _ass([
        r"{\i1}おい{\i0}、どこへ行くんだ？",
        r"\Nお前はもう死んでいる。俺たちの戦いはこれからだ！",
    ])
    assert detect_subtitle_language(ass) == "Japanese"


def test_detect_hindi_devanagari():
    ass = _ass([
        "तुम कहाँ जा रहे हो?",
        "मैं तुम्हारे साथ आऊंगा। यह बहुत खतरनाक है।",
    ])
    assert detect_subtitle_language(ass) == "Hindi"


def test_detect_chinese_and_korean():
    zh = _ass(["你要去哪里？我和你一起去。这非常危险。"])
    ko = _ass(["너는 어디 가는 거야? 나도 같이 갈게. 이건 정말 위험해."])
    assert detect_subtitle_language(zh) == "Chinese"
    assert detect_subtitle_language(ko) == "Korean"


def test_detect_english_via_langdetect():
    ass = _ass([
        "Where are you going? I will come with you.",
        "This is very dangerous, you know that right?",
    ])
    assert detect_subtitle_language(ass) == "English"


def test_detect_empty_or_too_short_returns_blank():
    assert detect_subtitle_language("") == ""
    assert detect_subtitle_language(_ass(["- Yes.", "- No."])) == ""
    assert detect_subtitle_language("not even ass text") == ""


def test_detect_is_deterministic():
    # langdetect is seeded internally, so repeat calls agree (no flaky labels).
    ass = _ass(["This is a longer english sentence used for a stable verdict."])
    assert detect_subtitle_language(ass) == detect_subtitle_language(ass) == "English"


# ── strip_moviesmod_lines: DDL content stripping ──────────────────────────────

def test_strip_removes_only_the_credit_line():
    ass = _ass([
        "This is a real subtitle line.",
        "Downloaded from MoviesMod.org - Best anime site",
        "Another genuine dialogue line here.",
    ])
    out, removed = strip_moviesmod_lines(ass)
    assert removed == 1
    assert "MoviesMod" not in out
    assert "This is a real subtitle line." in out
    assert "Another genuine dialogue line here." in out
    # Only the one event was dropped — the other two survive.
    assert out.count("Dialogue:") == 2


def test_strip_matches_case_insensitively_and_comments():
    ass = _ass(["Real line."]).replace(
        "[Events]\n",
        "[Events]\n"
        "Comment: 0,0:00:00.00,0:00:04.00,Default,,0,0,0,,visit MOVIESMOD.ORG now\n",
        1,
    )
    out, removed = strip_moviesmod_lines(ass)
    assert removed == 1
    assert "MOVIESMOD" not in out.upper().replace("REAL LINE.", "")
    assert "Real line." in out


def test_strip_noop_when_clean():
    ass = _ass(["Just a normal line.", "And another one."])
    out, removed = strip_moviesmod_lines(ass)
    assert removed == 0
    assert out == ass.replace("\r\n", "\n")


# ── is_moviesmod_title: title junk detection ──────────────────────────────────

def test_is_moviesmod_title():
    assert is_moviesmod_title("MoviesMod.org") is True
    assert is_moviesmod_title("Hindi - MoviesMod") is True
    assert is_moviesmod_title("moviesmod") is True
    assert is_moviesmod_title("Signs & Songs") is False
    assert is_moviesmod_title("") is False
    # A MoviesMod title is still a non-empty string, so the generic meaningfulness
    # check alone would WRONGLY keep it — the site check is what rejects it.
    assert is_meaningful_track_name("Hindi - MoviesMod") is True


# ── VegaMovies: the SAME stripping generalized to a second release brand ───────

def test_is_release_brand_title_covers_vegamovies():
    # The MoviesMod matcher now covers the whole banned-brand set, so VegaMovies
    # (and its dotted domain variant) are rejected exactly like MoviesMod.
    assert is_moviesmod_title("VegaMovies") is True
    assert is_moviesmod_title("VegaMovies.co.ru") is True
    assert is_moviesmod_title("Tamil - VegaMovies") is True
    assert is_moviesmod_title("vegamovies") is True
    # Unrelated titles still pass through untouched.
    assert is_moviesmod_title("English") is False
    assert is_moviesmod_title("Signs & Songs") is False


def test_strip_removes_vegamovies_credit_line():
    ass = _ass([
        "A real subtitle line.",
        "Follow us on VegaMovies.co.ru for more!",
        "Another genuine dialogue line.",
    ])
    out, removed = strip_moviesmod_lines(ass)
    assert removed == 1
    assert "VegaMovies" not in out
    assert "A real subtitle line." in out
    assert "Another genuine dialogue line." in out
    assert out.count("Dialogue:") == 2


def test_strip_removes_both_brands_in_one_pass():
    ass = _ass([
        "Genuine line one.",
        "Downloaded from MoviesMod.org",
        "Visit VEGAMOVIES now",
        "Genuine line two.",
    ])
    out, removed = strip_moviesmod_lines(ass)
    assert removed == 2
    assert "MoviesMod" not in out and "VEGAMOVIES" not in out.upper().replace(
        "GENUINE LINE ONE.", "").replace("GENUINE LINE TWO.", "")
    assert out.count("Dialogue:") == 2


# ── End-to-end title resolution through the remux argv ────────────────────────

class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"", b"")


def _run_remux(monkeypatch, tmp_path, *, sub_tracks, audio_tracks,
               content_by_rel, strip_domain):
    """Drive ``brand_torrent_subtitles`` with a faked ffmpeg that writes real
    ``.ass`` content for the extraction step, and capture the final remux argv."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        # Subtitle extraction: ffmpeg … -map 0:s:N -c:s ass OUT.ass
        if "-c:s" in argv and argv[-1].endswith(".ass"):
            out = Path(argv[-1])
            mi = argv.index("-map")
            rel = int(argv[mi + 1].split(":")[-1])  # "0:s:N" → N
            out.write_text(content_by_rel.get(rel, _ass(["x"])), encoding="utf-8")
        # The remux: has -c:v and writes the .mkv dest (last arg).
        elif "-c:v" in argv:
            captured["cmd"] = argv
        return _FakeProc()

    from nekofetch.sources import _hls

    monkeypatch.setattr(_hls, "find_ffmpeg", lambda: "ffmpeg", raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    dest = tmp_path / "out.mkv"
    dest.write_bytes(b"x" * 10)  # non-empty ⇒ ok=True

    def lang_display(code):
        return {"eng": "English", "jpn": "Japanese", "hin": "Hindi"}.get(
            (code or "").lower(), "")

    result = asyncio.run(
        _torrent_subs.brand_torrent_subtitles(
            tmp_path / "in.mkv", dest,
            sub_tracks=sub_tracks, video_ms=600_000,
            container_title="Show〢@AniXWeebs",
            brand_subtitle_title=brand_subtitle_title,
            audio_tracks=audio_tracks, brand_audio_title=brand_audio_title,
            lang_display=lang_display, normalize_dialogue=False,
            strip_domain=strip_domain,
        )
    )
    return captured.get("cmd", []), result


def test_untagged_sub_named_by_detected_language(monkeypatch, tmp_path):
    # No title, no language tag → the Japanese CONTENT names it "Japanese", and
    # the ISO code is stamped so player menus show it too. (Torrent + DDL.)
    jp = _ass([
        "おい、どこへ行くんだ？",
        "俺も一緒に行くぞ。危険すぎるからな。ありがとう。",
    ])
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "", "title": ""}]
    cmd, result = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: jp}, strip_domain=False,
    )
    assert result["ok"] is True
    assert "title=Japanese〘 @AniXWeebs 〙" in cmd
    # language tag derived from detection (jpn), not left blank.
    assert "language=jpn" in cmd
    # It NEVER fell back to the anonymous placeholder.
    assert "title=〘 By @AniXWeebs 〙" not in cmd


def test_tagged_language_beats_detection(monkeypatch, tmp_path):
    # A real language tag wins over content detection (cheaper + authoritative).
    en = _ass(["Where are you going, I will come with you right now for sure."])
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "eng", "title": ""}]
    cmd, _ = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: en}, strip_domain=False,
    )
    assert "title=English〘 @AniXWeebs 〙" in cmd


def test_meaningful_title_beats_everything(monkeypatch, tmp_path):
    # A fansub's own descriptive title is preserved (torrent rule) over language.
    jp = _ass(["おい、どこへ行くんだ？ありがとう、みんな。"])
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "jpn", "title": "Signs & Songs"}]
    cmd, _ = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: jp}, strip_domain=False,
    )
    assert "title=Signs & Songs〘 @AniXWeebs 〙" in cmd


def test_ddl_moviesmod_sub_title_falls_back_to_language(monkeypatch, tmp_path):
    # DDL: a MoviesMod site title is unusable → falls to the tagged language.
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "eng", "title": "MoviesMod.org"}]
    cmd, _ = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: _ass(["Hello there my friend, how are you today?"])},
        strip_domain=True,
    )
    assert "title=English〘 @AniXWeebs 〙" in cmd
    assert not any("MoviesMod" in c for c in cmd)


def test_torrent_keeps_moviesmod_like_title(monkeypatch, tmp_path):
    # Gating proof: with strip_domain=False (torrent) a "moviesmod" title is NOT
    # stripped — torrents are trusted, and this proves DDL-only scoping.
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "eng", "title": "MoviesMod.org"}]
    cmd, _ = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: _ass(["Hello there."])}, strip_domain=False,
    )
    assert "title=MoviesMod.org〘 @AniXWeebs 〙" in cmd


def test_ddl_moviesmod_audio_title_falls_back_to_language(monkeypatch, tmp_path):
    # DDL: "Hindi - MoviesMod" audio title → Hindi (from the tag), site dropped.
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "hin", "title": ""}]
    audio_tracks = [{"index": 0, "codec": "aac", "lang": "hin", "title": "Hindi - MoviesMod"}]
    cmd, _ = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=audio_tracks,
        content_by_rel={0: _ass(["तुम कहाँ जा रहे हो? मैं आऊंगा।"])},
        strip_domain=True,
    )
    assert "title=Hindi『 @AniXWeebs 』" in cmd
    assert not any("MoviesMod" in c and ":a:" not in c for c in cmd)
    assert not any("MoviesMod" in c for c in cmd)


def test_ddl_strips_moviesmod_content_line(monkeypatch, tmp_path):
    # The MoviesMod credit cue is stripped from the body (reported as a count).
    dirty = _ass([
        "A genuine line of dialogue here.",
        "Downloaded from MoviesMod.org",
        "Another genuine line right here.",
    ])
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "eng", "title": ""}]
    _, result = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: dirty}, strip_domain=True,
    )
    assert result["stripped_lines"] == 1


def test_torrent_does_not_strip_content(monkeypatch, tmp_path):
    # Same dirty content, torrent source → nothing stripped (DDL-only rule).
    dirty = _ass(["Real line.", "Downloaded from MoviesMod.org", "Real two."])
    sub_tracks = [{"index": 0, "codec": "ass", "lang": "eng", "title": ""}]
    _, result = _run_remux(
        monkeypatch, tmp_path, sub_tracks=sub_tracks, audio_tracks=[],
        content_by_rel={0: dirty}, strip_domain=False,
    )
    assert result["stripped_lines"] == 0
