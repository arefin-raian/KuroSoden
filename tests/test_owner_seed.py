"""Tests for owner-seed's id resolution.

The seeding itself is DB + service wiring (exercised indirectly by startup); the
one piece with real branching logic is ``_owner_id`` — which source wins when
``security.owner_id`` is set vs. falling back to ``ADMIN_IDS[0]``. Pure, no DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kurosoden.shared.admin_assignment import AdminAvailability
from kurosoden.shared.owner_seed import (
    _configured_principals,
    _owner_id,
    seed_configured_principals,
)
from nekofetch.domain.enums import Role
from nekofetch.infrastructure.database.postgres.models import User
from nekofetch.services.auth_service import AuthService


def _container(*, owner_id: int, admin_ids: list[int]):
    return SimpleNamespace(
        config=SimpleNamespace(security=SimpleNamespace(owner_id=owner_id)),
        env=SimpleNamespace(admin_ids=admin_ids),
    )


def _db_container(sessionmaker, *, owner_id: int, admin_ids: list[int]):
    c = _container(owner_id=owner_id, admin_ids=admin_ids)
    c.pg_sessionmaker = sessionmaker
    return c


class _FakeClient:
    def __init__(self, users: dict[int, tuple[str | None, str | None]]) -> None:
        self._users = users

    async def get_users(self, telegram_id: int):
        first_name, username = self._users[telegram_id]
        return SimpleNamespace(first_name=first_name, username=username)


def test_configured_owner_id_wins():
    c = _container(owner_id=777, admin_ids=[111, 222])
    assert _owner_id(c) == 777


def test_falls_back_to_first_admin_id():
    c = _container(owner_id=0, admin_ids=[111, 222])
    assert _owner_id(c) == 111


def test_env_owner_id_is_honored_when_yaml_owner_is_unset():
    c = _container(owner_id=0, admin_ids=[111, 222])
    c.env.owner_id = 999
    assert _owner_id(c) == 999
    assert AuthService(c).owner_ids() == {999}


def test_none_when_no_owner_and_no_admins():
    c = _container(owner_id=0, admin_ids=[])
    assert _owner_id(c) is None


def test_configured_principals_include_owner_and_admin_ids_once():
    c = _container(owner_id=777, admin_ids=[111, 777, 222])

    principals = _configured_principals(c)

    assert [(p.telegram_id, p.is_owner) for p in principals] == [
        (777, True),
        (111, False),
        (222, False),
    ]


@pytest.mark.asyncio
async def test_seed_creates_owner_and_env_admin_rows(sessionmaker):
    c = _db_container(sessionmaker, owner_id=6161189904, admin_ids=[101, 202])
    client = _FakeClient({
        6161189904: ("Raian", "owner_handle"),
        101: ("Milly", "milly"),
        202: (None, "kallen"),
    })

    await seed_configured_principals(c, client=client)

    async with sessionmaker() as session:
        users = (
            await session.execute(select(User).order_by(User.telegram_id))
        ).scalars().all()
        admins = (
            await session.execute(
                select(AdminAvailability).order_by(AdminAvailability.admin_telegram_id)
            )
        ).scalars().all()

    assert [(u.telegram_id, u.first_name, u.username, u.role) for u in users] == [
        (101, "Milly", "milly", Role.ADMIN),
        (202, "kallen", "kallen", Role.ADMIN),
        (6161189904, "Raian", "owner_handle", Role.ADMIN),
    ]
    assert admins[0].admin_name == "Milly"
    assert admins[0].assigned_bots == []
    assert admins[1].admin_name == "kallen"
    assert admins[1].assigned_bots == []
    assert admins[2].admin_name == "Raian"
    assert set(admins[2].assigned_bots) == {"lelouch", "levi", "senku", "gojo"}


@pytest.mark.asyncio
async def test_seed_refreshes_only_seed_generated_labels(sessionmaker):
    owner_id = 6161189905
    c = _db_container(sessionmaker, owner_id=owner_id, admin_ids=[])

    async with sessionmaker() as session:
        session.add(User(telegram_id=owner_id, first_name="Owner", role=Role.ADMIN))
        session.add(
            AdminAvailability(
                admin_telegram_id=owner_id,
                admin_name="Owner",
                is_available=True,
                assigned_bots=[],
                scheduled_breaks=[],
                weight=1,
            )
        )
        await session.commit()

    await seed_configured_principals(
        c,
        client=_FakeClient({owner_id: ("Raian", "owner_handle")}),
    )

    async with sessionmaker() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == owner_id))
        ).scalar_one()
        admin = (
            await session.execute(
                select(AdminAvailability).where(
                    AdminAvailability.admin_telegram_id == owner_id
                )
            )
        ).scalar_one()

    assert user.first_name == "Raian"
    assert user.username == "owner_handle"
    assert admin.admin_name == "Raian"
    assert admin.assigned_bots == []


@pytest.mark.asyncio
async def test_seed_preserves_manual_database_edits(sessionmaker):
    owner_id = 6161189906
    c = _db_container(sessionmaker, owner_id=owner_id, admin_ids=[])

    async with sessionmaker() as session:
        session.add(
            User(
                telegram_id=owner_id,
                first_name="Manual Name",
                username="manual",
                role=Role.USER,
            )
        )
        session.add(
            AdminAvailability(
                admin_telegram_id=owner_id,
                admin_name="Manual Pool",
                is_available=False,
                assigned_bots=["levi"],
                scheduled_breaks=[],
                weight=4,
            )
        )
        await session.commit()

    await seed_configured_principals(
        c,
        client=_FakeClient({owner_id: ("Raian", "owner_handle")}),
    )

    async with sessionmaker() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == owner_id))
        ).scalar_one()
        admin = (
            await session.execute(
                select(AdminAvailability).where(
                    AdminAvailability.admin_telegram_id == owner_id
                )
            )
        ).scalar_one()

    assert user.first_name == "Manual Name"
    assert user.username == "manual"
    assert user.role == Role.USER
    assert admin.admin_name == "Manual Pool"
    assert admin.is_available is False
    assert admin.assigned_bots == ["levi"]
    assert admin.weight == 4


@pytest.mark.asyncio
async def test_auth_resolve_creates_configured_principal_but_preserves_existing_role(
    sessionmaker,
):
    c = _db_container(sessionmaker, owner_id=6161189904, admin_ids=[101])
    auth = AuthService(c)

    created = await auth.resolve_user(101, username="milly", first_name="Milly")

    assert created.role == Role.ADMIN

    async with sessionmaker() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == 101))
        ).scalar_one()
        user.role = Role.USER
        await session.commit()

    resolved = await auth.resolve_user(101, username="newname", first_name="New")

    assert resolved.role == Role.USER
    assert resolved.username == "milly"
    assert resolved.first_name == "Milly"
