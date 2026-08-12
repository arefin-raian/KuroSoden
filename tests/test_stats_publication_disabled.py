"""Automatic database-statistics publication is opt-in.

Pack storage and the manual Gojo statistics dashboard remain available; only the
startup/publish fan-out to the pinned storage-channel message is gated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.core.config import AppConfig
from nekofetch.services import stats_service


def _container(*, enabled: bool):
    return SimpleNamespace(
        config=SimpleNamespace(
            storage_channel=SimpleNamespace(
                enabled=True,
                stats_message_enabled=enabled,
            ),
        ),
    )


def test_database_stats_message_is_off_by_default():
    assert AppConfig().storage_channel.stats_message_enabled is False


def test_deployed_config_keeps_database_stats_message_off():
    config = AppConfig.load()
    assert config.storage_channel.stats_message_enabled is False


@pytest.mark.asyncio
async def test_automatic_refresh_does_not_call_telegram_when_disabled(monkeypatch):
    called = False

    async def _refresh(_self):
        nonlocal called
        called = True
        return 123

    monkeypatch.setattr(stats_service.StatsService, "refresh", _refresh)

    assert await stats_service.refresh_automatic(_container(enabled=False)) is None
    assert called is False


@pytest.mark.asyncio
async def test_automatic_refresh_remains_reenableable(monkeypatch):
    called = False

    async def _refresh(_self):
        nonlocal called
        called = True
        return 123

    monkeypatch.setattr(stats_service.StatsService, "refresh", _refresh)

    assert await stats_service.refresh_automatic(_container(enabled=True)) == 123
    assert called is True
