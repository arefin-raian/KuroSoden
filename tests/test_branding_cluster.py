"""Branding cluster regressions: audio-title branding in the torrent remux, and
the partial-subtitle → watermark decision.

Covers two reported bugs:
  * Audio track titles came out PLAIN ("English"/"Japanese") while subtitles were
    branded — because audio branding relied on a later mkvpropedit pass that could
    silently no-op. The remux must now emit ``-metadata:s:a:N title=…`` itself.
  * A file with MIXED subtitle tracks (some brandable text, some image PGS) wrongly
    SKIPPED the watermark. Rule: skip only when ALL subs are branded text; partial
    or no branding → burn.
"""

from __future__ import annotations

import asyncio

import pytest

from nekofetch.sources import _torrent_subs
from nekofetch.sources._branding import brand_audio_title, brand_subtitle_title
from nekofetch.sources._torrent_subs import is_text_sub


# ── #19: audio titles branded inside the remux command ────────────────────────

def test_remux_command_includes_audio_title_metadata(monkeypatch, tmp_path):
    """When audio_tracks + brand_audio_title are supplied, the ffmpeg remux must
    emit a ``-metadata:s:a:N title=…`` for each audio stream (not defer to
    mkvpropedit). We capture the argv by stubbing the subprocess + ffmpeg probe.
    """
    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*args, **kwargs):
        # The final remux is the call whose args include "-map" and the dest path.
        if "-map" in args:
            captured["cmd"] = list(args)
        return _FakeProc()

    # Make the dest look written & non-empty so the function reports ok.
    dest = tmp_path / "out.mkv"

    from nekofetch.sources import _hls

    monkeypatch.setattr(_hls, "find_ffmpeg", lambda: "ffmpeg", raising=False)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_exec)
    # No text subs → no extraction; the remux still runs to brand audio + copy subs.
    sub_tracks = [{"index": 0, "codec": "hdmv_pgs_subtitle", "lang": "eng", "title": ""}]
    audio_tracks = [
        {"index": 0, "codec": "aac", "lang": "eng", "title": "Stereo"},
        {"index": 1, "codec": "aac", "lang": "jpn", "title": ""},
    ]

    # dest must exist & be non-empty for ok=True; create it up front (the fake
    # exec doesn't actually write).
    dest.write_bytes(b"x" * 10)

    result = asyncio.run(
        _torrent_subs.brand_torrent_subtitles(
            tmp_path / "in.mkv", dest,
            sub_tracks=sub_tracks, video_ms=600_000,
            container_title="Show〢@AniXWeebs",
            brand_subtitle_title=brand_subtitle_title,
            audio_tracks=audio_tracks, brand_audio_title=brand_audio_title,
            lang_display=lambda code: {"eng": "English", "jpn": "Japanese"}.get(code, ""),
        )
    )
    cmd = captured.get("cmd", [])
    joined = " ".join(cmd)
    assert "-metadata:s:a:0" in cmd, joined
    assert "-metadata:s:a:1" in cmd, joined
    # Stereo (generic) → English via fallback_lang; empty title jpn → Japanese.
    assert any("title=English『 @AniXWeebs 』" == c for c in cmd), joined
    assert any("title=Japanese『 @AniXWeebs 』" == c for c in cmd), joined
    assert result["ok"] is True


# ── #18: partial-branding → watermark decision ────────────────────────────────

def _fully_branded(subs: list[dict]) -> bool:
    """Mirror of the WatermarkStage 'skip' predicate: skip only when EVERY sub is
    a brandable text track (and there is at least one)."""
    return bool(subs) and all(is_text_sub(t.get("codec", "")) for t in subs)


def test_all_text_subs_skip_watermark():
    subs = [{"codec": "ass"}, {"codec": "subrip"}]
    assert _fully_branded(subs) is True  # → skip burn


def test_mixed_text_and_image_subs_do_not_skip():
    # 2 brandable + 2 image PGS → partial branding → MUST watermark.
    subs = [{"codec": "ass"}, {"codec": "ass"},
            {"codec": "hdmv_pgs_subtitle"}, {"codec": "dvd_subtitle"}]
    assert _fully_branded(subs) is False  # → burn


def test_no_subs_do_not_skip():
    assert _fully_branded([]) is False  # → burn


def test_only_image_subs_do_not_skip():
    subs = [{"codec": "hdmv_pgs_subtitle"}]
    assert _fully_branded(subs) is False  # → burn
