"""DDL franchise mapping (post-extract) + per-entry encode gap-fill.

The owner's scenario: a DDL request pastes several archives — every quality of N
seasons, plus a separate link that is a MOVIE in 1080p only. The system must:

* map the extracted files to the RIGHT franchise entries (S01, S02, …, Movie) —
  not collapse everything onto S01 by filename default,
* show, per entry, which qualities are PRESENT and which will be ENCODED to fill
  the gaps (the seasons ship all tiers → encode nothing; the 1080p-only movie →
  encode 720p + 480p),
* generalise to ANY entry kind (season / movie / OVA / special), each gap-filling
  independently.

These exercise the pure pieces that make that work: the shared tier-gapfill rule
(so the card and the encoder agree), the per-entry quality availability on the
mapping, and the card rendering. The interactive confirm + worker wiring are thin
Redis/Telegram glue over these.
"""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.tier_gapfill import (
    parse_fallbacks,
    tier_satisfied,
    tiers_to_encode,
)
from nekofetch.services.torrent_mapping import TorrentMapping, build_torrent_mapping
from nekofetch.ui.torrent_screens import format_torrent_mapping

_ENC = [720, 480]
_FB = {"480p": ["540p", "360p"]}


# ── shared gap-fill rule (card ⇄ encoder single source of truth) ──────────────

def test_tiers_to_encode_matrix():
    assert tiers_to_encode({1080}, _ENC, _FB) == [720, 480]      # 1080 only
    assert tiers_to_encode({1080, 720}, _ENC, _FB) == [480]      # need SD
    assert tiers_to_encode({1080, 720, 480}, _ENC, _FB) == []    # all present
    assert tiers_to_encode({1080, 540}, _ENC, _FB) == [720]      # 540 fills 480 slot
    assert tiers_to_encode({720}, _ENC, _FB) == [480]            # never up-encode 720
    assert tiers_to_encode({480}, _ENC, _FB) == []               # nothing below 480
    assert tiers_to_encode(set(), _ENC, _FB) == []               # unknown → nothing


def test_tier_satisfied_honours_substitutes():
    subs = parse_fallbacks(_FB)
    assert tier_satisfied({480}, 480, subs) is True
    assert tier_satisfied({540}, 480, subs) is True   # 540 fills 480
    assert tier_satisfied({360}, 480, subs) is True   # 360 fills 480
    assert tier_satisfied({720}, 480, subs) is False


# ── per-entry mapping: seasons all-tiers + a 1080p-only movie ─────────────────

def _franchise_s1_s2_movie() -> FranchiseMapping:
    return FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=10, kind=ContentKind.SEASON, season_number=1,
                     season_part=None, title="", episodes=2, included=True),
        MappingEntry(anilist_id=20, kind=ContentKind.SEASON, season_number=2,
                     season_part=None, title="", episodes=2, included=True),
        MappingEntry(anilist_id=30, kind=ContentKind.MOVIE, season_number=0,
                     season_part=None, title="The Movie", episodes=1, included=True),
    ])


def _ordered_files_multiseason_plus_movie() -> list[dict]:
    # S01 & S02: 2 eps each, all three tiers. Movie: 1080p only. The filenames
    # state their season, so ``season_explicit`` is True (as classify_file sets
    # it) — the resolver then trusts S02 instead of collapsing it onto S01.
    of: list[dict] = []
    idx = 0
    for season in (1, 2):
        for ep in (1, 2):
            of.append({
                "index": idx, "name": f"Show S{season:02d}E{ep:02d}",
                "season": season, "episode": ep, "kind": "episode",
                "season_explicit": True,
                "seq": idx + 1, "resolutions": ["1080p", "720p", "480p"],
            })
            idx += 1
    of.append({
        "index": idx, "name": "Show Movie 1080p", "season": 0, "episode": None,
        "kind": "movie", "seq": idx + 1, "resolutions": ["1080p"],
    })
    return of


