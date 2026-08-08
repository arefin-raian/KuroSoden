"""webp_to_jpeg — Telegram photo sends need JPEG, not the sticker-format WebP.

The renderer emits ``.webp`` cards; every ``send_photo`` / ``edit_message_media``
path converts through :func:`webp_to_jpeg` first so the operator's DM preview and
the channel reference card always land as a real photo. This file locks down the
conversion: output decodable, dimensions preserved, alpha flattened, and a safe
``None`` on garbage so callers fall back to the original path.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from nekofetch.services.thumbnail_service import webp_to_jpeg


def _webp_bytes(size=(1366, 641), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 60)).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_converts_webp_to_jpeg(tmp_path: Path, mode: str):
    src = tmp_path / "card.webp"
    src.write_bytes(_webp_bytes(mode=mode))

    dest = webp_to_jpeg(src)

    assert dest is not None
    assert dest == src.with_suffix(".jpg")
    assert dest.exists()
    with Image.open(dest) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"
        assert im.size == (1366, 641)


def test_accepts_str_path(tmp_path: Path):
    src = tmp_path / "card.webp"
    src.write_bytes(_webp_bytes())

    dest = webp_to_jpeg(str(src))

    assert dest is not None
    assert dest.name == "card.jpg"


def test_missing_file_returns_none(tmp_path: Path):
    assert webp_to_jpeg(tmp_path / "nope.webp") is None


def test_garbage_bytes_returns_none(tmp_path: Path):
    src = tmp_path / "garbage.webp"
    src.write_bytes(b"not-an-image")
    assert webp_to_jpeg(src) is None


def test_does_not_touch_source(tmp_path: Path):
    src = tmp_path / "card.webp"
    blob = _webp_bytes()
    src.write_bytes(blob)

    webp_to_jpeg(src)

    assert src.read_bytes() == blob


def test_already_jpeg_returns_source_untouched(tmp_path: Path):
    """A .jpg input must never be overwritten (dest would collide with src)."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (10, 20, 30)).save(buf, format="JPEG")
    blob = buf.getvalue()
    src = tmp_path / "card.jpg"
    src.write_bytes(blob)

    result = webp_to_jpeg(src)

    assert result == src
    assert src.read_bytes() == blob
