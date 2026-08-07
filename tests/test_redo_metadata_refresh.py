"""Task O — redo metadata refresh (HANDOFF §9.3).

Drives the REAL ``RedoService.refresh_metadata`` diff against seeded
``ThumbnailSource`` rows and asserts the 9.1 live-surface propagation fires
only when the title's facts actually changed:

* ORB case — a dual-audio version ships: stored source says ``Japanese`` /
  rating 75; the redo's fresh packs add an English track and the franchise
  yields rating 82 → the affected thumbnails re-render (SAME art) and the
  main-post + entry-card propagation fires, captions refresh.
* quality-only redo — identical facts: nothing re-renders, nothing pushes
  (only the relink that already ran), ``relinked_only`` stays True.
* Vanitas case — display title corrected: channel renames + service notice
  sweep even though the facts didn't change.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.models import (
    DistributionBot,
    StoragePack,
    ThumbnailSource,
)

_ART = {
    "logo_url": "https://cdn/logo.png",
    "poster_url": "https://cdn/poster.png",
    "bg_url": "https://cdn/bg.png",
}


def _container(sessionmaker, *, admin_client=None):
    cfg = SimpleNamespace(
        main_channel=SimpleNamespace(
            enabled=False, channel_id=0, caption_template="",
            index_button_text="", download_button_text="",
        ),
        thumbnail_channel=SimpleNamespace(
            enabled=False, channel_id=0, telegraph_access_token="",
            divider_sticker_id=None, cover_image=None,
            wipe_all_on_rebuild=False, wipe_max_history=50,
        ),
        post_format=SimpleNamespace(
            max_quality_buttons=3,
            divider_sticker_id=None,
            season_card_template="",
            movie_card_template="",
            premium_emoji={},
        ),
        bot=SimpleNamespace(filestore_bots=[], divider_sticker_id=None),
    )
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker, redis=None, progress=None,
        admin_client=admin_client, config=cfg,
    )


async def _mk_source(session, *, doc, anilist_id, fields):
    row = ThumbnailSource(anime_doc_id=doc, anilist_id=anilist_id, fields=fields)
    session.add(row)
    await session.commit()
    return row


async def _mk_pack(session, *, doc, season=1, audio="subbed"):
    pack = StoragePack(
        anime_doc_id=doc, anime_title="ORB", season=season,
        resolution="480p", audio=AudioType(audio), channel_id=-100123,
        start_message_id=10, end_message_id=20, file_count=6,
    )
    session.add(pack)
    await session.commit()
    return pack


def _franchise(doc, *, entry_aid, score, title):
    return {
        "anime_doc_id": doc,
        "title": title,
        "search": {"english": title, "romaji": ""},
        "franchise": {
            "1": {
                "anilist_id": entry_aid, "format": "TV", "episodes": 25,
                "score": score, "season": 1,
            },
        },
    }


# ── ORB case: metadata changed → re-render + propagate ──────────────────────


async def test_refresh_metadata_rerenders_and_pushes_on_change(
    session, sessionmaker, monkeypatch,
):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:1000"
    entry_aid = 1001
    old_fields = {
        "title": "ORB: On the Movements of the Earth",
        "entry_label": "Season 01",
        "language": "Japanese",
        "anilist_score": 75,
        **_ART,
    }
    await _mk_source(session, doc=doc, anilist_id=-1, fields=old_fields)
    await _mk_source(session, doc=doc, anilist_id=entry_aid, fields=old_fields)
    # Fresh packs after the redo: the original subbed copy + a NEW dual-audio.
    await _mk_pack(session, doc=doc, season=1, audio="subbed")
    await _mk_pack(session, doc=doc, season=1, audio="dual_audio")
    await session.commit()

    calls = {"rendered": 0, "persisted": 0, "main_push": 0, "entry_push": 0,
             "main_caption": 0, "entry_caption": 0}
    persist_langs: list[str] = []

    async def _fake_render(self, **kw):
        calls["rendered"] += 1
        return Path("/tmp/fake_thumb.webp")

    async def _fake_gather(self, container, title, anime_doc_id=None):
        return {"synopsis": "s", "meta_label": "2024", "studio": "MADHOUSE",
                "tmdb_rating": 8.2, "anilist_score": 82, "genres": ["Drama"],
                "native_title": "", "romaji_title": "", "country": "JP",
                "language": "English & Japanese"}

    async def _fake_persist(container, doc_id, aid, fields, *, image_path=None):
        calls["persisted"] += 1
        persist_langs.append(str(fields.get("language")))

    async def _main_push(self, anime_doc_id, image_path):
        calls["main_push"] += 1
        return True

    async def _entry_push(self, anime_doc_id, anilist_id, image_path):
        calls["entry_push"] += 1
        return True

    async def _main_cap(self, anime_doc_id):
        calls["main_caption"] += 1
        return True

    async def _entry_cap(self, anime_doc_id, anilist_id, caption):
        calls["entry_caption"] += 1
        return True

    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.ThumbnailRenderService.render_thumbnail",
        _fake_render)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.gather_thumbnail_fields", _fake_gather)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.persist_thumbnail_source", _fake_persist)
    monkeypatch.setattr(
        "nekofetch.services.main_channel_service.MainChannelService.refresh_thumbnail",
        _main_push)
    monkeypatch.setattr(
        "nekofetch.services.main_channel_service.MainChannelService.refresh_caption",
        _main_cap)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_channel_service.ThumbnailChannelService."
        "refresh_published_thumbnail", _entry_push)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_channel_service.ThumbnailChannelService."
        "refresh_published_caption", _entry_cap)

    result = await RedoService(_container(sessionmaker)).refresh_metadata(
        doc, _franchise(doc, entry_aid=entry_aid, score=8.2,
                        title="ORB: On the Movements of the Earth"))

    assert result.relinked_only is False
    assert result.main_changed is True
    assert entry_aid in result.changed_entries
    # Both stored rows re-rendered with the SAME art + fresh facts, persisted,
    # and pushed to their live surfaces.
    assert calls["rendered"] == 2
    assert calls["persisted"] == 2
    assert calls["main_push"] == 1
    assert calls["entry_push"] == 1
    assert calls["main_caption"] == 1
    assert calls["entry_caption"] == 1
    # The re-render persisted the refreshed source with the new language label
    # (same art — the merge kept the stored logo/poster/bg).
    assert set(persist_langs) == {"English & Japanese"}


# ── quality-only redo: identical facts → relink only ────────────────────────


async def test_refresh_metadata_quality_only_does_not_rerender(
    session, sessionmaker, monkeypatch,
):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:1001"
    entry_aid = 1002
    old_fields = {"title": "Vanitas no Carte", "entry_label": "Season 01",
                  "language": "Japanese", "anilist_score": 75, **_ART}
    await _mk_source(session, doc=doc, anilist_id=-1, fields=old_fields)
    await _mk_source(session, doc=doc, anilist_id=entry_aid, fields=old_fields)
    await _mk_pack(session, doc=doc, season=1, audio="subbed")
    await session.commit()

    calls = {"rendered": 0, "persisted": 0}

    async def _fake_render(self, **kw):
        calls["rendered"] += 1
        return Path("/tmp/x.webp")

    async def _fake_gather(self, container, title, anime_doc_id=None):
        return {"anilist_score": 75, "language": "Japanese"}

    async def _fake_persist(*a, **k):
        calls["persisted"] += 1

    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.ThumbnailRenderService.render_thumbnail",
        _fake_render)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.gather_thumbnail_fields", _fake_gather)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.persist_thumbnail_source", _fake_persist)

    result = await RedoService(_container(sessionmaker)).refresh_metadata(
        doc, _franchise(doc, entry_aid=entry_aid, score=7.5,
                        title="Vanitas no Carte"))

    assert result.relinked_only is True
    assert result.main_changed is False
    assert result.changed_entries == []
    assert calls["rendered"] == 0
    assert calls["persisted"] == 0


# ── Vanitas case: corrected display title renames the channel ───────────────


async def test_refresh_metadata_renames_channel_on_title_change(
    session, sessionmaker, monkeypatch,
):
    from kurosoden.shared.redo_service import RedoService

    doc = "anilist:1003"
    entry_aid = 1004
    old_fields = {"title": "Vanitas no Carte", "entry_label": "Season 01",
                  "language": "Japanese", "anilist_score": 75, **_ART}
    await _mk_source(session, doc=doc, anilist_id=-1, fields=old_fields)
    await _mk_source(session, doc=doc, anilist_id=entry_aid, fields=old_fields)
    await _mk_pack(session, doc=doc, season=1, audio="subbed")
    bot = DistributionBot(
        name="Case Study of Vanitas", username=None, anime_doc_id=doc,
        encrypted_token="x", enabled=True, is_channel=True, chat_id=-100888,
    )
    session.add(bot)
    await session.commit()

    renders = []
    sweeps = []

    async def _fake_render(self, **kw):
        renders.append(1)
        return Path("/tmp/x.webp")

    async def _fake_gather(self, container, title, anime_doc_id=None):
        return {"anilist_score": 75, "language": "Japanese"}

    async def _fake_persist(*a, **k):
        pass

    async def _fake_sweep(client, chat_id):
        sweeps.append(chat_id)
        return 0

    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.ThumbnailRenderService.render_thumbnail",
        _fake_render)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.gather_thumbnail_fields", _fake_gather)
    monkeypatch.setattr(
        "nekofetch.services.thumbnail_service.persist_thumbnail_source", _fake_persist)
    import kurosoden.shared.senku_publisher as _sp

    monkeypatch.setattr(_sp.SenkuPublisher, "_sweep_service_notices",
                        staticmethod(_fake_sweep))

    class _Client:
        def __init__(self):
            self.titles = []

        async def edit_chat_title(self, chat_id, title):
            self.titles.append((chat_id, title))

    client = _Client()
    result = await RedoService(_container(sessionmaker, admin_client=client)).refresh_metadata(
        doc, _franchise(doc, entry_aid=entry_aid, score=7.5,
                        title="Vanitas no Carte"))

    # Facts identical → no re-render; the corrected title renamed the channel
    # and the auto-posted service notice was swept.
    assert result.title_refreshed is True
    assert result.relinked_only is False
    assert result.main_changed is False
    assert renders == []
    assert client.titles == [(-100888, "Vanitas no Carte")]
    assert sweeps == [-100888]
    async with sessionmaker() as s:
        row = (await s.execute(select(DistributionBot).where(
            DistributionBot.anime_doc_id == doc))).scalars().first()
        assert row.name == "Vanitas no Carte"
