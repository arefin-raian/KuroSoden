"""Brand-style font size must scale to the ASS script resolution (PlayResY).

An ASS font size is expressed in the script's own PlayResY units, so a fixed
size-72 brand cue renders at wildly different on-screen fractions:

    * PlayResY 1080 → 72 / 1080 ≈ 6.7% of frame height  (correct — Sonny Boy)
    * PlayResY  360 → 72 /  360 ≈ 20%   of frame height  (huge — Sabikui Bisco)
    * PlayResY  288 → 72 /  288 ≈ 25%   of frame height  (giant — Orb OME)

These tests pin the fix: the injected AXWBrand style is scaled so the brand is
always ~the same fraction of screen height, whatever the script resolution.
"""

from __future__ import annotations

from nekofetch.sources._torrent_subs import (
    _BRAND_BASE_FONTSIZE,
    _BRAND_REF_RES_Y,
    _brand_style_line,
    brand_ass_text,
)


def _fontsize(style_line: str) -> int:
    # "Style: AXWBrand,Trebuchet MS,72,..." → field index 2 after "Style:"
    return int(style_line.split(":", 1)[1].split(",")[2])


def _script(play_res_y, styles=("Style: Default,Arial,26,&H0",)):
    head = ["[Script Info]", "PlayResX: 1280"]
    if play_res_y is not None:
        head.append(f"PlayResY: {play_res_y}")
    return head + ["", "[V4+ Styles]", *styles]


def test_brand_size_scales_with_playresy():
    # Each resolution yields the SAME fraction of screen height as the 1080 base.
    for res_y in (1080, 720, 480, 360, 288):
        line = _brand_style_line(_script(res_y))
        fs = _fontsize(line)
        expected = round(_BRAND_BASE_FONTSIZE * res_y / _BRAND_REF_RES_Y)
        assert fs == expected, (res_y, fs, expected)
        frac = fs / res_y
        base_frac = _BRAND_BASE_FONTSIZE / _BRAND_REF_RES_Y
        assert abs(frac - base_frac) < 0.01, (res_y, frac)


def test_reference_1080_is_unchanged():
    # The known-good case must keep its authored size exactly.
    assert _fontsize(_brand_style_line(_script(1080))) == _BRAND_BASE_FONTSIZE


def test_orb_288_no_longer_giant():
    # The Orb OME case: 288-tall script must NOT get a 72 (25%-of-frame) brand.
    fs = _fontsize(_brand_style_line(_script(288)))
    assert fs < 25  # ~19 — proportional, not the old giant 72
    assert fs / 288 < 0.08  # under 8% of frame height


def test_missing_playresy_falls_back_to_median_style_size():
    # No PlayResY → size tracks the file's own text (median of 26 & 40 = 33).
    line = _brand_style_line(_script(None, styles=[
        "Style: Default,Arial,26,&H0",
        "Style: Sign,Arial,40,&H0",
    ]))
    assert _fontsize(line) == 33


def test_outline_shadow_margins_scale_too():
    # A 288 script scales outline/shadow/margins down with the font, so the whole
    # cue stays proportional (not a thin font with a giant 1080-scale outline).
    line = _brand_style_line(_script(288))
    fields = line.split(":", 1)[1].split(",")
    # V4+ Styles: ...,Outline(16),Shadow(17),Alignment(18),MarginL(19),MarginR(20),MarginV(21)
    outline = float(fields[16])
    margin_v = int(fields[21])
    assert outline < 3.6  # base outline was authored for 1080
    assert margin_v < 60  # base MarginV was 60


def test_brand_ass_text_injects_scaled_style_for_low_res():
    ass = "\n".join([
        "[Script Info]", "PlayResX: 384", "PlayResY: 288", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize",
        "Style: Default,Arial,16",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,hi",
    ])
    out, n = brand_ass_text(ass, 600_000)
    assert n > 0
    brand = [l for l in out.split("\n") if l.startswith("Style: AXWBrand")][0]
    assert _fontsize(brand) < 25  # scaled, not the giant 72
