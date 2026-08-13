"""Seed configured owner/admin identities into durable database rows.

Config still decides who the owner is, and ``ADMIN_IDS`` still declares the
bootstrap admin set. The database stores the durable user/admin profile. This
module bridges those two facts on boot without clobbering later operator edits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ConfiguredPrincipal:
    telegram_id: int
    is_owner: bool

    @property
    def fallback_name(self) -> str:
        return "Owner" if self.is_owner else f"Admin {self.telegram_id}"


@dataclass(frozen=True)
class TelegramProfile:
    username: str | None = None
    first_name: str | None = None

    @property
    def display_name(self) -> str | None:
        return self.first_name or self.username


def _owner_id(container: Container) -> int | None:
    """Return ``security.owner_id`` when set, otherwise ``ADMIN_IDS[0]``."""
    configured = int(getattr(container.config.security, "owner_id", 0) or 0)
    if configured:
        return configured
    env_owner = int(getattr(container.env, "owner_id", 0) or 0)
    if env_owner:
        return env_owner
    admin_ids = list(getattr(container.env, "admin_ids", []) or [])
    return int(admin_ids[0]) if admin_ids else None


def _configured_principals(container: Container) -> list[ConfiguredPrincipal]:
    owner_id = _owner_id(container)
    seen: set[int] = set()
    principals: list[ConfiguredPrincipal] = []

    if owner_id is not None:
        owner_id = int(owner_id)
        principals.append(ConfiguredPrincipal(owner_id, True))
        seen.add(owner_id)

    for raw_id in list(getattr(container.env, "admin_ids", []) or []):
        admin_id = int(raw_id)
        if admin_id <= 0 or admin_id in seen:
            continue
        principals.append(ConfiguredPrincipal(admin_id, False))
        seen.add(admin_id)

    return principals


def _seed_name(value: str | None, principal: ConfiguredPrincipal) -> bool:
    if not value:
        return True
    return value in {"Owner", f"Admin {principal.telegram_id}"}


async def _telegram_profile(client: Any, telegram_id: int) -> TelegramProfile:
    if client is None:
        return TelegramProfile()
    try:
        user = await client.get_users(telegram_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("principal_seed.profile_lookup_failed", user=telegram_id, error=str(exc))
        return TelegramProfile()
    return TelegramProfile(
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
    )


async def seed_configured_principals(container: Container, *, client: Any = None) -> None:
    """Create missing DB rows for configured owner/admin ids.

    Existing rows are preserved. Only blank or seed-generated labels get replaced
    with Telegram profile data, so a name edited in the database stays intact even
    when the env/config values still contain the same id.
    """
    principals = _configured_principals(container)
    if not principals:
        log.warning("principal_seed.no_configured_principals")
        return

    from sqlalchemy import select

    from kurosoden.shared.admin_assignment import AdminAvailability
    from kurosoden.shared.management_service import STAGES
    from nekofetch.domain.enums import Role
    from nekofetch.infrastructure.database.postgres.models import User
    from nekofetch.infrastructure.database.postgres.session import session_scope

    profiles = {
        principal.telegram_id: await _telegram_profile(client, principal.telegram_id)
        for principal in principals
    }

    try:
        async with session_scope(container.pg_sessionmaker) as session:
            for principal in principals:
                profile = profiles[principal.telegram_id]
                fallback = principal.fallback_name
                display_name = profile.display_name or fallback

                user = (
                    await session.execute(
                        select(User).where(User.telegram_id == principal.telegram_id)
                    )
                ).scalar_one_or_none()
                if user is None:
                    user = User(
                        telegram_id=principal.telegram_id,
                        username=profile.username,
                        first_name=display_name,
                        role=Role.ADMIN,
                    )
                    session.add(user)
                else:
                    if profile.username and not user.username:
                        user.username = profile.username
                    if display_name and _seed_name(user.first_name, principal):
                        user.first_name = display_name

                availability = (
                    await session.execute(
                        select(AdminAvailability).where(
                            AdminAvailability.admin_telegram_id == principal.telegram_id
                        )
                    )
                ).scalar_one_or_none()
                if availability is None:
                    availability = AdminAvailability(
                        admin_telegram_id=principal.telegram_id,
                        admin_name=display_name,
                        is_available=True,
                        assigned_bots=list(STAGES) if principal.is_owner else [],
                        scheduled_breaks=[],
                        weight=1,
                    )
                    session.add(availability)
                else:
                    if display_name and _seed_name(availability.admin_name, principal):
                        availability.admin_name = display_name

            await session.flush()
        log.info("principal_seed.done", count=len(principals))
    except Exception as exc:  # noqa: BLE001
        log.warning("principal_seed.failed", error=str(exc))


async def seed_owner(container: Container) -> None:
    """Backward-compatible boot hook for the container startup path."""
    await seed_configured_principals(container)
