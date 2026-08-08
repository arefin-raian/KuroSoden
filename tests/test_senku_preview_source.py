"""Senku thumbnail preview source selection.

The distribution channel already proved the public-URL route works. Senku's DM
preview must use the same route after render_entry has mirrored the card; local
file upload remains only as a total-host-failure fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bots.senku.handlers.wizard import _thumbnail_preview_source
from nekofetch.ui.components import cb, keyboard
from kurosoden.shared import senku_voice as V


async def _serialize_markup(markup):
    """Exercise the same Pyrogram serialization used by send_photo."""
    raw_markup = await markup.write(None)
    return raw_markup.write()


class TestSenkuThumbnailApprovalMarkup:
    @pytest.mark.asyncio
    async def test_approval_markup_serializes_as_bytes(self):
        markup = keyboard(
            [(V.BTN_THUMB_APPROVE, cb("senku", "wiz", "thumbok", "REQ-1078", "1")),
             (V.BTN_THUMB_REDO, cb("senku", "wiz", "thumbredo", "REQ-1078", "1"))],
        )

        encoded = await _serialize_markup(markup)

        assert isinstance(encoded, bytes)
        assert encoded


class TestThumbnailPreviewSource:
    def test_prefers_public_mirror(self, tmp_path: Path):
        local = tmp_path / "render.webp"
        local.write_bytes(b"not-read")

        source = _thumbnail_preview_source(
            local, "https://kappa.lol/abc123",
        )

        assert source == "https://kappa.lol/abc123"

    def test_uses_jpeg_fallback_when_no_public_mirror(self, tmp_path: Path):
        from PIL import Image

        local = tmp_path / "render.webp"
        Image.new("RGB", (20, 10), (20, 30, 40)).save(local, format="WEBP")

        source = _thumbnail_preview_source(local, "file:///tmp/render.webp")

        assert source == local.with_suffix(".jpg")
        assert source.exists()

    def test_uses_jpeg_fallback_when_mirror_is_missing(self, tmp_path: Path):
        from PIL import Image

        local = tmp_path / "render.webp"
        Image.new("RGB", (20, 10), (20, 30, 40)).save(local, format="WEBP")

        source = _thumbnail_preview_source(local, None)

        assert source == local.with_suffix(".jpg")
