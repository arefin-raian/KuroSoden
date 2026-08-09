from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nekofetch.bots.fsm import FSM
from kurosoden.shared.distribution_cache import DistributionCache
import kurosoden.bots.senku.handlers.wizard as wiz
from tests.test_senku_wizard_routing import (
    CODE, FlowContainer, FakeRedis, USER_ID, _dispatch_callback,
)
from kurosoden.bots.senku.app import build_senku


@pytest.mark.asyncio
async def test_previous_logo_prompt_yes_stores_latest_path(monkeypatch, tmp_path: Path):
    redis = FakeRedis()
    client = build_senku(FlowContainer(redis), token="1:AAAA")
    for _ in range(20):
        await asyncio.sleep(0)
    fsm = FSM(redis, bot="senku")
    latest = tmp_path / "latest.png"
    latest.write_bytes(b"png")
    cache = DistributionCache(FlowContainer(redis))
    await cache.set_last_text_logo(CODE, path=str(latest), text="Vanitas", font="playfair")
    await fsm.set(USER_ID, wiz.STATE_ASSET_PICKER, code=CODE, index=1, asset="logo")

    calls = []
    async def fake_store(self, code, index, path):
        calls.append((code, index, path))
        return SimpleNamespace(), None
    monkeypatch.setattr(wiz.SenkuThumbnailAdapter, "store_text_logo", fake_store)
    async def fake_next(*args, **kwargs):
        return None
    monkeypatch.setattr(wiz, "_thumb_next", fake_next, raising=False)
    # The router owns the nested helper; capture its outgoing card and invoke the
    # real callback sequence by first entering Text.
    sent = []
    async def fake_send_screen(_c, chat_id, screen, old_msg=None):
        sent.append(screen)
        return SimpleNamespace(id=9, chat=SimpleNamespace(id=chat_id))
    monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

    await _dispatch_callback(client, f"senku|wiz|text|{CODE}|1")
    state, data = await fsm.get(USER_ID)
    assert state == wiz.STATE_TEXT_REUSE
    assert Path(sent[-1].image) == latest

    await _dispatch_callback(client, f"senku|wiz|textprev_yes|{CODE}|1")
    assert calls == [(CODE, 1, str(latest))]
    state, _ = await fsm.get(USER_ID)
    assert state is None


@pytest.mark.asyncio
async def test_previous_logo_prompt_no_arms_new_text(monkeypatch, tmp_path: Path):
    redis = FakeRedis()
    client = build_senku(FlowContainer(redis), token="1:AAAA")
    for _ in range(20):
        await asyncio.sleep(0)
    fsm = FSM(redis, bot="senku")
    latest = tmp_path / "latest.png"
    latest.write_bytes(b"png")
    cache = DistributionCache(FlowContainer(redis))
    await cache.set_last_text_logo(CODE, path=str(latest), text="Vanitas")
    await fsm.set(USER_ID, wiz.STATE_ASSET_PICKER, code=CODE, index=1, asset="logo")
    sent = []
    async def fake_send_screen(_c, chat_id, screen, old_msg=None):
        sent.append(screen)
        return SimpleNamespace(id=9, chat=SimpleNamespace(id=chat_id))
    monkeypatch.setattr(wiz, "send_screen", fake_send_screen)

    await _dispatch_callback(client, f"senku|wiz|text|{CODE}|1")
    await _dispatch_callback(client, f"senku|wiz|textprev_no|{CODE}|1")
    state, data = await fsm.get(USER_ID)
    assert state == wiz.STATE_AWAIT_TEXT
    assert data.get("code") == CODE
