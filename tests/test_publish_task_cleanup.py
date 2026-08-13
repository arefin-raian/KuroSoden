"""Publish task-cleanup regression (Phase 11).

A request that was force-published (or published through any path) must leave
the operator's /tasks list and must not double-post later. The bug: the Gojo
stage assignment stayed ``assigned`` and a pending schedule stayed ``pending``
because the button/schedule paths were the only places that completed them.

Now ``PublishingService.publish`` itself finishes the bookkeeping on EVERY
successful publish path — completing the gojo assignment (idempotent) and
cancelling any pending scheduled post for the request code. This test pins both
behaviours against the real service with every network/DB heavy-hitter faked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nekofetch.infrastructure.database.postgres.models import ScheduledPost
from kurosoden.shared.admin_assignment import AdminAssignmentEngine

pytestmark = pytest.mark.asyncio


def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        admin_client=None,
        pipeline_manager=None,
        config=SimpleNamespace(
            features=SimpleNamespace(thumbnail_generation=False, distribution_bots=False),
            bot=SimpleNamespace(auto_create_on_publish=False),
            storage_channel=SimpleNamespace(enabled=False, stats_message_enabled=False),
        ),
    )


def _patch_heavy_deps(monkeypatch, *, main_result=123):
    """Replace every network/DB-heavy collaborator of ``publish`` with a stub.

    All of these are imported lazily INSIDE ``publish``, so patching the module
    attribute is enough — the publish flow never touches Telegram/Redis here.
    Returns the recorded call log.
    """
    calls: list[str] = []

    class _Analytics:
        def __init__(self, _c):
            pass

        async def record(self, *a, **k):
            calls.append("analytics")

    class _Main:
        def __init__(self, _c):
            pass

        async def publish(self, *a, **k):
            calls.append("main")
            return main_result

    class _Index:
        def __init__(self, _c):
            pass

        @staticmethod
        def letter_of(title):
            return "T"

        async def refresh_letter(self, *a, **k):
            calls.append("index")

    class _Backup:
        def __init__(self, _c):
            pass

        async def backup_one(self, *a, **k):
            calls.append("backup_one")

        async def record_index(self, *a, **k):
            calls.append("record_index")

        async def record_distribution_channel(self, *a, **k):
            calls.append("backup_dist")

    class _Log:
        def __init__(self, _c):
            pass

        async def event(self, *a, **k):
            calls.append("log")

    class _Notify:
        def __init__(self, _c):
            pass

        async def request_published(self, *a, **k):
            calls.append("notify")

    monkeypatch.setattr(
        "nekofetch.services.analytics_service.AnalyticsService", _Analytics,
    )
    monkeypatch.setattr(
        "nekofetch.services.main_channel_service.MainChannelService", _Main,
    )
    monkeypatch.setattr(
        "nekofetch.services.index_channel_service.IndexChannelService", _Index,
    )
    monkeypatch.setattr(
        "nekofetch.services.backup_service.BackupService", _Backup,
    )
    monkeypatch.setattr(
        "nekofetch.services.log_channel_service.LogChannelService", _Log,
    )
    monkeypatch.setattr(
        "nekofetch.services.notification_service.NotificationService", _Notify,
    )
    return calls


async def test_publish_completes_gojo_task_and_cancels_schedule(
    sessionmaker, monkeypatch,
):
    from sqlalchemy import select

    from nekofetch.infrastructure.database.postgres.models import Request, User
    from nekofetch.services.publishing_service import PublishingService
    from tests.helpers import _create_request

    completed: list[tuple[str, str]] = []

    async def _complete(self, code, stage, _session=None):
        completed.append((code, stage))

    monkeypatch.setattr(AdminAssignmentEngine, "complete_task", _complete)
    calls = _patch_heavy_deps(monkeypatch)

    async with sessionmaker() as s:
        user = User(telegram_id=777001, username="pub", first_name="Pub",
                    language="en")
        s.add(user)
        await s.commit()
        await _create_request(s, code="REQ-CLEAN", user_id=user.id,
                              anime_title="Cleanup Test")
        # A pending schedule that must be cancelled by the publish itself.
        s.add(ScheduledPost(
            request_code="REQ-CLEAN", admin_telegram_id=777001,
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
            status="pending",
        ))
        await s.commit()

    svc = PublishingService(_container(sessionmaker))
    count = await svc.publish("REQ-CLEAN")

    assert count == 0
    # The gojo-stage task was completed (this is what removes it from /tasks).
    assert ("REQ-CLEAN", "gojo") in completed
    # The pending schedule was cancelled — no double-post later.
    async with sessionmaker() as s:
        rows = (await s.execute(
            select(ScheduledPost).where(ScheduledPost.request_code == "REQ-CLEAN")
        )).scalars().all()
    assert len(rows) == 1 and rows[0].status == "cancelled"
    # The request itself is now PUBLISHED.
    async with sessionmaker() as s:
        req = (await s.execute(
            select(Request).where(Request.code == "REQ-CLEAN")
        )).scalar_one()
        assert req.status.value == "published"
    # Sanity: the happy path still exercised the main/index/log steps.
    assert {"main", "index", "log", "notify", "analytics"} <= set(calls)


async def test_publish_cleanup_is_best_effort_on_missing_assignment(
    sessionmaker, monkeypatch,
):
    """A publish with NO gojo assignment row must not crash the cleanup."""
    from nekofetch.infrastructure.database.postgres.models import User
    from nekofetch.services.publishing_service import PublishingService
    from tests.helpers import _create_request

    _patch_heavy_deps(monkeypatch)

    async with sessionmaker() as s:
        user = User(telegram_id=777002, username="pub2", first_name="Pub2",
                    language="en")
        s.add(user)
        await s.commit()
        await _create_request(s, code="REQ-NOASSIGN", user_id=user.id,
                              anime_title="No Assignment")

    # Real AdminAssignmentEngine.complete_task with no row → must be a no-op.
    svc = PublishingService(_container(sessionmaker))
    count = await svc.publish("REQ-NOASSIGN")
    assert count == 0
