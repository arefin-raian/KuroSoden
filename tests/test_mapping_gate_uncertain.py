"""The DDL mapping gate's uncertainty decision — when to auto-proceed vs require
the owner's confirmation (park).

Owner rule: a CONFIDENT, COMPLETE mapping flows straight through; anything with
gaps, unmatched files, low confidence, or an episode numbered only by last-resort
file order is UNCERTAIN → gated (parked) so nothing uploads without a confirm.
"""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.download_service import _mapping_uncertain
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry
from nekofetch.services.torrent_mapping import build_torrent_mapping


def _franchise(episodes=3):
    return FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=10, kind=ContentKind.SEASON, season_number=1,
                     season_part=None, title="", episodes=episodes, included=True),
    ])


def _files(nums, *, source="regex"):
    return [{"index": i, "name": f"Show S01E{n:02d}", "season": 1, "episode": n,
             "kind": "episode", "seq": i + 1, "episode_source": source,
             "resolutions": ["1080p"]} for i, n in enumerate(nums)]


def test_confident_complete_mapping_is_not_uncertain():
    of = _files([1, 2, 3])
    m = build_torrent_mapping(of, _franchise(3))
    assert _mapping_uncertain(m, of) is False


def test_gap_makes_it_uncertain():
    of = _files([1, 2, 3])            # franchise expects 4 → episode 4 missing
    m = build_torrent_mapping(of, _franchise(4))
    assert m.has_gaps is True
    assert _mapping_uncertain(m, of) is True


def test_unmatched_file_makes_it_uncertain():
    of = _files([1, 2, 3]) + [{
        "index": 99, "name": "Show Movie", "season": 0, "episode": None,
        "kind": "movie", "seq": 4, "resolutions": ["1080p"],
    }]  # a movie, but the franchise has no movie entry → unmatched
    m = build_torrent_mapping(of, _franchise(3))
    assert m.unmatched
    assert _mapping_uncertain(m, of) is True


def test_order_sourced_episode_makes_it_uncertain():
    # Numbers were only assignable by file order (positional detector declined) —
    # a soft "not sure" signal even when counts line up.
    of = _files([1, 2, 3], source="order")
    m = build_torrent_mapping(of, _franchise(3))
    assert _mapping_uncertain(m, of) is True
