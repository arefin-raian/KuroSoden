"""Unit coverage for the offline storage-channel rebuild parser (P3).

The rebuild tool recovers pack boundaries + resolution + audio from the
human-readable header caption alone, so a DB-loss scenario can still identify
every pack from Telegram. These tests pin the pure header parser — no network,
no Telegram, no DB.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rebuild_storage_index.py"


def _load():
    """Import the parser helpers without triggering the container bootstrap."""
    spec = importlib.util.spec_from_file_location("_rebuild_storage_index", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_header_recovers_resolution_audio_and_season():
    mod = _load()
    caption = "<b>➠ TAKOPI'S ORIGINAL SIN : SEASON 3</b>\n<b>➠ 480p [DUAL ∽ ENG + JPN]</b>"
    out = mod._parse_header(caption)
    assert out["resolution"] == "480p"
    assert out["audio_tag"] == "DUAL"
    assert out["season"] == 3
    assert "TAKOPI" in out["title_line"]


def test_parse_header_short_season_token():
    mod = _load()
    out = mod._parse_header("➠ SOME ANIME : S2\n➠ 1080p [SUB ∽ JPN + EngSubs]")
    assert out["resolution"] == "1080p"
    assert out["audio_tag"] == "SUB"
    assert out["season"] == 2


def test_parse_header_movie_has_no_season_and_bare_resolution():
    mod = _load()
    out = mod._parse_header("➠ SOME MOVIE : MOVIE\n➠ 720p")
    assert out["resolution"] == "720p"
    assert out["audio_tag"] is None
    assert out["season"] is None
