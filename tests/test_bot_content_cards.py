"""Tests for the card-text transforms in ``BotContentService``.

Focus on the duration/episode-count fix: a single-episode entry (movie, one-shot
OVA) must render the movie card with a real AniList runtime, while a
multi-episode entry renders the season card with an episode count. The old code
fed an episode *count* into the movie card's minutes slot (``1h {count}m``),
which this guards against.

These exercise only the pure ``_build_season_card`` transform against a fake
container carrying a default ``PostFormatConfig`` — no DB, Telegram, or AniList.
"""

from __future__ import annotations

from dataclasses import dataclass

from nekofetch.core.config import PostFormatConfig
from nekofetch.domain.enums import AudioType
from nekofetch.services.bot_content import BotContentService


@dataclass
class _Pack:
    """Minimal StoragePack stand-in (only the fields the card reads)."""
    resolution: str = "1080p"
    audio: AudioType = AudioType.SUBBED
    season: int | None = 1
    season_part: int | None = None
    episode_from: int | None = 1
    episode_to: int | None = 12
    file_count: int | None = 12


class _Cfg:
    def __init__(self, fmt: PostFormatConfig | None = None):
        self.post_format = fmt or PostFormatConfig()


class _Container:
    def __init__(self, fmt: PostFormatConfig | None = None):
        self.config = _Cfg(fmt)


def _svc(fmt: PostFormatConfig | None = None):
    return BotContentService(_Container(fmt))


def test_movie_card_uses_real_runtime_not_episode_count():
    svc = _svc()
    # A movie entry: single file, season None, AniList runtime 95 min.
    packs = [_Pack(season=None, episode_from=1, episode_to=1, file_count=1)]
    meta = {"title": "A Silent Voice", "duration_min": 95, "entry_episodes": 1}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    assert "1h 35m" in caption
    # The buggy "1h {episode}m" shape must be gone.
    assert "1h 1m" not in caption


def test_short_movie_runtime_under_an_hour():
    svc = _svc()
    packs = [_Pack(season=None, episode_from=1, episode_to=1, file_count=1)]
    meta = {"title": "Short OVA", "duration_min": 24, "entry_episodes": 1}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    assert "24m" in caption


def test_movie_card_missing_duration_shows_dash():
    svc = _svc()
    packs = [_Pack(season=None, episode_from=1, episode_to=1, file_count=1)]
    meta = {"title": "Unknown Runtime", "entry_episodes": 1}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    assert "—" in caption


def test_multi_episode_entry_renders_season_card():
    svc = _svc()
    # A normal 12-episode season must NOT be treated as a movie.
    packs = [_Pack(season=1, episode_from=1, episode_to=12, file_count=12)]
    meta = {"title": "Some Anime", "entry_episodes": 12, "duration_min": 24}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    # Season card carries the episode count, not a runtime.
    assert "12" in caption


def test_split_season_uses_local_count_not_global_episode_to():
    svc = _svc()
    # Part 2 is globally located at episodes 13–24 but contains 12 files.
    packs = [_Pack(season=1, episode_from=13, episode_to=24, file_count=12)]
    caption, _ = svc._build_season_card(
        {"title": "Vanitas Part 2", "entry_episodes": 12},
        season=1, season_part=2, packs=packs,
    )
    assert "EPISODES:</b> 12" in caption
    assert "EPISODES:</b> 24" not in caption


def test_split_season_watch_guide_uses_local_count():
    from types import SimpleNamespace

    part1 = SimpleNamespace(anilist_id=131646, format="TV", season_part=1, episodes=12)
    part2 = SimpleNamespace(anilist_id=135136, format="TV", season_part=2, episodes=12)
    packs = [
        _Pack(season=1, episode_from=1, episode_to=12, file_count=12),
        _Pack(season=1, episode_from=13, episode_to=24, file_count=12),
    ]
    fmt = PostFormatConfig(
        watch_guide_template="{seasons}",
        watch_guide_season_line="{season_label}={episodes}",
    )
    svc = _svc(fmt)
    svc._tv_entry_identities = lambda entries: {
        part1.anilist_id: (1, 1), part2.anilist_id: (1, 2),
    }
    guide = svc._build_franchise_watch_guide(
        {}, packs, {"tv": [part1, part2], "all": [part1, part2]},
    )
    assert "Season 01 Part 1=12" in guide
    assert "Season 01 Part 2=12" in guide
    assert "=24" not in guide


