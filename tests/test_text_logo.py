from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from kurosoden.shared.text_logo import (
    CATEGORIES,
    COLORS,
    FONTS,
    colors,
    font_weights,
    fonts_for_category,
    get_color,
    render_text_logo,
    sanitize_text,
)

FONT_DIR = Path("resources/fonts/text_logo")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FONT_DIR_ABS = _REPO_ROOT / "resources" / "fonts" / "text_logo"


def _longest_font_callback() -> int:
    """Bytes of the longest ``senku|wiz|textfont|…`` callback for any font."""
    from nekofetch.ui.components import cb

    code = "REQ-99999"  # a long-but-realistic request code
    worst = 0
    for category in CATEGORIES:
        for font in fonts_for_category(category.key):
            worst = max(worst, len(cb(
                "senku", "wiz", "textfont", code, "1", category.key, font.key,
            ).encode("utf-8")))
    return worst


def test_six_categories_with_ten_plus_bundled_fonts_each():
    assert len(CATEGORIES) == 6
    assert len(COLORS) >= 12
    assert {font.category for font in FONTS} == {category.key for category in CATEGORIES}
    for category in CATEGORIES:
        assert len(fonts_for_category(category.key)) >= 10, category.key
        # keys are globally unique (they live in one _FONT_BY_KEY namespace)
        keys = [f.key for f in fonts_for_category(category.key)]
        assert len(set(keys)) == len(keys)


def test_every_bundled_font_exists_and_ships_its_ofl_license():
    for font in FONTS:
        ttf = _FONT_DIR_ABS / font.filename
        assert ttf.is_file(), f"missing font file: {font.filename}"
        assert ttf.stat().st_size > 10000, f"suspiciously small font: {font.filename}"
        # the OFL-1.1 license sibling must exist for the family slug
        slug = font.name.lower().replace(" ", "-")
        assert (_FONT_DIR_ABS / f"OFL-1.1-{slug}.txt").is_file(), (
            f"missing OFL license for {font.name}"
        )


def test_every_font_loads_in_pil():
    from PIL import ImageFont

    for font in FONTS:
        ImageFont.truetype(str(_FONT_DIR_ABS / font.filename), 24)


def test_longest_textfont_callback_stays_under_64_bytes():
    assert _longest_font_callback() <= 64
    # color + upload callbacks are shorter by construction
    from nekofetch.ui.components import cb

    assert len(cb("senku", "wiz", "textcolor", "REQ-99999", "1", "white").encode()) <= 64
    assert len(cb("senku", "wiz", "textupfont", "REQ-99999", "1").encode()) <= 64


def test_sanitize_text_removes_controls_and_bounds_length():
    assert sanitize_text("  One\tTwo\n\nThree\x00  ") == "One Two\n\nThree"
    assert len(sanitize_text("x" * 500)) == 120


def test_variable_weights_and_static_fonts(tmp_path: Path):
    weights = font_weights(next(font for font in FONTS if font.key == "montserrat"))
    assert weights
    regular = render_text_logo("Hi", "montserrat", weight=400, output_dir=tmp_path)
    black = render_text_logo("Hi", "montserrat", weight=900, output_dir=tmp_path)
    assert regular != black
    static = render_text_logo("Hi", "bebas", weight=900, output_dir=tmp_path)
    assert static.is_file()

    from nekofetch.ui.components import cb
    assert len(cb("senku", "wiz", "textweight", "REQ-99999", "1",
                   "montserrat", "900", "0").encode()) <= 64
    assert len(cb("senku", "wiz", "textitalic", "REQ-99999", "1",
                   "montserrat", "900", "1").encode()) <= 64
    assert len(cb("senku", "wiz", "textprev_yes", "REQ-99999", "1").encode()) <= 64


def test_unsupported_italic_is_ignored(tmp_path: Path):
    upright = render_text_logo("Hi", "bebas", italic=False, output_dir=tmp_path)
    ignored = render_text_logo("Hi", "bebas", italic=True, output_dir=tmp_path)
    assert upright == ignored


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


def test_explicit_line_breaks_are_preserved():
    from PIL import Image, ImageDraw, ImageFont
    from kurosoden.shared.text_logo import _wrap_to_width

    probe = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(probe)
    font = ImageFont.truetype(str(_FONT_DIR_ABS / "BebasNeue-Regular.ttf"), 44)
    assert _wrap_to_width(draw, "One Piece", font).count("\\n") == 0
    assert _wrap_to_width(draw, "Line A\nLine B", font) == "Line A\nLine B"
    assert _wrap_to_width(draw, "A\n\nB", font) == "A\n\nB"


