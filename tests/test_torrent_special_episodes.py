"""Special / fractional / zero-based episode detection in ``_torrent``.

Regression coverage for the ".5 and 00" bug: fractional episodes (13.5, 17.5)
must be classified as specials/OVAs (not silently dropped), and an episode
numbered ``00`` must be disambiguated — a contiguous ``00..N`` run is zero-based
(00 = episode 1), while a stray ``00`` beside a complete ``1..N`` run is a
prologue special.
"""

from __future__ import annotations

from nekofetch.sources._torrent import order_episodes, parse_release_meta


def _files(names):
    return [{"name": n, "path": n, "length": 1, "index": i}
            for i, n in enumerate(names, start=1)]


# ── parse_release_meta ────────────────────────────────────────────────────────

def test_fractional_dash_number_is_special():
    m = parse_release_meta("[Grp] Show - 13.5 [1080p].mkv")
    assert m["kind"] == "special"
    assert m["episode"] == 13
    assert m["fractional"] is True


def test_fractional_sxxexx_is_special():
    m = parse_release_meta("[Grp] Show S01E17.5.mkv")
    assert m["kind"] == "special"
    assert m["episode"] == 17
    assert m["fractional"] is True


def test_integer_episode_is_not_fractional():
    m = parse_release_meta("[Grp] Show - 12 [1080p].mkv")
    assert m["kind"] == "episode"
    assert m["episode"] == 12
    assert m["fractional"] is False


def test_zero_parsed_as_episode_zero():
    m = parse_release_meta("[Grp] Show - 00 [1080p].mkv")
    assert m["episode"] == 0


# ── order_episodes: zero-based vs prologue ────────────────────────────────────

def test_zero_based_run_shifts_to_one_based():
    # 00,01,02 is a zero-based release → episodes 1,2,3.
    o = order_episodes(_files([
        "Show - 00 [1080p].mkv",
        "Show - 01 [1080p].mkv",
        "Show - 02 [1080p].mkv",
    ]))
    assert [e["episode"] for e in o] == [1, 2, 3]
    assert all(e["kind"] == "episode" for e in o)


def test_stray_zero_beside_complete_run_is_prologue_special():
    # 00 present but the numbers are NOT a contiguous 0..N run (no episode 1) →
    # 00 can't be zero-based episode 1, so it's a prologue special; the real
    # episodes keep their numbers. (A contiguous 00..N run is instead zero-based;
    # a 00 sitting on an otherwise-complete 1..N is only resolvable with the
    # franchise map's expected count, handled by the mapping layer.)
    o = order_episodes(_files([
        "Show - 00 [1080p].mkv",
        "Show - 02 [1080p].mkv",
        "Show - 03 [1080p].mkv",
    ]))
    mains = [e for e in o if e["kind"] == "episode"]
    specials = [e for e in o if e["kind"] == "special"]
    assert [e["episode"] for e in mains] == [2, 3]
    assert len(specials) == 1
    assert specials[0]["episode"] == 0


def test_fractional_special_ordered_after_main_episodes():
    o = order_episodes(_files([
        "Show - 12 [1080p].mkv",
        "Show - 13 [1080p].mkv",
        "Show - 13.5 [1080p].mkv",
    ]))
    # Main episodes first, then the .5 special.
    assert [e["kind"] for e in o] == ["episode", "episode", "special"]
    assert o[-1]["episode"] == 13
    assert o[-1]["fractional"] is True