def test_split_season_range_fallback_counts_local_episodes():
    svc = _svc()
    packs = [_Pack(season=1, episode_from=13, episode_to=24, file_count=0)]
    caption, _ = svc._build_season_card(
        {"title": "Vanitas Part 2", "entry_episodes": 12},
        season=1, season_part=2, packs=packs,
    )
    assert "EPISODES:</b> 12" in caption
    assert "EPISODES:</b> 24" not in caption


def test_pack_only_split_guide_sums_parts_once():
    svc = _svc()
    packs = [
        _Pack(season=1, season_part=1, episode_from=1, episode_to=12, file_count=12),
        _Pack(season=1, season_part=1, episode_from=1, episode_to=12, file_count=12,
              resolution="720p"),
        _Pack(season=1, season_part=2, episode_from=13, episode_to=24, file_count=12),
        _Pack(season=1, season_part=2, episode_from=13, episode_to=24, file_count=12,
              resolution="720p"),
    ]
    fmt = PostFormatConfig(
        watch_guide_template="{seasons}",
        watch_guide_season_line="{season_label}={episodes}",
    )
    guide = _svc(fmt)._build_watch_guide_fallback({}, packs)
    assert "Season 01=24" in guide
    assert "Season 01=12" not in guide


def test_multi_episode_ova_is_not_a_movie():
    svc = _svc()
    # Multi-episode OVA (season None but >1 episode) → season/extras card.
    packs = [_Pack(season=None, episode_from=1, episode_to=4, file_count=4)]
    meta = {"title": "4-part OVA", "entry_episodes": 4, "duration_min": 30}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    assert "4" in caption
    assert "1h 4m" not in caption


# ── template overrides (Settings → Post Format) ───────────────────────────────

def test_season_template_override_wins_over_catalog():
    fmt = PostFormatConfig(season_card_template="OVERRIDE {title} :: {episodes} eps")
    svc = _svc(fmt)
    packs = [_Pack(season=1, episode_from=1, episode_to=12, file_count=12)]
    meta = {"title": "Overridden", "entry_episodes": 12}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    # The {title} slot now carries the styled header (<b>English</b>, with 〢Romaji
    # appended when it differs) — the template's job is the surrounding layout.
    assert caption == "OVERRIDE <b>Overridden</b> :: 12 eps"


def test_movie_template_override_receives_duration():
    fmt = PostFormatConfig(movie_card_template="{title} runs {duration}")
    svc = _svc(fmt)
    packs = [_Pack(season=None, episode_from=1, episode_to=1, file_count=1)]
    meta = {"title": "Film", "duration_min": 128, "entry_episodes": 1}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    assert caption == "<b>Film</b> runs 2h 8m"


def test_malformed_override_falls_back_to_catalog():
    # An unknown placeholder must not crash a publish — it falls back to en.json.
    fmt = PostFormatConfig(season_card_template="{nonexistent_field}")
    svc = _svc(fmt)
    packs = [_Pack(season=1, episode_from=1, episode_to=12, file_count=12)]
    meta = {"title": "Safe", "entry_episodes": 12}
    caption, _ = svc._build_season_card(meta, season=1, packs=packs)
    # Fell back to the shipped catalog card, which carries the title.
    assert "Safe" in caption
    assert "{nonexistent_field}" not in caption


