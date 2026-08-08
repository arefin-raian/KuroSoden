"""Generate transparent text-logo PNGs for Senku's thumbnail wizard.

The font files in ``resources/fonts/text_logo`` are Google Fonts releases under
OFL-1.1.  This module deliberately stays independent of Telegram and Redis: it
only sanitizes the operator's text, loads one of the bundled fonts, and returns
a local transparent PNG.  The adapter owns mirroring and persistence.
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_REPO_ROOT = Path(__file__).resolve().parents[1]
_FONT_DIR = _REPO_ROOT / "resources" / "fonts" / "text_logo"
_OUTPUT_DIR = _REPO_ROOT / "data" / "text_logos"
_MAX_TEXT = 120
_MAX_LINES = 3
_MAX_WIDTH = 1500
_MIN_FONT_SIZE = 44
_START_FONT_SIZE = 220
_STROKE_WIDTH = 3


@dataclass(frozen=True, slots=True)
class TextLogoFont:
    key: str
    name: str
    category: str
    description: str
    filename: str


@dataclass(frozen=True, slots=True)
class TextLogoCategory:
    key: str
    name: str
    description: str


CATEGORIES: tuple[TextLogoCategory, ...] = (
    TextLogoCategory("elegant", "Elegant serif", "Polished, classic title lettering"),
    TextLogoCategory("modern", "Modern sans", "Clean, sharp and contemporary"),
    TextLogoCategory("script", "Cursive / script", "Smooth flowing handwritten lettering"),
    TextLogoCategory("bold", "Bold display", "Strong lettering that reads at a glance"),
    TextLogoCategory("retro", "Retro display", "Playful throwback title lettering"),
    TextLogoCategory("handwritten", "Handwritten", "Casual, personal marker-style lettering"),
)

FONTS: tuple[TextLogoFont, ...] = (
    TextLogoFont("playfair", "Playfair Display", "elegant", "High-contrast editorial serif", "PlayfairDisplay[wght].ttf"),
    TextLogoFont("montserrat", "Montserrat", "modern", "Geometric modern sans", "Montserrat[wght].ttf"),
    TextLogoFont("pacifico", "Pacifico", "script", "Friendly brush script", "Pacifico-Regular.ttf"),
    TextLogoFont("bebas", "Bebas Neue", "bold", "Tall condensed display", "BebasNeue-Regular.ttf"),
    TextLogoFont("bungee", "Bungee", "retro", "Rounded arcade display", "Bungee-Regular.ttf"),
    TextLogoFont("caveat", "Caveat", "handwritten", "Relaxed handwritten style", "Caveat[wght].ttf"),
)

_FONT_BY_KEY = {font.key: font for font in FONTS}
_CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}


def categories() -> tuple[TextLogoCategory, ...]:
    return CATEGORIES


def get_category(category_key: str) -> TextLogoCategory | None:
    return _CATEGORY_BY_KEY.get(category_key)


def fonts_for_category(category: str) -> tuple[TextLogoFont, ...]:
    if category not in _CATEGORY_BY_KEY:
        return ()
    return tuple(font for font in FONTS if font.category == category)


def get_font(font_key: str) -> TextLogoFont | None:
    return _FONT_BY_KEY.get(font_key)


def sanitize_text(value: str) -> str:
    """Keep useful text while bounding work and removing invisible controls."""
    value = str(value or "")
    value = "".join(ch for ch in value if ch in "\n\t" or not ord(ch) < 32)
    value = value.replace("\t", " ")
    lines = [re.sub(r"[ ]+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if line]
    cleaned = "\n".join(lines)
    return cleaned[:_MAX_TEXT].rstrip()


def _font_path(font: TextLogoFont) -> Path:
    path = _FONT_DIR / font.filename
    if not path.is_file():
        raise FileNotFoundError(f"bundled text-logo font is missing: {path}")
    return path


def _bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int, int, int]:
    return draw.multiline_textbbox(
        (0, 0), text, font=font, spacing=12, align="center", stroke_width=_STROKE_WIDTH,
    )


def _wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> str:
    """Wrap words and long unbroken titles until they fit the logo canvas."""
    out: list[str] = []
    for original in text.splitlines()[:_MAX_LINES]:
        words = original.split() or [original]
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            left, _top, right, _bottom = _bbox(draw, candidate, font)
            if current and right - left > _MAX_WIDTH:
                out.append(current)
                current = word
            elif not current and right - left > _MAX_WIDTH:
                # A URL/code-like word has no spaces; split it conservatively.
                pieces = textwrap.wrap(word, width=18, break_long_words=True,
                                       break_on_hyphens=False) or [word]
                out.extend(pieces[:-1])
                current = pieces[-1]
            else:
                current = candidate
        if current:
            out.append(current)
    return "\n".join(out[:_MAX_LINES])


def render_text_logo(text: str, font_key: str, *, output_dir: Path | None = None) -> Path:
    """Render ``text`` as a centered transparent PNG and return its path.

    The image has no opaque background. A restrained dark stroke keeps white logo
    lettering readable on light posters while the alpha outside the glyphs stays
    fully transparent.
    """
    clean = sanitize_text(text)
    font = get_font(font_key)
    if not clean:
        raise ValueError("text logo cannot be empty")
    if font is None:
        raise ValueError(f"unknown text-logo font: {font_key}")

    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe)
    chosen: ImageFont.FreeTypeFont | None = None
    wrapped = clean
    for size in range(_START_FONT_SIZE, _MIN_FONT_SIZE - 1, -4):
        candidate = ImageFont.truetype(_font_path(font), size=size)
        wrapped_candidate = _wrap_to_width(probe_draw, clean, candidate)
        left, top, right, bottom = _bbox(probe_draw, wrapped_candidate, candidate)
        if right - left <= _MAX_WIDTH and bottom - top <= 430:
            chosen = candidate
            wrapped = wrapped_candidate
            break
    if chosen is None:
        chosen = ImageFont.truetype(_font_path(font), size=_MIN_FONT_SIZE)
        wrapped = _wrap_to_width(probe_draw, clean, chosen)

    left, top, right, bottom = _bbox(probe_draw, wrapped, chosen)
    text_width = max(1, right - left)
    text_height = max(1, bottom - top)
    width = max(720, min(_MAX_WIDTH + 120, text_width + 120))
    height = max(260, min(600, text_height + 100))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (width // 2, height // 2), wrapped, font=chosen,
        fill=(255, 255, 255, 255),
        stroke_width=_STROKE_WIDTH,
        stroke_fill=(0, 0, 0, 180),
        spacing=12, align="center", anchor="mm",
    )

    target_dir = output_dir or _OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{font_key}\0{clean}".encode("utf-8")).hexdigest()[:20]
    path = target_dir / f"text_logo_{digest}.png"
    image.save(path, format="PNG", optimize=True)
    return path
