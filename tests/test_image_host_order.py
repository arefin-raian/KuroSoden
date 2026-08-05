"""Config-driven image-host mirror order (Phase 7, workstream F).

``backup_bytes`` mirrors an image onto every independent host so a restore
survives a single operator outage. The *order* hosts are tried is config-driven
(``bot.image_host_order``), but the result always records each host's URL in its
own field and ``BackupImage.primary`` picks catbox → telegraph → imgbb → source
regardless of the attempt order.

These pin the order resolver (unknown keys dropped, missing hosts appended) and
that every host is attempted, with the network stubbed so nothing is uploaded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kurosoden.shared.image_backup as ib
from kurosoden.shared.image_backup import _host_order, backup_bytes


def _container(order=None):
    return SimpleNamespace(
        env=SimpleNamespace(imgbb_api_key="k"),
        config=SimpleNamespace(
            bot=SimpleNamespace(image_host_order=order),
            thumbnail_channel=SimpleNamespace(telegraph_access_token=""),
        ),
    )


def test_host_order_respects_config():
    assert _host_order(_container(["imgbb", "catbox", "telegraph"])) == (
        "imgbb", "catbox", "telegraph",
    )


def test_host_order_drops_unknown_and_appends_missing():
    # "bogus" is dropped; only "imgbb" named, so catbox/telegraph are appended.
    assert _host_order(_container(["bogus", "imgbb"])) == (
        "imgbb", "catbox", "telegraph",
    )


@pytest.mark.asyncio
async def test_backup_bytes_empty_blob_is_noop(monkeypatch):
    called = False

    async def boom(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(ib, "_upload_catbox", boom)
    result = await backup_bytes(_container(), b"", mime="image/jpeg")
    assert result.primary is None
    assert called is False


# ── ImgBB: keep ONLY the full-resolution data.url, never thumb/medium ────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Captures the POST and returns a canned ImgBB response."""

    sent: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, params=None, data=None):
        _FakeAsyncClient.sent = {"url": url, "params": params, "data": data}
        # A realistic ImgBB body: full url + a downscaled thumb + medium.
        return _FakeResp({
            "success": True, "status": 200,
            "data": {
                "url": "https://i.ibb.co/full/post.jpg",
                "thumb": {"url": "https://i.ibb.co/THUMB/post.jpg"},
                "medium": {"url": "https://i.ibb.co/MED/post.jpg"},
                "display_url": "https://i.ibb.co/MED/post.jpg",
            },
        })


@pytest.mark.asyncio
async def test_imgbb_no_key_returns_none(monkeypatch):
    container = SimpleNamespace(env=SimpleNamespace(imgbb_api_key=""),
                                config=SimpleNamespace(bot=None, thumbnail_channel=None))
    # Must not even attempt a request without a key.
    def boom(*a, **k):
        raise AssertionError("must not call httpx without an API key")

    monkeypatch.setattr(ib.httpx, "AsyncClient", boom)
    assert await ib._upload_imgbb(container, b"data") is None
