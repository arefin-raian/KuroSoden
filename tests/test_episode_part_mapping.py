from __future__ import annotations

from nekofetch.services.download_service import _map_episode_locator


def _split_data() -> dict:
    return {
        "entries": [
            {
                "kind": "season",
                "season_number": 1,
                "season_part": 1,
                "episodes": 12,
            },
            {
                "kind": "season",
                "season_number": 1,
                "season_part": 2,
                "episodes": 12,
            },
        ]
    }


def test_split_season_episodes_restart_at_one_per_part():
    data = _split_data()

    assert _map_episode_locator(1, 1, data) == (1, 1)
    assert _map_episode_locator(12, 1, data) == (1, 12)
    assert _map_episode_locator(13, 1, data) == (2, 1)
    assert _map_episode_locator(24, 1, data) == (2, 12)


def test_single_entry_season_keeps_source_episode_and_no_part():
    data = {
        "entries": [
            {
                "kind": "season",
                "season_number": 1,
                "season_part": None,
                "episodes": 12,
            }
        ]
    }

    assert _map_episode_locator(7, 1, data) == (None, 7)


def test_other_seasons_are_not_used_as_boundaries():
    data = {
        "entries": [
            {"kind": "season", "season_number": 1, "season_part": None, "episodes": 12},
            {"kind": "season", "season_number": 2, "season_part": None, "episodes": 12},
        ]
    }

    assert _map_episode_locator(12, 2, data) == (None, 12)


def test_explicit_part_normalizes_an_absolute_torrent_episode():
    data = _split_data()
    assert _map_episode_locator(13, 1, data, season_part=2) == (2, 1)
    assert _map_episode_locator(24, 1, data, season_part=2) == (2, 12)


def test_multiple_unlabelled_entries_do_not_invent_parts():
    data = {
        "entries": [
            {"kind": "season", "season_number": 1, "season_part": None, "episodes": 12},
            {"kind": "season", "season_number": 1, "season_part": None, "episodes": 12},
        ]
    }
    assert _map_episode_locator(13, 1, data) == (None, 13)
