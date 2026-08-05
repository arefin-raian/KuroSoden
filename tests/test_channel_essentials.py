"""Unit coverage for ``channel_essentials`` — the manual channel pieces must
match what NekoFetch's auto-pipeline would produce (PLAN §2).

The whole point of this adapter is *no drift*: the title comes from NekoFetch's
``format_bot_name`` fed by ``BotFactory._gather``, the username from
``format_bot_username(is_channel=True)`` (the ``…_axw`` channel handle, no ``bot``
suffix), and the description from ``BotFactory._BRANDING_DESCRIPTION`` (or the
operator override). These tests pin each of those wires.
"""

from __future__ import annotations

import pytest

from kurosoden.shared.channel_essentials import build_channel_essentials


class _Bot:
    description_text = ""


class _Config:
    bot = _Bot()


class FakeContainer:
    def __init__(self, description_text=""):
        self.config = _Config()
        self.config.bot.description_text = description_text
        self.tmdb = None
        self.pg_sessionmaker = None


FRANCHISE = {
    "english": "Spy x Family",
    "romaji": "Spy x Family",
    "anime_doc_id": "anilist:140960",
}


@pytest.fixture
def _stub_gather(monkeypatch):
    """Stub ``BotFactory._gather`` so no DB is needed; return real pack-shaped meta."""
    async def fake_gather(self, anime_doc_id):
        return {
            "english": "Spy x Family",
            "romaji": "Spy x Family",
            "audios": {"dual_audio"},
            "languages": {"english", "japanese"},
            "qualities": ["720p", "1080p"],
        }

    from nekofetch.services.bot_factory import BotFactory
    monkeypatch.setattr(BotFactory, "_gather", fake_gather)


@pytest.mark.asyncio
async def test_username_is_channel_handle(_stub_gather):
    """Username must be the ``…_axw`` channel handle — never the ``…_bot`` one."""
    ess = await build_channel_essentials(
        FakeContainer(), anime_doc_id="anilist:140960", franchise=FRANCHISE)
    assert ess.username == "spy_x_family_axw"
    assert not ess.username.endswith("_bot")


@pytest.mark.asyncio
async def test_title_carries_audio_and_quality(_stub_gather):
    """Title is built by NekoFetch's formatter — it must reflect the real packs."""
    ess = await build_channel_essentials(
        FakeContainer(), anime_doc_id="anilist:140960", franchise=FRANCHISE)
    assert "Spy x Family" in ess.title
    assert "720p" in ess.title and "1080p" in ess.title


@pytest.mark.asyncio
async def test_operator_override_wins(_stub_gather):
    ess = await build_channel_essentials(
        FakeContainer(description_text="Custom bio"),
        anime_doc_id="anilist:140960", franchise=FRANCHISE)
    assert ess.description == "Custom bio"


@pytest.mark.asyncio
async def test_poster_url_is_tmdb_search():
    ess = await build_channel_essentials(
        FakeContainer(), anime_doc_id=None, franchise=FRANCHISE)
    assert ess.poster_search_url.startswith("https://www.themoviedb.org/search?query=")
    assert "Spy" in ess.poster_search_url


@pytest.mark.asyncio
async def test_survives_gather_failure(monkeypatch):
    """When packs can't be gathered, essentials still resolve from the franchise."""
    async def boom(self, anime_doc_id):
        raise RuntimeError("no DB")

    from nekofetch.services.bot_factory import BotFactory
    monkeypatch.setattr(BotFactory, "_gather", boom)

    ess = await build_channel_essentials(
        FakeContainer(), anime_doc_id="anilist:140960", franchise=FRANCHISE)
    assert "Spy x Family" in ess.title
    assert ess.username == "spy_x_family_axw"
