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


# ── junk classification (trailers/teasers/promos are NOT episodes) ──────────────

def test_season_trailer_is_extra_not_episode():
    # The owner's exact bug: "Season 1 Trailer 1" was matched as S01E01 and
    # downloaded as the first episode. It must classify as EXTRA (junk) so it's
    # never assigned an episode slot — it lands in the unmatched "doesn't belong
    # here" bucket instead.
    m = parse_release_meta("The Elusive Samurai Season 1 Trailer 1.mkv")
    assert m["kind"] == "extra"


def test_teaser_promo_creditless_are_extra():
    for name in (
        "Show S01 Teaser.mkv",
        "Show - Promo 2.mkv",
        "Show NCED 01.mkv",
        "Show Creditless Opening.mkv",
        "Show - PV 3.mkv",
    ):
        assert parse_release_meta(name)["kind"] == "extra", name


def test_real_episode_still_parses_next_to_junk_tokens():
    # A legit episode whose title happens to contain no junk token stays an
    # episode — the junk filter must not over-match.
    m = parse_release_meta("[Grp] Show S01E05 [1080p].mkv")
    assert m["kind"] == "episode"
    assert m["episode"] == 5


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


# ── order_episodes: positional (incrementing-column) episode detection ─────────
# The owner's rule: the episode number is the numeric token that climbs +1,+1
# across the pack; the constant prefix and non-incrementing right-side codes are
# NOT episodes. analyze_pack finds that column; order_episodes must use it so
# unusual styles the per-file regex misses still number correctly. Same code path
# serves torrent AND DDL (both call order_episodes).

def _mains(names):
    return [e for e in order_episodes(_files(names)) if e["kind"] == "episode"]


def test_clean_sxxexx_pack_numbers_by_column():
    o = _mains([f"Clevatess.S01E{n:02d}.1080p.WEB-DL.mkv" for n in range(1, 7)])
    assert [e["episode"] for e in o] == [1, 2, 3, 4, 5, 6]
    assert all(e["season"] == 1 for e in o)


def test_leading_number_dot_title_style_now_detected():
    # "01. Title" — the per-file regex misses this entirely (no e/ep/episode/dash
    # anchor), so before wiring analyze_pack it fell through to no-number → gaps.
    # The incrementing column recovers 1..N.
    o = _mains(["01. The Beginning.mkv", "02. The Journey.mkv", "03. The End.mkv"])
    assert [e["episode"] for e in o] == [1, 2, 3]
    assert all(e.get("episode_source") == "column" for e in o)


def test_varying_right_side_does_not_derail_episode_column():
    # Right side (title / bracket codes) changes but does NOT increment +1; the
    # episode column is the one that does. Detection must pick the episode column.
    o = _mains([
        "Attack on Titan S01 - 01 [A1B2].mkv",
        "Attack on Titan S01 - 02 [Z9Q7].mkv",
        "Attack on Titan S01 - 03 [K3M4].mkv",
    ])
    assert [e["episode"] for e in o] == [1, 2, 3]
    assert all(e["season"] == 1 for e in o)


def test_space_separated_season_episode_styles():
    for names in (
        ["Show S01 01.mkv", "Show S01 02.mkv", "Show S01 03.mkv"],
        ["Show S01 EP01.mkv", "Show S01 EP02.mkv", "Show S01 EP03.mkv"],
    ):
        o = _mains(names)
        assert [e["episode"] for e in o] == [1, 2, 3], names
        assert all(e["season"] == 1 for e in o), names


def test_genuine_gap_is_preserved_not_renumbered():
    # 1,2,3,4,6,7 (EP5 missing) is unique but NOT contiguous → the column detector
    # declines (ambiguous), the per-file regex keeps the true numbers, and the gap
    # at 5 survives so the mapping layer can flag it missing.
    o = _mains([f"Show.S01E{n:02d}.mkv" for n in (1, 2, 3, 4, 6, 7)])
    assert [e["episode"] for e in o] == [1, 2, 3, 4, 6, 7]


def test_unparseable_names_fall_back_to_file_order():
    # No detectable number anywhere → last-resort file order numbers 1..N so the
    # pack still maps instead of collapsing to all-None.
    o = _mains(["aaa.mkv", "bbb.mkv", "ccc.mkv"])
    assert [e["episode"] for e in o] == [1, 2, 3]
    assert all(e.get("episode_source") == "order" for e in o)
