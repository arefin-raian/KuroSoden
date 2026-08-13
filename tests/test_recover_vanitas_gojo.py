"""Regression tests for the manual Vanitas → Gojo task recovery helper."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_recovery_helper_is_dry_run_without_writing(sessionmaker):
    from kurosoden.scripts.recover_vanitas_gojo import ensure_gojo_assignment
    from nekofetch.infrastructure.database.postgres.models import User
    from tests.helpers import _create_request

    async with sessionmaker() as session:
        user = User(telegram_id=930001, username="vanitas", first_name="Vanitas")
        session.add(user)
        await session.commit()
        await _create_request(
            session,
            code="REQ-VANITAS-DRY",
            user_id=user.id,
            anime_title="The Case Study of Vanitas (Dry Run)",
            anime_doc_id="anilist:48580",
            status="published",
        )

    async with sessionmaker() as session:
        result = await ensure_gojo_assignment(
            session, "REQ-VANITAS-DRY", 6161189904, apply=False,
        )
        assert not result.created
        assert "dry run" in result.reason
        await session.rollback()

    from kurosoden.shared.admin_assignment import AdminAssignment
    from sqlalchemy import select

    async with sessionmaker() as session:
        rows = (await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-VANITAS-DRY",
            )
        )).scalars().all()
        assert rows == []


async def test_recovery_helper_creates_one_open_task_idempotently(sessionmaker):
    from kurosoden.scripts.recover_vanitas_gojo import ensure_gojo_assignment
    from nekofetch.infrastructure.database.postgres.models import User
    from tests.helpers import _create_request

    async with sessionmaker() as session:
        user = User(telegram_id=930002, username="vanitas2", first_name="Vanitas 2")
        session.add(user)
        await session.commit()
        await _create_request(
            session,
            code="REQ-VANITAS-APPLY",
            user_id=user.id,
            anime_title="The Case Study of Vanitas (Apply)",
            anime_doc_id="anilist:48580",
            status="published",
        )

    async with sessionmaker() as session:
        first = await ensure_gojo_assignment(
            session, "REQ-VANITAS-APPLY", 6161189904, apply=True,
        )
        await session.commit()
        assert first.created
        assert first.admin_telegram_id == 6161189904

    async with sessionmaker() as session:
        second = await ensure_gojo_assignment(
            session, "REQ-VANITAS-APPLY", 999999999, apply=True,
        )
        await session.commit()
        assert not second.created
        assert second.admin_telegram_id == 6161189904
        assert "already exists" in second.reason

    from kurosoden.shared.admin_assignment import AdminAssignment
    from sqlalchemy import select

    async with sessionmaker() as session:
        rows = (await session.execute(
            select(AdminAssignment).where(
                AdminAssignment.request_code == "REQ-VANITAS-APPLY",
                AdminAssignment.stage == "gojo",
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "assigned"
        assert rows[0].assignment_mode == "fallback"


async def test_title_lookup_prefers_case_insensitive_exact_match(sessionmaker):
    from kurosoden.scripts.recover_vanitas_gojo import _find_requests
    from nekofetch.infrastructure.database.postgres.models import User
    from tests.helpers import _create_request

    async with sessionmaker() as session:
        user = User(telegram_id=930003, username="vanitas3", first_name="Vanitas 3")
        session.add(user)
        await session.commit()
        await _create_request(
            session,
            code="REQ-VANITAS-LOOKUP",
            user_id=user.id,
            anime_title="The Case Study of Vanitas: Lookup Only",
            status="published",
        )
        rows, kind = await _find_requests(
            session, "the case study of vanitas: lookup only")
        assert kind == "exact"
        assert [row.code for row in rows] == ["REQ-VANITAS-LOOKUP"]
