"""Configurable thumbnail styling (Senku settings → thumbnail_style).

The renderer is container-less, so a provider bridges the live
``ThumbnailStyleConfig`` (kept in DB-sync by SettingsService) into the token
substitution. These tests pin: the tokens are all plain numbers, the template
substitutes with none left over, overrides flow through, and the section is
wired into the settings surface.
"""

from __future__ import annotations

import re

import pytest

import nekofetch.services.thumbnail_service as ts
from nekofetch.core.config import AppConfig, ThumbnailStyleConfig


@pytest.fixture(autouse=True)
def _reset_provider():
    ts.set_thumbnail_style_provider(None)
    yield
    ts.set_thumbnail_style_provider(None)


def test_thumbnail_style_is_an_appconfig_section():
    cfg = AppConfig()
    assert isinstance(cfg.thumbnail_style, ThumbnailStyleConfig)
    # Defaults are the SOFTENED values (owner: original shadows too dark).
    assert cfg.thumbnail_style.shadow_opacity == 0.55
    assert cfg.thumbnail_style.overlay_darkness == 0.65


def test_style_tokens_are_all_plain_numbers():
    toks = ts._style_tokens()
    expected = {
        "{{STYLE_SHADOW_BLUR}}", "{{STYLE_SHADOW_OPACITY}}", "{{STYLE_SHADOW_OPACITY2}}",
        "{{STYLE_OVERLAY_FROM}}", "{{STYLE_OVERLAY_VIA}}", "{{STYLE_OVERLAY_TO}}",
        "{{STYLE_LOGO_ALPHA}}", "{{STYLE_POSTER_ALPHA}}", "{{STYLE_RING_ALPHA}}",
        "{{STYLE_SYNOPSIS_PX}}", "{{STYLE_LOGO_HEIGHT}}",
    }
    assert set(toks) == expected
    for k, v in toks.items():
        # Every value must be a bare number so it drops cleanly into CSS/Tailwind.
        assert re.fullmatch(r"\d+(\.\d+)?", v), f"{k}={v!r} is not a plain number"


def test_template_has_no_unsubstituted_style_tokens():
    html = ts._load_template()
    for token, value in ts._style_tokens().items():
        html = html.replace(token, value)
    leftover = re.findall(r"\{\{STYLE_[A-Z_0-9]+\}\}", html)
    assert leftover == [], f"unsubstituted style tokens: {leftover}"
    # Every STYLE token the template declares must be covered by the token map
    # (guards against a template token with no matching config field).
    raw = ts._load_template()
    declared = set(re.findall(r"\{\{STYLE_[A-Z_0-9]+\}\}", raw))
    assert declared == set(ts._style_tokens()), (
        f"template/token mismatch: template={declared} tokens={set(ts._style_tokens())}"
    )


def test_overlay_stops_derive_from_one_knob_and_preserve_ratio():
    ts.set_thumbnail_style_provider(lambda: ThumbnailStyleConfig(overlay_darkness=0.8))
    t = ts._style_tokens()
    # 0.80 → 80 / 30 / 10 (the template's original ratio 1 : 0.375 : 0.125).
    assert (t["{{STYLE_OVERLAY_FROM}}"], t["{{STYLE_OVERLAY_VIA}}"],
            t["{{STYLE_OVERLAY_TO}}"]) == ("80", "30", "10")


def test_provider_override_flows_into_tokens():
    ts.set_thumbnail_style_provider(
        lambda: ThumbnailStyleConfig(shadow_opacity=0.2, synopsis_px=20,
                                     logo_height_rem=5.0))
    t = ts._style_tokens()
    assert t["{{STYLE_SHADOW_OPACITY}}"] == "0.2"
    assert t["{{STYLE_SHADOW_OPACITY2}}"] == "0.14"      # 0.2 * 0.7
    assert t["{{STYLE_SYNOPSIS_PX}}"] == "20"
    assert t["{{STYLE_LOGO_HEIGHT}}"] == "5.0"


def test_provider_failure_falls_back_to_defaults():
    def _boom():
        raise RuntimeError("no container")
    ts.set_thumbnail_style_provider(_boom)
    t = ts._style_tokens()               # must not raise
    assert t["{{STYLE_SHADOW_OPACITY}}"] == "0.55"       # default


def test_settings_surface_wires_the_section():
    # Owner-only gating + the Senku panel lists the section.
    from nekofetch.core.settings_schema import OWNER_ONLY_SECTIONS
    from kurosoden.shared.settings_ui import SECTION_LABELS
    assert "thumbnail_style" in OWNER_ONLY_SECTIONS
    assert "thumbnail_style" in SECTION_LABELS


def test_number_fields_render_as_number_widget():
    from nekofetch.core.settings_schema import widget_for
    assert widget_for("thumbnail_style", "shadow_opacity", 0.55) == "number"
    assert widget_for("thumbnail_style", "synopsis_px", 16) == "number"
