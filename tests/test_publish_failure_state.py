"""Publishing failure must leave the request retryable."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kurosoden.shared.admin_assignment import AdminAssignment
from nekofetch.infrastructure.database.postgres.models import Request, User

pytestmark = pytest.mark.asyncio


def _container(sessionmaker):
    return SimpleNamespace(
        pg_sessionmaker=sessionmaker,
        redis=None,
        admin_client=None,
        pipeline_manager=None,
        config=SimpleNamespace(
            features=SimpleNamespace(
                thumbnail_generation=False,
                distribution_bots=False,
            ),
            bot=SimpleNamespace(auto_create_on_publish=False),
            storage_channel=SimpleNamespace(enabled=False, stats_message_enabled=False),
        ),
    )


async def test_failed_main_publish_does_not_mark_request_published(
    sessionmaker, monkeypatch,
):
    from sqlalchemy import select

    from nekofetch.services.publishing_service import PublishingService
    from tests.helpers import _create_request
    from tests.test_publish_task_cleanup import _patch_heavy_deps

    async with sessionmaker() as session:
        user = User(telegram_id=940001, username="retry", first_name="Retry")
        session.add(user)
        await session.commit()
        await _create_request(
            session,
            code="REQ-PUBLISH-FAIL",
            user_id=user.id,
            anime_title="Retryable Publish",
            status="ready",
        )
        session.add(AdminAssignment(
            admin_telegram_id=940001,
            request_code="REQ-PUBLISH-FAIL",
            stage="gojo",
            status="assigned",
        ))
        await session.commit()

    _patch_heavy_deps(monkeypatch, main_result=None)

    with pytest.raises(RuntimeError, match="main channel publish returned no message id"):
        await PublishingService(_container(sessionmaker)).publish("REQ-PUBLISH-FAIL")

    async with sessionmaker() as session:
        request = (await session.execute(
            select(Request).where(Request.code == "REQ-PUBLISH-FAIL")
        )).scalar_one()
        assignment = (await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-PUBLISH-FAIL",
                AdminAssignment.stage == "gojo",
            )
        )).scalar_one()
        assert request.status.value == "ready"
        assert assignment.status == "assigned"
