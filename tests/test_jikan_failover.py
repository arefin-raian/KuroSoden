"""Public→local Jikan failover in MyAnimeListClient._get.

The public api.jikan.moe 502/504s per-resource. When JIKAN_FALLBACK_URL points
at a self-hosted jikan-rest, a primary base that exhausts its retries on a 5xx
must fail over to the fallback base and use its response. These drive the real
``_get`` with a fake transport that answers differently per base URL — no network.
"""

from __future__ import annotations

import pytest

from nekofetch.sources.telegram.myanimelist import MyAnimeListClient

PRIMARY = "https://api.jikan.moe/v4"
LOCAL = "http://localhost:8080/v4"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.headers = {}
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeSession:
    """Answers by base URL prefix: primary → 504, local → 200 (or per the map)."""

    def __init__(self, by_base: dict):
        self._by_base = by_base
        self.calls: list[str] = []

    async def get(self, url, params=None):
        self.calls.append(url)
        for base, resp in self._by_base.items():
            if url.startswith(base):
                return resp
        return _Resp(404)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    # Strip the ~0.4s rate-limit gap so the 3-attempt loops don't slow the test.
    async def _noop(self):
        return None
    monkeypatch.setattr(MyAnimeListClient, "_throttle", _noop)


def _client(bases, session, monkeypatch):
    c = MyAnimeListClient()
    c._bases = bases
    c._base = bases[0]
    # ``_session`` is a property (data descriptor) — an instance ``__dict__`` entry
    # can't shadow it, so patch the class property to return our fake transport.
    monkeypatch.setattr(type(c), "_session", property(lambda self: session))
    return c


async def test_primary_504_fails_over_to_local(monkeypatch):
    sess = _FakeSession({
        PRIMARY: _Resp(504),
        LOCAL: _Resp(200, {"data": {"mal_id": 1, "title": "Cowboy Bebop"}}),
    })
    c = _client([PRIMARY, LOCAL], sess, monkeypatch)
    out = await c._get("anime/1/full")
    assert out == {"mal_id": 1, "title": "Cowboy Bebop"}      # got the local payload
    assert any(u.startswith(LOCAL) for u in sess.calls)        # local was actually hit
    assert any(u.startswith(PRIMARY) for u in sess.calls)      # primary tried first


async def test_primary_success_never_touches_fallback(monkeypatch):
    sess = _FakeSession({
        PRIMARY: _Resp(200, {"data": {"mal_id": 1}}),
        LOCAL: _Resp(200, {"data": {"mal_id": 999}}),
    })
    c = _client([PRIMARY, LOCAL], sess, monkeypatch)
    out = await c._get("anime/1/full")
    assert out == {"mal_id": 1}                                # primary payload
    assert all(not u.startswith(LOCAL) for u in sess.calls)    # fallback untouched


async def test_both_bases_down_returns_none(monkeypatch):
    sess = _FakeSession({PRIMARY: _Resp(504), LOCAL: _Resp(504)})
    c = _client([PRIMARY, LOCAL], sess, monkeypatch)
    out = await c._get("anime/1/full")
    assert out is None                                          # graceful miss
    assert any(u.startswith(LOCAL) for u in sess.calls)         # still tried the fallback


async def test_404_on_primary_does_not_fail_over(monkeypatch):
    # A definitive 404 (not found) is an answer, not an outage — don't waste the
    # fallback on it.
    sess = _FakeSession({PRIMARY: _Resp(404), LOCAL: _Resp(200, {"data": {"mal_id": 7}})})
    c = _client([PRIMARY, LOCAL], sess, monkeypatch)
    out = await c._get("anime/999999/full")
    assert out is None
    assert all(not u.startswith(LOCAL) for u in sess.calls)     # 404 = stop, no failover


async def test_single_base_no_fallback_still_works(monkeypatch):
    sess = _FakeSession({PRIMARY: _Resp(200, {"data": {"mal_id": 1}})})
    c = _client([PRIMARY], sess, monkeypatch)
    out = await c._get("anime/1/full")
    assert out == {"mal_id": 1}


async def test_search_list_payload_not_double_unwrapped(monkeypatch):
    # A search (/anime?q=) returns a LIST under data; _get must return it as-is.
    sess = _FakeSession({PRIMARY: _Resp(200, {"data": [{"mal_id": 1}, {"mal_id": 2}]})})
    c = _client([PRIMARY], sess, monkeypatch)
    out = await c._get("anime", {"q": "x"})
    assert out == [{"mal_id": 1}, {"mal_id": 2}]
