"""Multi-track subtitle branding must pick gaps silent across ALL tracks.

When a release ships more than one subtitle file, a subtitle-free gap in one
track can sit on top of dialogue in another. The branding windows are therefore
chosen from the POOLED cues of every track, so each of the three on-screen
branding slots (early / middle / near-end-but-not-outro) is silent in every
track and the same times are stamped into each file.

These cover the shared ``_subs`` helpers used by all three source paths:
streaming mux (:mod:`_mux`), manual normalize (:mod:`_normalize`), and — via its
own ASS pooling — torrents (:mod:`_torrent_subs`).
"""

from __future__ import annotations

from pathlib import Path

from nekofetch.sources._subs import (
    Cue,
    branding_windows,
    pooled_branding_windows,
    process_subtitle,
    shared_windows_for_vtts,
)

VIDEO_MS = 600_000  # 10-minute episode


def _vtt(path: Path, cues: list[tuple[int, int]]) -> Path:
    def ts(ms: int) -> str:
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    body = "WEBVTT\n\n" + "\n".join(
        f"{ts(s)} --> {ts(e)}\nline\n" for s, e in cues
    )
    path.write_text(body, encoding="utf-8")
    return path


def test_pooled_windows_reject_gap_occupied_in_another_track():
    """A middle slot open in track A but full of dialogue in track B is dropped."""
    # Track A: only early dialogue → its own middle is wide open.
    a = [Cue(5_000, 8_000, "")]
    # Track B: continuous dialogue right through the middle zone.
    b = [Cue(240_000, 360_000, "")]

    # Per-track (the OLD behaviour) A would brand inside the 240-360s hole.
    a_only = branding_windows(a, VIDEO_MS)
    assert any(240_000 <= s < 360_000 for s, _e in a_only)

    # Pooled: B blocks that slot, so no chosen window overlaps 240-360s.
    pooled = pooled_branding_windows([a, b], VIDEO_MS)
    assert pooled
    for s, e in pooled:
        assert not (s < 360_000 and e > 240_000), f"{(s, e)} overlaps track B"


def test_shared_windows_for_vtts_matches_pooled(tmp_path):
    """The file-path convenience returns the same windows as pooling cue lists."""
    a = _vtt(tmp_path / "a.vtt", [(5_000, 8_000)])
    b = _vtt(tmp_path / "b.vtt", [(240_000, 360_000)])
    from_files = shared_windows_for_vtts([a, b], VIDEO_MS)
    from_cues = pooled_branding_windows(
        [[Cue(5_000, 8_000, "")], [Cue(240_000, 360_000, "")]], VIDEO_MS
    )
    assert from_files == from_cues
    assert from_files, "expect early/mid/late windows"


def test_process_subtitle_stamps_shared_windows_into_every_file(tmp_path):
    """Both tracks branded with the same shared windows get identical brand times."""
    a = _vtt(tmp_path / "a.vtt", [(5_000, 8_000), (250_000, 253_000)])
    b = _vtt(tmp_path / "b.vtt", [(9_000, 12_000), (400_000, 403_000)])
    shared = shared_windows_for_vtts([a, b], VIDEO_MS)

    ma = process_subtitle(a, VIDEO_MS, windows=shared)
    mb = process_subtitle(b, VIDEO_MS, windows=shared)
    assert ma["brand_count"] == mb["brand_count"] == len(shared)

    def _stamps(ass_path: str) -> list[str]:
        text = Path(ass_path).read_text(encoding="utf-8")
        return sorted(
            ln.split(",", 3)[1] + "-" + ln.split(",", 3)[2]
            for ln in text.splitlines()
            if ln.startswith("Dialogue:") and ",Brand," in ln
        )

    assert _stamps(ma["ass"]) == _stamps(mb["ass"])


def test_process_subtitle_without_windows_is_per_track(tmp_path):
    """Back-compat: omitting windows derives them from the track's own cues."""
    a = _vtt(tmp_path / "solo.vtt", [(5_000, 8_000)])
    meta = process_subtitle(a, VIDEO_MS)
    assert meta["brand_count"] >= 1


def test_shared_windows_skip_unreadable_files(tmp_path):
    """A missing/broken track never sinks the pool — the rest still get windows."""
    good = _vtt(tmp_path / "good.vtt", [(5_000, 8_000)])
    missing = tmp_path / "nope.vtt"  # never created
    win = shared_windows_for_vtts([good, missing], VIDEO_MS)
    assert win
