"""Senku thumbnail preview source selection.

The distribution channel already proved the public-URL route works. Senku's DM
preview must use the same route after render_entry has mirrored the card; local
file upload remains only as a total-host-failure fallback.
"""

from __future__ import annotations

from pathlib import Path

from bots.senku.handlers.wizard import _thumbnail_preview_source


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
