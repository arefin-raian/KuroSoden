"""Telegraph full-mapping renderer (torrent_screens.mapping_telegraph_nodes).

The confirm-card caption truncates the franchise→torrent mapping (the "unusable"
S01 24/12 ‧ S02P2 0/12 view). mapping_telegraph_nodes renders the COMPLETE
mapping as Telegraph DOM nodes — every entry, every numbered file, and the
missing episodes — so the admin can read and verify the whole structure. The
file numbers shown must match the 1-based ``<file#> S<season>`` edit grammar.
"""

from __future__ import annotations

from nekofetch.domain.enums import ContentKind
from nekofetch.services.franchise_flow import MappingEntry
from nekofetch.services.torrent_mapping import (
    FileAssignment,
    MissingEpisode,
    TorrentMapping,
    TorrentMappingEntry,
)
from nekofetch.ui.torrent_screens import mapping_telegraph_nodes


def _season_entry(season, part, title, episodes, files, *, included=True,
                  missing=None, conf=0.95):
    fe = MappingEntry(
        kind=ContentKind.SEASON, season_number=season, season_part=part,
        title=title, episodes=episodes, included=included,
    )
    return TorrentMappingEntry(
        franchise_entry=fe, files=files, confidence=conf,
        missing=missing or [],
    )


def _file(idx, ep, name):
    return FileAssignment(file_index=idx, filename=name, episode_number=ep)


def _flatten_text(nodes) -> str:
    """Collect all string leaves from a Telegraph node tree."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, dict):
            for c in n.get("children", []):
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(nodes)
    return "\n".join(out)


def test_nodes_list_every_entry_and_file_with_running_numbers():
    m = TorrentMapping(
        torrent_name="Vanitas [BD]",
        overall_confidence=0.9,
        entries=[
            _season_entry(1, 1, "The Case Study of Vanitas", 12,
                          [_file(1, 1, "Vanitas - 01.mkv"),
                           _file(2, 2, "Vanitas - 02.mkv")]),
            _season_entry(1, 2, "The Case Study of Vanitas Part 2", 12,
                          [_file(13, 1, "Vanitas P2 - 01.mkv")]),
        ],
    )
    nodes = mapping_telegraph_nodes(m, m.torrent_name)
    text = _flatten_text(nodes)

    # Torrent name heading + both entry headers present.
    assert "Vanitas [BD]" in text
    assert "Season 1 Part 1" in text
    assert "Season 1 Part 2" in text
    # File numbers run continuously across entries (1,2 then 3) — matching the
    # 1-based display index the <file#> S<n> edit grammar uses, NOT the raw
    # torrent file_index (which is 13 for the third file).
    assert "#1  E01  Vanitas - 01.mkv" in text
    assert "#2  E02  Vanitas - 02.mkv" in text
    assert "#3  E01  Vanitas P2 - 01.mkv" in text


def test_nodes_show_expected_counts_and_missing():
    m = TorrentMapping(
        torrent_name="Show",
        overall_confidence=1.0,
        entries=[
            _season_entry(1, None, "Show", 12,
                          [_file(1, 1, "Show - 01.mkv")],
                          missing=[MissingEpisode(1, 2, "The Second")]),
        ],
    )
    text = _flatten_text(mapping_telegraph_nodes(m))
    assert "1 files / 12 expected" in text
    assert "Missing episodes" in text
    assert "S01E02 — The Second" in text


def test_excluded_entry_marked_and_files_still_counted():
    m = TorrentMapping(
        torrent_name="Show",
        overall_confidence=1.0,
        entries=[
            _season_entry(1, None, "Main", 12, [_file(1, 1, "a.mkv")]),
            _season_entry(0, None, "Recap", 1, [_file(2, 1, "recap.mkv")],
                          included=False),
            _season_entry(2, None, "S2", 12, [_file(3, 1, "s2-01.mkv")]),
        ],
    )
    text = _flatten_text(mapping_telegraph_nodes(m))
    assert "Recap — excluded" in text
    # The excluded entry's file consumes a display number so the numbering after
    # it stays aligned with the torrent's real file order (#3, not #2).
    assert "#1  E01  a.mkv" in text
    assert "#3  E01  s2-01.mkv" in text


def test_returns_plain_node_dicts():
    m = TorrentMapping(torrent_name="X", overall_confidence=0.5, entries=[])
    nodes = mapping_telegraph_nodes(m, "X")
    assert isinstance(nodes, list)
    assert all(isinstance(n, (dict, str)) for n in nodes)
