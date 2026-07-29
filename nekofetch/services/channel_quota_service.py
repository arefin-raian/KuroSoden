"""Per-userbot channel-quota tracking for the two-scope creation flow.

Telegram caps how many public channels one account may own (~10 for most, more
for some). When the operator opts to have a **userbot** create a distribution
channel (rather than making it themselves), we must pick a session that still has
a free slot — and never reveal which one to the operator.

A session's used-slot count is the sum of:
  • channels we created through it and recorded (``DistributionBot.creation_scope
    == "userbot"`` with ``userbot_account`` set), and
  • public channels the account *already owned* before we started (seeded once via
    :meth:`seed_preexisting`, stored in Redis) — otherwise a fresh deployment would
    over-promise slots on an account that's already near its cap.

:meth:`pick_available` returns a random account name with a free slot (blind
choice — the caller passes it back to the userbot pool by name), or ``None`` when
every account is full so the flow can tell the operator to create their own.
"""

from __future__ import annotations

import random

from sqlalchemy import func, select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import safe_redis_get, safe_redis_set
from nekofetch.infrastructure.database.postgres.models import DistributionBot
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)

# Redis key holding an account's pre-existing (pre-us) public-channel count.
_PREEXISTING_KEY = "nf:userbot:{name}:preexisting_channels"


class ChannelQuotaService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self.cap = int(getattr(self._c.config.bot, "max_channels_per_account", 10))

    def _account_names(self) -> list[str]:
        """Names of every configured userbot account (order preserved)."""
        try:
            from nekofetch.sources.telegram.userbot import UserbotPool

            pool = UserbotPool.from_env(
                self._c.env.telegram_api_id, self._c.env.telegram_api_hash,
                str(self._c.env.session_path),
            )
            return [a.name for a in pool.accounts]
        except Exception as exc:  # noqa: BLE001 — no pool → no userbot creation
            log.warning("quota.accounts.load_failed", error=str(exc))
            return []

    async def _preexisting(self, name: str) -> int:
        raw = await safe_redis_get(self._c.redis, _PREEXISTING_KEY.format(name=name))
        try:
            return int(raw) if raw else 0
        except (TypeError, ValueError):
            return 0

    async def seed_preexisting(self, name: str, count: int) -> None:
        """Record how many public channels ``name`` already owned before us.

        Called once per account at setup (or when an operator corrects the tally)
        so quota math accounts for channels we didn't create. Idempotent — a later
        call overwrites the stored value."""
        await safe_redis_set(
            self._c.redis, _PREEXISTING_KEY.format(name=name), str(max(0, count)),
        )
        log.info("quota.preexisting.seeded", account=name, count=count)

    async def _created_counts(self) -> dict[str, int]:
        """Channels we created per userbot account (from the bots table)."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (
                await session.execute(
                    select(DistributionBot.userbot_account, func.count())
                    .where(
                        DistributionBot.is_channel.is_(True),
                        DistributionBot.creation_scope == "userbot",
                        DistributionBot.userbot_account.is_not(None),
                    )
                    .group_by(DistributionBot.userbot_account)
                )
            ).all()
        return {name: int(n) for name, n in rows if name}

    async def used_slots(self, name: str) -> int:
        """Total used slots for ``name`` = pre-existing + channels we created."""
        created = (await self._created_counts()).get(name, 0)
        return created + await self._preexisting(name)

    async def free_slots(self, name: str) -> int:
        """Remaining free channel slots for ``name`` (never negative)."""
        return max(0, self.cap - await self.used_slots(name))

    async def availability(self) -> dict[str, int]:
        """Free-slot count for every configured account (name → free slots)."""
        created = await self._created_counts()
        out: dict[str, int] = {}
        for name in self._account_names():
            used = created.get(name, 0) + await self._preexisting(name)
            out[name] = max(0, self.cap - used)
        return out

    async def pick_available(self) -> str | None:
        """Return a random account name with ≥1 free slot, or None if all full.

        Blind by design: the operator opting into userbot creation never learns
        which account was used. Accounts with zero free slots are excluded so we
        never try to create past a session's Telegram cap."""
        avail = await self.availability()
        candidates = [name for name, free in avail.items() if free > 0]
        if not candidates:
            log.warning("quota.no_available_account", accounts=len(avail))
            return None
        # Vary the pick without Math.random-equivalent determinism concerns: the
        # standard library RNG is fine here (not security-sensitive, not resumed).
        return random.choice(candidates)
