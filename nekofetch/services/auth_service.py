"""Authentication & authorization service.

Resolves the acting user, determines their effective role (the ``.env`` admin
whitelist always wins), and answers permission checks used by the bot middleware.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nekofetch.core.config import EnvSettings
from nekofetch.core.container import Container
from nekofetch.core.exceptions import PermissionDenied
from nekofetch.domain.enums import ROLE_PERMISSIONS, Permission, Role
from nekofetch.infrastructure.database.postgres.models import User
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.infrastructure.repositories.user_repo import UserRepository

# How stale ``last_seen_at`` must be before we spend a write round-trip on it.
_LAST_SEEN_THROTTLE = timedelta(minutes=5)


class AuthService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._env: EnvSettings = container.env

    async def resolve_user(
        self, telegram_id: int, *, username: str | None = None, first_name: str | None = None
    ) -> User:
        async with session_scope(self._c.pg_sessionmaker) as session:
            repo = UserRepository(session)
            user = await repo.get_by_telegram_id(telegram_id)
            now = datetime.now(UTC)
            if user is None:
                role = Role.ADMIN if telegram_id in self._configured_principal_ids() else Role.USER
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    role=role,
                    last_seen_at=now,
                )
                await repo.add(user)
            else:
                if username and not user.username:
                    user.username = username
                if first_name and not user.first_name:
                    user.first_name = first_name
                # Throttle the last_seen write: this runs on EVERY tap in the
                # middleware, and a dirty COMMIT is a full round-trip to a
                # WAN-distant DB. Only touch it when it's stale (>5 min) so the
                # common case is a read-only transaction — no wasted write RTT.
                last = user.last_seen_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if last is None or (now - last) >= _LAST_SEEN_THROTTLE:
                    user.last_seen_at = now
            await session.flush()
            session.expunge(user)
            return user

    def role_of(self, user: User) -> Role:
        return Role(user.role)

    def owner_ids(self) -> set[int]:
        """The owner(s) — the only identities allowed to touch sensitive config.

        ``security.owner_id`` is authoritative when set; otherwise ``OWNER_ID``
        is used when configured, then the first env admin is treated as the owner
        so a fresh install still has one.
        """
        configured = self._c.config.security.owner_id
        if configured:
            return {configured}
        env_owner = int(getattr(self._env, "owner_id", 0) or 0)
        if env_owner:
            return {env_owner}
        return set(self._env.admin_ids[:1])

    def is_owner(self, user: User | None) -> bool:
        return bool(user) and user.telegram_id in self.owner_ids()

    def _configured_principal_ids(self) -> set[int]:
        ids = {int(admin_id) for admin_id in self._env.admin_ids}
        ids.update(self.owner_ids())
        return ids

    def has_permission(self, user: User, permission: Permission) -> bool:
        if user.is_banned:
            return False
        return permission in ROLE_PERMISSIONS.get(self.role_of(user), set())

    def require(self, user: User, permission: Permission) -> None:
        if not self.has_permission(user, permission):
            raise PermissionDenied(f"{self.role_of(user)} lacks {permission}")
