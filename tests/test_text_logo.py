from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from kurosoden.shared.text_logo import (
    CATEGORIES,
    FONTS,
    fonts_for_category,
    render_text_logo,
    sanitize_text,
)


def test_six_font_categories_and_bundled_fonts():
    assert len(CATEGORIES) == 6
    assert len(FONTS) == 6
    assert {font.category for font in FONTS} == {category.key for category in CATEGORIES}
    assert all((Path("resources/fonts/text_logo") / font.filename).is_file() for font in FONTS)
    assert all(fonts_for_category(category.key) for category in CATEGORIES)


def test_sanitize_text_removes_controls_and_bounds_length():
    assert sanitize_text("  One\tTwo\n\nThree\x00  ") == "One Two\nThree"
    assert len(sanitize_text("x" * 500)) == 120


def test_render_text_logo_is_transparent_and_deterministic(tmp_path: Path):
    first = render_text_logo("Vanitas", "playfair", output_dir=tmp_path)
    second = render_text_logo("Vanitas", "playfair", output_dir=tmp_path)

    assert first == second
    assert first.suffix == ".png"
    with Image.open(first) as image:
        assert image.mode == "RGBA"
        assert image.size[0] >= 720
        assert image.getpixel((0, 0))[3] == 0
        assert image.getchannel("A").getbbox() is not None


def test_render_text_logo_rejects_empty_or_unknown_font(tmp_path: Path):
    with pytest.raises(ValueError):
        render_text_logo("   ", "playfair", output_dir=tmp_path)
    with pytest.raises(ValueError):
        render_text_logo("Vanitas", "missing", output_dir=tmp_path)
