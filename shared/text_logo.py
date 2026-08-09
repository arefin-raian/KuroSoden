"""Generate transparent text-logo PNGs for Senku's thumbnail wizard.

The font files in ``resources/fonts/text_logo`` are Google Fonts releases under
OFL-1.1 (each family ships its ``OFL-1.1-<slug>.txt`` sibling).  This module
deliberately stays independent of Telegram and Redis: it only sanitizes the
operator's text, loads one of the bundled fonts (or a one-shot uploaded font),
and returns a local transparent PNG.  The adapter owns mirroring and
persistence.

The color palette lives here too (``COLORS`` / :func:`colors`) so the renderer
and the wizard share one source of truth: white and black first, then the
primary spectrum, each with an honest emoji swatch.
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from math import ceil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_REPO_ROOT = Path(__file__).resolve().parents[1]
_FONT_DIR = _REPO_ROOT / "resources" / "fonts" / "text_logo"
_OUTPUT_DIR = _REPO_ROOT / "data" / "text_logos"
_UPLOAD_DIR = _OUTPUT_DIR / "uploaded"
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


@dataclass(frozen=True, slots=True)
class TextLogoColor:
    key: str            # short, stable, in callback data: "white","black","red",...
    name: str           # "White"
    emoji: str          # "⚪"
    rgb: tuple[int, int, int]   # (255,255,255)


CATEGORIES: tuple[TextLogoCategory, ...] = (
    TextLogoCategory("elegant", "Elegant serif", "Polished, classic title lettering"),
    TextLogoCategory("modern", "Modern sans", "Clean, sharp and contemporary"),
    TextLogoCategory("script", "Cursive / script", "Smooth flowing handwritten lettering"),
    TextLogoCategory("bold", "Bold display", "Strong lettering that reads at a glance"),
    TextLogoCategory("retro", "Retro display", "Playful throwback title lettering"),
    TextLogoCategory("handwritten", "Handwritten", "Casual, personal marker-style lettering"),
)

# 60 Google-Fonts families (OFL-1.1), 10 per category. Keys are short, stable,
# globally unique slugs used in callback data (so each stays ≤64 bytes in the
# ``senku|wiz|textfont|…`` payload).
FONTS: tuple[TextLogoFont, ...] = (
    # elegant (serif)
    TextLogoFont("playfair", "Playfair Display", "elegant", "High-contrast editorial serif", "PlayfairDisplay[wght].ttf"),
    TextLogoFont("cormorant", "Cormorant Garamond", "elegant", "Elegant high-contrast serif", "CormorantGaramond[wght].ttf"),
    TextLogoFont("ebgaramond", "EB Garamond", "elegant", "Classic Renaissance book serif", "EBGaramond[wght].ttf"),
    TextLogoFont("cinzel", "Cinzel", "elegant", "Carved Roman inscriptional serif", "Cinzel[wght].ttf"),
    TextLogoFont("librebask", "Libre Baskerville", "elegant", "Readable classic book serif", "LibreBaskerville[wght].ttf"),
    TextLogoFont("cardo", "Cardo", "elegant", "Humanist academic serif", "Cardo-Regular.ttf"),
    TextLogoFont("spectral", "Spectral", "elegant", "Editorial screen serif", "Spectral-Regular.ttf"),
    TextLogoFont("marcellus", "Marcellus", "elegant", "Refined neoclassical serif", "Marcellus-Regular.ttf"),
    TextLogoFont("prata", "Prata", "elegant", "Sleek high-contrast serif", "Prata-Regular.ttf"),
    TextLogoFont("dmserif", "DM Serif Display", "elegant", "Bold display serif", "DMSerifDisplay-Regular.ttf"),
    # modern (sans)
    TextLogoFont("montserrat", "Montserrat", "modern", "Geometric modern sans", "Montserrat[wght].ttf"),
    TextLogoFont("poppins", "Poppins", "modern", "Friendly geometric sans", "Poppins-Regular.ttf"),
    TextLogoFont("inter", "Inter", "modern", "Clean UI-focused sans", "Inter[opsz,wght].ttf"),
    TextLogoFont("worksans", "Work Sans", "modern", "Neutral grotesque sans", "WorkSans[wght].ttf"),
    TextLogoFont("raleway", "Raleway", "modern", "Elegant thin-to-black sans", "Raleway[wght].ttf"),
    TextLogoFont("oswald", "Oswald", "modern", "Condensed grotesque sans", "Oswald[wght].ttf"),
    TextLogoFont("archivo", "Archivo", "modern", "Versatile grotesque sans", "Archivo[wdth,wght].ttf"),
    TextLogoFont("manrope", "Manrope", "modern", "Modern rounded grotesque", "Manrope[wght].ttf"),
    TextLogoFont("barlow", "Barlow", "modern", "Calm technical sans", "Barlow-Regular.ttf"),
    TextLogoFont("rubik", "Rubik", "modern", "Rounded friendly sans", "Rubik[wght].ttf"),
    # script (cursive)
    TextLogoFont("pacifico", "Pacifico", "script", "Friendly brush script", "Pacifico-Regular.ttf"),
    TextLogoFont("dancing", "Dancing Script", "script", "Lively connected script", "DancingScript[wght].ttf"),
    TextLogoFont("greatvibes", "Great Vibes", "script", "Flourished elegant script", "GreatVibes-Regular.ttf"),
    TextLogoFont("alexbrush", "Alex Brush", "script", "Graceful brush script", "AlexBrush-Regular.ttf"),
    TextLogoFont("sacramento", "Sacramento", "script", "Light sweeping script", "Sacramento-Regular.ttf"),
    TextLogoFont("allura", "Allura", "script", "Smooth calligraphic script", "Allura-Regular.ttf"),
    TextLogoFont("parisienne", "Parisienne", "script", "Parisian fashion script", "Parisienne-Regular.ttf"),
    TextLogoFont("sueellen", "Sue Ellen Francisco", "script", "Casual marker handwriting", "SueEllenFrancisco-Regular.ttf"),
    TextLogoFont("kaushan", "Kaushan Script", "script", "Bouncy marker script", "KaushanScript-Regular.ttf"),
    TextLogoFont("cookie", "Cookie", "script", "Warm casual script", "Cookie-Regular.ttf"),
    # bold (display)
    TextLogoFont("bebas", "Bebas Neue", "bold", "Tall condensed display", "BebasNeue-Regular.ttf"),
    TextLogoFont("anton", "Anton", "bold", "Bold condensed sans", "Anton-Regular.ttf"),
    TextLogoFont("teko", "Teko", "bold", "Condensed strong sans", "Teko[wght].ttf"),
    TextLogoFont("fjalla", "Fjalla One", "bold", "Medium-condensed headlines", "FjallaOne-Regular.ttf"),
    TextLogoFont("alfaslab", "Alfa Slab One", "bold", "Heavy slab display", "AlfaSlabOne-Regular.ttf"),
    TextLogoFont("archivobl", "Archivo Black", "bold", "Extra-bold grotesque", "ArchivoBlack-Regular.ttf"),
    TextLogoFont("passion", "Passion One", "bold", "Chunky bold display", "PassionOne-Regular.ttf"),
    TextLogoFont("titanone", "Titan One", "bold", "Rounded heavy display", "TitanOne-Regular.ttf"),
    TextLogoFont("bowlby", "Bowlby One", "bold", "Oversized heavy display", "BowlbyOne-Regular.ttf"),
    TextLogoFont("staatlich", "Staatliches", "bold", "Single-case tall display", "Staatliches-Regular.ttf"),
    # retro (display)
    TextLogoFont("bungee", "Bungee", "retro", "Rounded arcade display", "Bungee-Regular.ttf"),
    TextLogoFont("monoton", "Monoton", "retro", "Wavy neon display", "Monoton-Regular.ttf"),
    TextLogoFont("bungeein", "Bungee Inline", "retro", "Rounded inline display", "BungeeInline-Regular.ttf"),
    TextLogoFont("lobster", "Lobster", "retro", "Vintage poster script", "Lobster-Regular.ttf"),
    TextLogoFont("righteous", "Righteous", "retro", "Retro rock-poster display", "Righteous-Regular.ttf"),
    TextLogoFont("fredoka", "Fredoka", "retro", "Rounded playful display", "Fredoka[wdth,wght].ttf"),
    TextLogoFont("bevan", "Bevan", "retro", "Sturdy slab display", "Bevan-Regular.ttf"),
    TextLogoFont("suezone", "Suez One", "retro", "Heavy newspaper slab", "SuezOne-Regular.ttf"),
    TextLogoFont("sniglet", "Sniglet", "retro", "Rounded cartoon display", "Sniglet-Regular.ttf"),
    TextLogoFont("shrikhand", "Shrikhand", "retro", "Bold Indian-style display", "Shrikhand-Regular.ttf"),
    # handwritten
    TextLogoFont("caveat", "Caveat", "handwritten", "Relaxed handwritten style", "Caveat[wght].ttf"),
    TextLogoFont("shadows", "Shadows Into Light", "handwritten", "Quick pen handwriting", "ShadowsIntoLight.ttf"),
    TextLogoFont("indie", "Indie Flower", "handwritten", "Carefree marker handwriting", "IndieFlower-Regular.ttf"),
    TextLogoFont("patrick", "Patrick Hand", "handwritten", "Neat pencil handwriting", "PatrickHand-Regular.ttf"),
    TextLogoFont("kalam", "Kalam", "handwritten", "Casual pen handwriting", "Kalam-Regular.ttf"),
    TextLogoFont("gloria", "Gloria Hallelujah", "handwritten", "Sketchy handwritten caps", "GloriaHallelujah.ttf"),
    TextLogoFont("architects", "Architects Daughter", "handwritten", "Blueprint handwriting", "ArchitectsDaughter-Regular.ttf"),
    TextLogoFont("caveatbr", "Caveat Brush", "handwritten", "Bold brush handwriting", "CaveatBrush-Regular.ttf"),
    TextLogoFont("nanumpen", "Nanum Pen Script", "handwritten", "Korean brush-pen script", "NanumPenScript-Regular.ttf"),
    TextLogoFont("reenie", "Reenie Beanie", "handwritten", "Tiny casual handwriting", "ReenieBeanie.ttf"),
)

# Emoji-swatch palette. White and black MUST stay first (owner's rule: main
# colors lead); the rest is the primary spectrum. The emoji visually matches the
# fill so an inline button reads as a color swatch.
COLORS: tuple[TextLogoColor, ...] = (
    TextLogoColor("white", "White", "⚪", (255, 255, 255)),
    TextLogoColor("black", "Black", "⚫", (0, 0, 0)),
    TextLogoColor("red", "Red", "🔴", (220, 38, 38)),
    TextLogoColor("blue", "Blue", "🔵", (37, 99, 235)),
    TextLogoColor("green", "Green", "🟢", (22, 163, 74)),
    TextLogoColor("yellow", "Yellow", "🟡", (234, 179, 8)),
    TextLogoColor("orange", "Orange", "🟠", (234, 88, 12)),
    TextLogoColor("purple", "Purple", "🟣", (147, 51, 234)),
    TextLogoColor("pink", "Pink", "🩷", (236, 72, 153)),
    TextLogoColor("brown", "Brown", "🟤", (120, 72, 40)),
    TextLogoColor("gray", "Gray", "🌫", (107, 114, 128)),
    TextLogoColor("cyan", "Cyan", "🟦", (6, 182, 212)),
)

_FONT_BY_KEY = {font.key: font for font in FONTS}
_CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
_COLOR_BY_KEY = {color.key: color for color in COLORS}


def categories() -> tuple[TextLogoCategory, ...]:
    return CATEGORIES


def get_category(category_key: str) -> TextLogoCategory | None:
    return _CATEGORY_BY_KEY.get(category_key)


def colors() -> tuple[TextLogoColor, ...]:
    return COLORS


def get_color(key: str) -> TextLogoColor | None:
    return _COLOR_BY_KEY.get(key)


def fonts_for_category(category: str) -> tuple[TextLogoFont, ...]:
    if category not in _CATEGORY_BY_KEY:
        return ()
    return tuple(font for font in FONTS if font.category == category)


def get_font(font_key: str) -> TextLogoFont | None:
    return _FONT_BY_KEY.get(font_key)


def uploaded_font_dir() -> Path:
    """The one-shot uploaded-font staging dir (never inside the bundled set)."""
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _UPLOAD_DIR


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


def _contrast_stroke(color_rgb: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """A stroke that contrasts the fill so every color stays legible.

    Dark fills get a light stroke, light fills a dark one — a black logo on a
    dark poster must not vanish the way a fixed dark stroke would.
    """
    lum = 0.299 * color_rgb[0] + 0.587 * color_rgb[1] + 0.114 * color_rgb[2]
    return (255, 255, 255, 180) if lum < 128 else (0, 0, 0, 180)


def render_text_logo(
    text: str,
    font_key: str | None = None,
    *,
    color_rgb: tuple[int, int, int] = (255, 255, 255),
    font_path: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Render ``text`` as a centered transparent PNG and return its path.

    The image has no opaque background. A contrast-aware stroke keeps the
    lettering readable on light posters while the alpha outside the glyphs stays
    fully transparent.

    ``font_key`` selects a bundled family; ``font_path`` overrides it with an
    arbitrary local font file (the one-shot uploaded-font path) and makes
    ``font_key`` optional. ``color_rgb`` is the letter fill — the digest includes
    it so two colors of the same text+font never collide on one PNG path.
    """
    clean = sanitize_text(text)
    if not clean:
        raise ValueError("text logo cannot be empty")
    if font_path is None:
        font = get_font(font_key)
        if font is None:
            raise ValueError(f"unknown text-logo font: {font_key}")
        source = _font_path(font)
        font_id = font_key or ""
    else:
        source = font_path
        # Content-hash identity: the same uploaded file is the same font, even
        # if two admins' temp paths differ.
        font_id = hashlib.sha256(source.read_bytes()).hexdigest()[:20]

    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe)
    chosen: ImageFont.FreeTypeFont | None = None
    wrapped = clean
    for size in range(_START_FONT_SIZE, _MIN_FONT_SIZE - 1, -4):
        candidate = ImageFont.truetype(source, size=size)
        wrapped_candidate = _wrap_to_width(probe_draw, clean, candidate)
        left, top, right, bottom = _bbox(probe_draw, wrapped_candidate, candidate)
        if right - left <= _MAX_WIDTH and bottom - top <= 430:
            chosen = candidate
            wrapped = wrapped_candidate
            break
    if chosen is None:
        chosen = ImageFont.truetype(source, size=_MIN_FONT_SIZE)
        wrapped = _wrap_to_width(probe_draw, clean, chosen)

    left, top, right, bottom = _bbox(probe_draw, wrapped, chosen)
    text_width = max(1, right - left)
    text_height = max(1, bottom - top)
    # Pillow may return float coordinates from ``multiline_textbbox`` (notably
    # with variable fonts). Image.new and the draw origin require integer sizes;
    # round upward so fractional glyph extents are never clipped.
    width = int(ceil(max(720, min(_MAX_WIDTH + 120, text_width + 120))))
    height = int(ceil(max(260, min(600, text_height + 100))))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (width // 2, height // 2), wrapped, font=chosen,
        fill=(*color_rgb, 255),
        stroke_width=_STROKE_WIDTH,
        stroke_fill=_contrast_stroke(color_rgb),
        spacing=12, align="center", anchor="mm",
    )

    target_dir = output_dir or _OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        f"{font_id}\0{color_rgb}\0{clean}".encode("utf-8"),
    ).hexdigest()[:20]
    path = target_dir / f"text_logo_{digest}.png"
    image.save(path, format="PNG", optimize=True)
    return path