def test_custom_duration_format_is_honoured():
    fmt = PostFormatConfig(
        movie_card_template="{duration}",
        duration_format_hm="{h}시간 {m}분",
        duration_format_m="{m}분",
    )
    svc = _svc(fmt)
    packs = [_Pack(season=None, episode_from=1, episode_to=1, file_count=1)]
    caption, _ = svc._build_season_card(
        {"title": "X", "duration_min": 95, "entry_episodes": 1}, season=1, packs=packs)
    assert caption == "1시간 35분"


# ── file-store bot rotation: one bot per ENTRY, strict round-robin ────────────

import pytest


class _FakeRedis:
    """Minimal async Redis stub: monotonic INCR + no-op EXPIRE (round-robin)."""

    def __init__(self):
        self._counters: dict[str, int] = {}

    async def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expire(self, key, ttl):
        return True


@dataclass
class _FstorePack:
    """StoragePack stand-in with the fields ``_generate_fstore_links`` reads."""
    resolution: str
    audio: AudioType = AudioType.SUBBED
    channel_id: int = -1001234567890
    header_message_id: int = 100
    end_message_id: int = 110
    start_message_id: int = 100
    file_message_ids: tuple[int, ...] = (101, 102, 103)


class _FstoreCfg:
    def __init__(self, bots, rotation="per_entry"):
        self.post_format = PostFormatConfig()
        self.bot = type("B", (), {"filestore_bots": bots,
                                  "fstore_rotation": rotation})()
        self.storage_channel = type("S", (), {"enabled": True})()


class _FstoreContainer:
    def __init__(self, bots, redis, rotation="per_entry"):
        self.config = _FstoreCfg(bots, rotation)
        self.redis = redis


def _bot_of(link: str) -> str:
    # https://t.me/<bot>?start=...
    return link.split("t.me/", 1)[1].split("?", 1)[0]


@pytest.mark.asyncio
async def test_fstore_one_bot_per_entry_and_rotates_across_entries():
    """Every quality of one entry shares ONE bot; consecutive entries cycle
    Killua → Makise → Ulquiorra → Killua… so each bot gets an even share."""
    bots = ["Killua", "Makise", "Ulquiorra"]
    svc = BotContentService(_FstoreContainer(bots, _FakeRedis()))

    def _three_packs():
        return [_FstorePack("480p"), _FstorePack("720p"), _FstorePack("1080p")]

    seen = []
    for _ in range(4):  # four entries
        links = await svc._generate_fstore_links(_three_packs())
        used = {_bot_of(v) for v in links.values()}
        # All three qualities of this entry are served by exactly one bot.
        assert len(used) == 1, f"entry spread across {used}"
        seen.append(used.pop())

    # Strict per-entry rotation, wrapping after the third bot.
    assert seen == ["Killua", "Makise", "Ulquiorra", "Killua"]


@pytest.mark.asyncio
async def test_fstore_per_pack_rotates_each_quality():
    """With rotation=per_pack, each quality pack advances the round-robin, so a
    single entry's three qualities land on three DIFFERENT bots in order."""
    bots = ["Killua", "Makise", "Ulquiorra"]
    svc = BotContentService(
        _FstoreContainer(bots, _FakeRedis(), rotation="per_pack"))
    packs = [_FstorePack("480p"), _FstorePack("720p"), _FstorePack("1080p")]
    links = await svc._generate_fstore_links(packs)
    used = [_bot_of(links[f"{p.resolution}_{p.audio.value}"]) for p in packs]
    assert used == ["Killua", "Makise", "Ulquiorra"]


@pytest.mark.asyncio
async def test_fstore_single_bot_config_still_works():
    """A single configured bot serves every entry (no rotation needed)."""
    svc = BotContentService(_FstoreContainer(["OnlyBot"], _FakeRedis()))
    links = await svc._generate_fstore_links([_FstorePack("480p"), _FstorePack("720p")])
    assert {_bot_of(v) for v in links.values()} == {"OnlyBot"}

