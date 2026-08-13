"""Coverage for durable scheduling + per-admin timezone (Workstream A).

Runs against the SQLite fixture engine + a fake publisher, so no Telegram calls
happen. Verifies:

  • timefmt: parse_local round-trips a wall-clock string through an admin's zone
    into aware UTC; to_tz renders back; a bad/None zone falls back safely;
  • AdminAssignmentEngine.get/set_timezone persists per-admin and creates a row
    when the admin has no availability record yet;
  • ScheduleService.schedule persists a pending row, supersedes an earlier
    pending schedule for the same request, and collision_window flags neighbours;
  • sweep_due publishes only past-due pending rows, marks them published, is
    idempotent, and records a failure (without wedging) when a publish raises.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nekofetch.core import timefmt
from nekofetch.infrastructure.database.postgres.models import ScheduledPost
from nekofetch.services.schedule_service import ScheduleService
from kurosoden.shared.admin_assignment import AdminAssignmentEngine

pytestmark = pytest.mark.asyncio


# ── timefmt helpers ────────────────────────────────────────────────────────────

def test_parse_local_and_to_tz_round_trip():
    # 15:30 wall-clock in Dhaka (UTC+6) → 09:30 UTC.
    utc = timefmt.parse_local("2026-08-01 15:30", "Asia/Dhaka")
    assert utc is not None
    assert utc.tzinfo is not None
    assert (utc.hour, utc.minute) == (9, 30)
    # Rendering back into Dhaka returns the original wall-clock (no label).
    assert timefmt.to_tz(utc, "Asia/Dhaka", with_label=False) == "2026-08-01 15:30"


def test_parse_local_bad_input_returns_none():
    assert timefmt.parse_local("not a date", "Asia/Dhaka") is None


def test_tz_for_falls_back_on_bad_zone():
    # An unknown zone must not raise — it falls back to the global display tz.
    assert timefmt.tz_for("Totally/Bogus") is timefmt.DISPLAY_TZ
    assert timefmt.tz_for(None) is timefmt.DISPLAY_TZ


def test_tz_offset_label_shape():
    assert timefmt.tz_offset_label("UTC") == "UTC+0"
    assert timefmt.tz_offset_label("Asia/Dhaka") == "UTC+6"


# ── per-admin timezone persistence ──────────────────────────────────────────────

async def test_set_and_get_timezone_creates_row(sessionmaker):
    engine = AdminAssignmentEngine(sessionmaker)
    assert await engine.get_timezone(999) is None
    await engine.set_timezone(999, "America/New_York", admin_name="Gojo")
    assert await engine.get_timezone(999) == "America/New_York"
    # Overwrite works and keeps the row.
    await engine.set_timezone(999, "Asia/Tokyo")
    assert await engine.get_timezone(999) == "Asia/Tokyo"


# ── ScheduleService ──────────────────────────────────────────────────────────

def _container(sessionmaker, *, publish=None):
    return SimpleNamespace(pg_sessionmaker=sessionmaker, admin_client=None, redis=None)


async def test_schedule_supersedes_earlier_pending(sessionmaker):
    svc = ScheduleService(_container(sessionmaker))
    t1 = datetime.now(timezone.utc) + timedelta(hours=2)
    t2 = datetime.now(timezone.utc) + timedelta(hours=3)
    await svc.schedule("REQ1", 1, t1, anime_title="One")
    await svc.schedule("REQ1", 1, t2, anime_title="One")  # re-schedule
    pending = await svc.list_pending()
    assert len(pending) == 1
    # SQLite doesn't round-trip tzinfo, so compare wall-clock components.
    got = pending[0].scheduled_at
    assert (got.hour, got.minute) == (t2.hour, t2.minute)


async def test_collision_window_flags_neighbours(sessionmaker):
    svc = ScheduleService(_container(sessionmaker))
    base = datetime.now(timezone.utc) + timedelta(hours=5)
    await svc.schedule("REQ1", 1, base, anime_title="One")
    # 5 minutes later is within the ±10min window.
    near = await svc.collision_window(base + timedelta(minutes=5), exclude_code="REQ2")
    assert [r.request_code for r in near] == ["REQ1"]
    # 30 minutes later is clear.
    far = await svc.collision_window(base + timedelta(minutes=30))
    assert far == []
    # Excluding the same request drops it.
    same = await svc.collision_window(base + timedelta(minutes=1), exclude_code="REQ1")
    assert same == []


async def test_sweep_due_publishes_past_due_only(sessionmaker, monkeypatch):
    published: list[str] = []

    class _FakePublisher:
        def __init__(self, _c):
            pass

        async def publish(self, code, *, caption_override=None, silent=False):
            published.append(code)

    monkeypatch.setattr(
        "nekofetch.services.publishing_service.PublishingService", _FakePublisher,
    )
    # complete_task is bookkeeping only; stub it so no assignment row is needed.
    monkeypatch.setattr(
        AdminAssignmentEngine, "complete_task",
        lambda self, code, stage, _session=None: _noop(),
    )

    svc = ScheduleService(_container(sessionmaker))
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await svc.schedule("PAST", 1, past, anime_title="Past")
    await svc.schedule("FUTURE", 1, future, anime_title="Future")

    res = await svc.sweep_due()
    assert res.fired == 1 and res.published == 1 and res.failed == 0
    assert published == ["PAST"]

    # Idempotent: a second sweep fires nothing (row is no longer pending).
    res2 = await svc.sweep_due()
    assert res2.fired == 0
    assert await svc.list_pending()  # the future row is still pending


async def test_sweep_due_records_failure_without_wedging(sessionmaker, monkeypatch):
    class _BoomPublisher:
        def __init__(self, _c):
            pass

        async def publish(self, code, *, caption_override=None, silent=False):
            raise RuntimeError("channel gone")

    monkeypatch.setattr(
        "nekofetch.services.publishing_service.PublishingService", _BoomPublisher,
    )
    svc = ScheduleService(_container(sessionmaker))
    await svc.schedule("BAD", 1, datetime.now(timezone.utc) - timedelta(minutes=1))
    res = await svc.sweep_due()
    assert res.fired == 1 and res.failed == 1 and res.published == 0
    # Row is marked failed with the error recorded, not left pending.
    from sqlalchemy import select
    async with sessionmaker() as s:
        row = (await s.execute(select(ScheduledPost).where(ScheduledPost.request_code == "BAD"))).scalar_one()
        assert row.status == "failed"
        assert "channel gone" in (row.error or "")


async def test_cancel_for_request_cancels_all_pending(sessionmaker):
    """After a force publish, every pending schedule for the request is
    cancelled so the set time can't double-post later (Phase 11)."""
    from datetime import datetime, timedelta, timezone

    svc = ScheduleService(_container(sessionmaker))
    base = datetime.now(timezone.utc) + timedelta(hours=4)
    await svc.schedule("REQ-X", 1, base, anime_title="X")
    await svc.schedule("REQ-Y", 1, base + timedelta(minutes=30), anime_title="Y")

    # Seed a second pending row for REQ-X directly (schedule() supersedes, but
    # a real duplicate can only exist from older rows — cancel must catch ALL).
    async with sessionmaker() as s:
        s.add(ScheduledPost(
            request_code="REQ-X", admin_telegram_id=1,
            scheduled_at=base + timedelta(hours=1), status="pending",
        ))
        await s.commit()

    cancelled = await svc.cancel_for_request("REQ-X")
    assert cancelled == 2
    pending = await svc.list_pending()
    codes = [r.request_code for r in pending]
    # REQ-X fully gone; other tests' rows (shared engine) and REQ-Y remain.
    assert "REQ-X" not in codes
    assert "REQ-Y" in codes

    # Idempotent: nothing left to cancel the second time.
    assert await svc.cancel_for_request("REQ-X") == 0


async def _noop():
    return None
