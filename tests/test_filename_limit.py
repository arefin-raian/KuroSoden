"""60-char filename limit with the pack-caption shortening fallback.

The owner's rule: a final output filename's STEM (extension excluded) must be
≤60 chars. The worst case is the 1080p variant (4-digit resolution token), so if
the 1080p stem fits, every derived tier fits. When it's over, fall back through
the SAME title ladder the pack caption uses — full title → shortest Latin
synonym → acronym — until it fits. These pin the shared helpers and the confirm
card's example (which must match what RenameStage produces).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nekofetch.services.bot_naming import (
    FILENAME_STEM_LIMIT,
    pick_title_within,
    title_candidates,
)


def test_limit_is_sixty():
    assert FILENAME_STEM_LIMIT == 60


def test_title_candidates_ladder_order():
    # full → shortest Latin synonym → acronym, de-duplicated.
    cands = title_candidates(
        "That Time I Got Reincarnated As A Slime",
        ["TenSura", "テンスラ", "That Time I Got Reincarnated as a Slime the Movie"],
    )
    assert cands[0] == "That Time I Got Reincarnated As A Slime"  # full first
    assert cands[1] == "TenSura"                                  # shortest Latin alt, natural case
    assert cands[-1] == "TTIGRAS"                                 # acronym last
    # Non-Latin synonym is never a candidate.
    assert all("テ" not in c for c in cands)


def test_pick_title_within_returns_first_that_fits():
    cands = ["A Very Long Anime Title Indeed", "SHORTONE", "SLT"]
    # render = title + a fixed 34-char suffix; limit 45 → only the shorter fit.
    suffix = " S01E01 [1080p] [Dual] @AniXWeebs"  # 32 chars
    best, rendered = pick_title_within(cands, lambda t: t + suffix, 45)
    assert best == "SHORTONE"
    assert len(rendered) <= 45


def test_pick_title_within_falls_through_to_shortest():
    # Nothing fits → return the LAST (shortest) candidate rather than crash.
    cands = ["Loooong", "Medium", "Shorter"]
    best, rendered = pick_title_within(cands, lambda t: t + "x" * 100, 10)
    assert best == "Shorter"


# ── integrated: confirm-card example applies the limit + fallback ───────────────

def _request(title, synonyms=None):
    return SimpleNamespace(
        anime_title=title,
        franchise_data={"synonyms": synonyms or []},
        source="ddl",
    )


def _container():
    # Default season template (mirrors config.yaml) + a group brand.
    return SimpleNamespace(
        config=SimpleNamespace(
            rename=SimpleNamespace(
                template="{title} S{season}{season_part}E{episode} [{resolution}] [{audio}] @AniXWeebs",
                movie_template="", special_template="",
            ),
        ),
    )


def test_example_filename_shortens_when_over_limit(monkeypatch):
    from nekofetch.services import naming_confirm
    from nekofetch.domain.enums import AudioType

    monkeypatch.setattr(
        "nekofetch.services.branding_service.BrandingService",
        lambda c: SimpleNamespace(group="@AniXWeebs"),
    )

    long_title = "The Eminence in Shadow: Master of Garden and the Seven Shadows"
    req = _request(long_title, synonyms=["Kagejitsu"])
    name = naming_confirm.build_example_filename(
        _container(), req, resolution="1080p", audio=AudioType.DUAL_AUDIO, season=1,
    )
    # Stem (no extension appended by build_example_filename) must fit 60.
    assert len(name) <= FILENAME_STEM_LIMIT
    # It fell back to the short synonym (natural case preserved) rather than the
    # 60+ full title.
    assert "Kagejitsu" in name


def test_example_filename_keeps_full_title_when_it_fits(monkeypatch):
    from nekofetch.services import naming_confirm
    from nekofetch.domain.enums import AudioType

    monkeypatch.setattr(
        "nekofetch.services.branding_service.BrandingService",
        lambda c: SimpleNamespace(group="@AniXWeebs"),
    )

    req = _request("Bocchi the Rock", synonyms=["BTR"])
    name = naming_confirm.build_example_filename(
        _container(), req, resolution="1080p", audio=AudioType.SUBBED, season=1,
    )
    assert len(name) <= FILENAME_STEM_LIMIT
    assert "Bocchi the Rock" in name  # short enough → full title kept