def test_render_preserves_single_line_and_explicit_breaks(tmp_path: Path):
    one = render_text_logo("One Piece", "bebas", output_dir=tmp_path)
    two = render_text_logo("Line A\nLine B", "bebas", output_dir=tmp_path)
    assert one != two
    with Image.open(one) as image_one, Image.open(two) as image_two:
        assert image_two.height > image_one.height


def test_render_long_unbroken_text_still_fits(tmp_path: Path):
    out = render_text_logo("x" * 400, "bebas", output_dir=tmp_path)
    with Image.open(out) as image:
        assert image.width <= 1620
        assert image.getchannel("A").getbbox() is not None


def test_render_has_mild_shadow(tmp_path: Path):
    out = render_text_logo("Shadow", "bebas", output_dir=tmp_path)
    with Image.open(out) as image:
        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        assert bbox is not None
        x0, y0, x1, y1 = bbox
        pixels = [image.getpixel((x, y)) for y in range(max(0, y0), min(image.height, y1 + 18))
                  for x in range(x0, x1) if 0 < image.getpixel((x, y))[3] < 220]
        assert pixels, "expected translucent shadow pixels around the glyphs"


def test_render_honors_color_rgb(tmp_path: Path):
    """Two colors of the same text+font → different paths AND different pixels."""
    white = render_text_logo("Vanitas", "bebas", color_rgb=(255, 255, 255),
                             output_dir=tmp_path)
    black = render_text_logo("Vanitas", "bebas", color_rgb=(0, 0, 0),
                             output_dir=tmp_path)
    assert white != black
    with Image.open(white) as w, Image.open(black) as b:
        assert w.tobytes() != b.tobytes()
        # Pick the most-opaque OVERLAPPING glyph pixel of each render — the
        # fills must differ. (Anti-aliased edge pixels can be < 255 alpha, so
        # we maximize min-alpha over the overlap instead of demanding 255.)
        wa, wb = w.getchannel("A"), b.getchannel("A")
        bbox = wb.getbbox()
        assert bbox is not None
        x0, y0, x1, y1 = bbox
        found = None
        best = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                aw, ab = wa.getpixel((x, y)), wb.getpixel((x, y))
                if aw >= 200 and ab >= 200:
                    overlap = min(aw, ab)
                    if overlap > best:
                        best = overlap
                        found = (w.getpixel((x, y)), b.getpixel((x, y)))
        assert found is not None, "no overlapping solid glyph pixels to compare"
        w_px, b_px = found
        assert w_px != b_px

        def _lum(px: tuple[int, int, int, int]) -> float:
            return 0.299 * px[0] + 0.587 * px[1] + 0.114 * px[2]

        assert _lum(w_px) > _lum(b_px) + 100, (
            f"white fill ({w_px}) must be much brighter than black ({b_px})"
        )


def test_render_with_custom_font_path(tmp_path: Path):
    """A one-shot uploaded font renders via ``font_path`` (no font_key needed)."""
    uploaded = _FONT_DIR_ABS / "BebasNeue-Regular.ttf"
    out = render_text_logo("Hi", None, color_rgb=(220, 38, 38),
                           font_path=uploaded, output_dir=tmp_path)
    assert out.is_file()
    with Image.open(out) as image:
        assert image.getchannel("A").getbbox() is not None
        # the red fill must actually appear on glyph pixels
        red_present = any(
            image.getpixel((x, y))[0] > 100
            for x, y in _sample_alpha(image)
        )
        assert red_present


def test_render_with_unknown_font_key_and_no_path_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        render_text_logo("Vanitas", "missing", output_dir=tmp_path)
    with pytest.raises(ValueError):
        render_text_logo("Vanitas", None, output_dir=tmp_path)


def test_render_text_logo_rejects_empty(tmp_path: Path):
    with pytest.raises(ValueError):
        render_text_logo("   ", "playfair", output_dir=tmp_path)


def _sample_alpha(image) -> list[tuple[int, int]]:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return []
    x0, y0, x1, y1 = bbox
    return [(x, y) for y in range(y0, y1, 3) for x in range(x0, x1, 3)
            if alpha.getpixel((x, y))]


def test_color_model_white_black_first_and_lookup():
    palette = colors()
    assert palette[0].key == "white" and palette[0].rgb == (255, 255, 255)
    assert palette[1].key == "black" and palette[1].rgb == (0, 0, 0)
    assert get_color("red") is not None and get_color("red").rgb == (220, 38, 38)
    assert get_color("nope") is None
    # every swatch carries an emoji and a real rgb triple
    for c in palette:
        assert c.emoji and len(c.rgb) == 3
        assert all(0 <= channel <= 255 for channel in c.rgb)
    # keys are unique
    assert len({c.key for c in palette}) == len(palette)