def test_multiseason_plus_movie_maps_each_entry_with_qualities():
    m = build_torrent_mapping(
        _ordered_files_multiseason_plus_movie(), _franchise_s1_s2_movie())

    by_tag = {}
    for e in m.entries:
        fe = e.franchise_entry
        key = ("MOV" if fe.kind == ContentKind.MOVIE else f"S{fe.season_number:02d}")
        by_tag[key] = e

    # Four→three real entries, each with the right files.
    assert set(by_tag) == {"S01", "S02", "MOV"}
    assert by_tag["S01"].actual == 2
    assert by_tag["S02"].actual == 2
    assert by_tag["MOV"].actual == 1

    # Seasons ship every tier → encode NOTHING.
    assert by_tag["S01"].present_resolutions == ["1080p", "720p", "480p"]
    assert by_tag["S01"].tiers_to_encode(_ENC, _FB) == []
    assert by_tag["S02"].tiers_to_encode(_ENC, _FB) == []
    # The movie ships 1080p only → encode 720p + 480p (independent of the seasons).
    assert by_tag["MOV"].present_resolutions == ["1080p"]
    assert by_tag["MOV"].tiers_to_encode(_ENC, _FB) == [720, 480]


def test_mapping_survives_dict_roundtrip_with_resolutions():
    # The confirmed mapping is persisted to franchise_data['_torrent_mapping'] and
    # reloaded in the worker — resolutions (and thus the encode preview) must live
    # through the round-trip.
    m = build_torrent_mapping(
        _ordered_files_multiseason_plus_movie(), _franchise_s1_s2_movie())
    m2 = TorrentMapping.from_dict(m.to_dict())
    movie = next(e for e in m2.entries
                 if e.franchise_entry.kind == ContentKind.MOVIE)
    assert movie.present_resolutions == ["1080p"]
    assert movie.tiers_to_encode(_ENC, _FB) == [720, 480]


# ── card rendering ────────────────────────────────────────────────────────────

def test_ddl_card_shows_present_and_to_encode_per_entry():
    m = build_torrent_mapping(
        _ordered_files_multiseason_plus_movie(), _franchise_s1_s2_movie())
    card = format_torrent_mapping(m, encode_heights=_ENC, fallbacks_cfg=_FB)

    # The movie line lists its present tier and the encode plan; the seasons list
    # their tiers with no "encode" clause.
    assert "1080p, 720p, 480p" in card          # a season's full set
    assert "encode 720p, 480p" in card          # the movie's gap-fill plan
    # No season wrongly advertises an encode pass.
    assert card.count("encode ") == 1


def test_torrent_card_has_no_quality_line():
    # Torrent files carry no resolutions (quality unknown pre-download) and the
    # caller passes no encode config → the card shows no quality/encode line.
    of = [{"index": i, "name": f"S01E{i + 1:02d}", "season": 1, "episode": i + 1,
           "kind": "episode", "seq": i + 1} for i in range(2)]
    fr = FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=10, kind=ContentKind.SEASON, season_number=1,
                     season_part=None, title="", episodes=2, included=True),
        MappingEntry(anilist_id=20, kind=ContentKind.SEASON, season_number=2,
                     season_part=None, title="", episodes=2, included=True),
    ])
    card = format_torrent_mapping(build_torrent_mapping(of, fr))
    assert "encode" not in card
    assert "⌬" not in card


# ── entry independence: an OVA and a movie don't cross-contaminate ────────────

def test_two_unnumbered_extras_gap_fill_independently():
    # A movie (1080p only) AND an OVA (1080p+720p) — each is its own entry, so
    # their present tiers never union: the movie still needs 480 AND 720, the OVA
    # only 480. (Guards the "unnumbered entries collide" concern.)
    fr = FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=30, kind=ContentKind.MOVIE, season_number=0,
                     season_part=None, title="Movie", episodes=1, included=True),
        MappingEntry(anilist_id=40, kind=ContentKind.SPECIAL, season_number=0,
                     season_part=None, title="OVA", episodes=1, included=True),
    ])
    of = [
        {"index": 0, "name": "Movie 1080p", "season": 0, "episode": None,
         "kind": "movie", "seq": 1, "resolutions": ["1080p"]},
        {"index": 1, "name": "OVA 1080p 720p", "season": 0, "episode": None,
         "kind": "special", "seq": 2, "resolutions": ["1080p", "720p"]},
    ]
    m = build_torrent_mapping(of, fr)
    movie = next(e for e in m.entries if e.franchise_entry.kind == ContentKind.MOVIE)
    ova = next(e for e in m.entries if e.franchise_entry.kind == ContentKind.SPECIAL)
    assert movie.tiers_to_encode(_ENC, _FB) == [720, 480]
    assert ova.tiers_to_encode(_ENC, _FB) == [480]
