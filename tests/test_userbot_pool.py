"""Userbot pool lifecycle regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.providers.acute_bot import fetch_from_acutebot
from nekofetch.sources.telegram.userbot import (
    Account,
    UserbotPool,
    is_transport_error,
)


class _FakeClient:
    def __init__(self, *, name: str, healthy: bool = True):
        self.name = name
        self.is_connected = True
        self.healthy = healthy
        self.started = 0
        self.stopped = 0
        self.probes = 0

    async def get_me(self):
        self.probes += 1
        if not self.healthy:
            raise RuntimeError("TCPTransport closed")
        return SimpleNamespace(id=100 + self.probes)

    async def start(self):
        self.is_connected = True
        self.started += 1

    async def stop(self):
        self.is_connected = False
        self.stopped += 1


@pytest.mark.asyncio
async def test_acquire_retires_stale_active_transport_and_rolls_over(monkeypatch):
    pool = UserbotPool(
        12345,
        "hash",
        [Account("primary"), Account("backup")],
    )
    stale = _FakeClient(name="primary", healthy=False)
    backup = _FakeClient(name="backup", healthy=True)
    clients = {"primary": stale, "backup": backup}
    monkeypatch.setattr(pool, "_build", lambda account: clients[account.name])
    pool._clients["primary"] = stale
    pool._active = stale

    result = await pool.acquire()

    assert result is backup
    assert stale.stopped == 1
    assert "primary" not in pool._clients
    assert pool._active is backup
    assert backup.probes == 1


@pytest.mark.asyncio
async def test_execute_retires_and_rebuilds_after_mid_operation_failure(monkeypatch):
    pool = UserbotPool(12345, "hash", [Account("primary")])
    first = _FakeClient(name="primary", healthy=True)
    rebuilt = _FakeClient(name="primary", healthy=True)
    clients = iter((first, rebuilt))
    monkeypatch.setattr(pool, "_build", lambda _account: next(clients))

    calls = 0

    async def operation(client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("TCPTransport closed during ResolveUsername")
        return client.name

    result = await pool.execute(operation, retries=2)

    assert result == "primary"
    assert calls == 2
    assert first.stopped == 1
    assert pool._active is rebuilt


@pytest.mark.asyncio
async def test_acute_fetch_requests_bounded_second_attempt(monkeypatch):
    pool = UserbotPool(12345, "hash", [Account("primary")])
    observed: dict[str, int] = {}

    async def execute(_fn, *, retries=1, retry_on=None, max_attempts=None):
        observed["retries"] = retries
        observed["retry_on"] = retry_on
        observed["max_attempts"] = max_attempts
        return None

    monkeypatch.setattr(pool, "execute", execute)
    result = await fetch_from_acutebot("Cowboy Bebop", pool)

    assert result is None
    assert observed["retries"] == 2
    assert observed["retry_on"] is is_transport_error
    # Owner spec: @acutebot is hard-capped at 3 total attempts.
    assert observed["max_attempts"] == 3


@pytest.mark.asyncio
async def test_execute_does_not_retry_non_transport_errors():
    pool = UserbotPool(12345, "hash", [Account("primary")])
    client = _FakeClient(name="primary", healthy=True)
    pool._clients["primary"] = client
    pool._active = client
    calls = 0

    async def operation(_client):
        nonlocal calls
        calls += 1
        raise ValueError("invalid response")

    with pytest.raises(ValueError):
        await pool.execute(operation, retries=2, retry_on=is_transport_error)

    assert calls == 1
    assert client.stopped == 0


@pytest.mark.asyncio
async def test_execute_max_attempts_caps_total_tries(monkeypatch):
    # Many accounts × retries would allow >3 tries, but max_attempts=3 caps it.
    accounts = [Account(f"acc{i}") for i in range(5)]
    pool = UserbotPool(12345, "hash", accounts)
    calls = 0

    async def _acquire():
        return _FakeClient(name="x", healthy=True)

    monkeypatch.setattr(pool, "acquire", _acquire)
    monkeypatch.setattr(pool, "_retire", lambda *_a, **_k: _noop())

    async def operation(_client):
        nonlocal calls
        calls += 1
        raise ConnectionError("transport closed")  # transport error → retryable

    with pytest.raises(RuntimeError):
        await pool.execute(operation, retries=2, retry_on=is_transport_error,
                           max_attempts=3)
    assert calls == 3  # hard-capped, not 5*2=10


async def _noop():
    return None
