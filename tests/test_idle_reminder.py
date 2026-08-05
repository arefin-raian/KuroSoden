"""Tests for kurosoden/shared/idle_reminder.py — the idle-admin nudge job.

Covers (per PLAN §7 / §9):
  • Fires: work waiting + an on-shift idle admin → one DM, cooldown marked
  • Suppressed: paused campaign mode
  • Suppressed: nothing in the line
  • Suppressed: admin already mid-task (not idle)
  • Suppressed: within the per-admin cooldown window (fake Redis clock)
  • A failing DM never stops the tick or the other admins
"""

from __future__ import annotations

import pytest

from kurosoden.shared.idle_reminder import make_idle_nudge_job
from kurosoden.shared.management_service import ManagementService


# ── Fakes ──────────────────────────────────────────────────────────────────────

class FakeRedis:
    """In-memory Redis honouring the ``kurosoden:*`` string keys the job uses.

    ``ex`` is accepted and ignored — cooldown is modelled by key presence, which
    is exactly what the job checks. Tests set/clear keys to simulate the clock."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class FakeClient:
    def __init__(self, fail_ids: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_ids = fail_ids or set()

    async def send_message(self, chat_id, text, *a, **kw):
        if chat_id in self.fail_ids:
            raise RuntimeError("blocked by user")
        self.sent.append((chat_id, text))


class FakePipelineManager:
    def __init__(self, client):
        # Levi owns the idle nudge (download detail); idle_reminder reads mgr.levi.
        self.levi = client


class FakeContainer:
    """Only the attributes idle_reminder actually reaches for."""

    def __init__(self, sessionmaker, *, redis=None, client=None, mode="normal"):
        self.pg_sessionmaker = sessionmaker
        self.redis = redis
        self.pipeline_manager = FakePipelineManager(client) if client else None
        self._mode = mode
        # get_mode() reads redis; mirror the mode into the fake store.
        if redis is not None:
            redis.store["kurosoden:mode"] = mode


async def _seed_idle_admin(sessionmaker, tid=1000, stage="levi"):
    """One available, on-shift, zero-task admin covering ``stage``."""
    svc = ManagementService(sessionmaker)
    await svc.ensure_admin(tid, name=f"Admin{tid}")
    await svc.set_bots(tid, [stage])
    return svc


async def _seed_work(sessionmaker, n=2):
    from kurosoden.shared.work_service import WorkService
    await WorkService(sessionmaker).add_batch(
        1, [{"anime_title": f"W{i}"} for i in range(n)])


# ── Fire ─────────────────────────────────────────────────────────────────────

class TestFires:
    async def test_nudges_idle_admin_when_work_waits(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_work(sessionmaker, 2)
        redis = FakeRedis()
        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=redis, client=client)

        await make_idle_nudge_job(c)()

        assert len(client.sent) == 1
        assert client.sent[0][0] == 1000
        # Cooldown key was written so we don't ping again next tick.
        assert redis.store.get("kurosoden:idle_nudge:1000") == "1"

    async def test_nudges_multiple_idle_admins(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_idle_admin(sessionmaker, 1001)
        await _seed_work(sessionmaker, 1)
        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=client)

        await make_idle_nudge_job(c)()
        assert {t for t, _ in client.sent} == {1000, 1001}


# ── Suppress ───────────────────────────────────────────────────────────────────

class TestSuppressed:
    async def test_paused_mode_nudges_no_one(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_work(sessionmaker, 3)
        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=client, mode="paused")
        await make_idle_nudge_job(c)()
        assert client.sent == []

    async def test_no_work_no_nudge(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=client)
        await make_idle_nudge_job(c)()
        assert client.sent == []

    async def test_busy_admin_not_nudged(self, sessionmaker, session):
        """An admin mid-task is not idle → suppressed (wake the idle, not the busy)."""
        from kurosoden.shared.admin_assignment import AdminAssignment
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_work(sessionmaker, 2)
        # Give 1000 an active task so idle_admins drops them.
        session.add(AdminAssignment(admin_telegram_id=1000, request_code="REQ-x",
                                    stage="levi", status="in_progress"))
        await session.commit()

        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=client)
        await make_idle_nudge_job(c)()
        assert client.sent == []

    async def test_cooldown_suppresses_repeat(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_work(sessionmaker, 2)
        redis = FakeRedis()
        # Pre-mark as recently nudged → this tick must stay silent.
        redis.store["kurosoden:idle_nudge:1000"] = "1"
        client = FakeClient()
        c = FakeContainer(sessionmaker, redis=redis, client=client)
        await make_idle_nudge_job(c)()
        assert client.sent == []

    async def test_no_client_no_crash(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_work(sessionmaker, 1)
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=None)
        await make_idle_nudge_job(c)()  # pipeline_manager is None → early return


# ── Resilience ───────────────────────────────────────────────────────────────

class TestResilience:
    async def test_failing_dm_does_not_stop_others(self, sessionmaker, session):
        await _seed_idle_admin(sessionmaker, 1000)
        await _seed_idle_admin(sessionmaker, 1001)
        await _seed_work(sessionmaker, 1)
        client = FakeClient(fail_ids={1000})  # first DM raises
        c = FakeContainer(sessionmaker, redis=FakeRedis(), client=client)

        await make_idle_nudge_job(c)()
        # 1001 still got its nudge despite 1000 blowing up.
        assert (1001,) == tuple(t for t, _ in client.sent)

    async def test_tick_never_raises(self, sessionmaker, session):
        """A broken container attribute is swallowed, not propagated."""
        class Broken:
            pg_sessionmaker = sessionmaker
            redis = None
            @property
            def pipeline_manager(self):
                raise RuntimeError("boom")
        # Should complete without raising.
        await make_idle_nudge_job(Broken())()
