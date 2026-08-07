"""Coverage for Gojo's Phase-6 maintenance additions (workstream E).

Three pieces the plan added on top of the existing manual /updates + /bancheck:

  • ``_flatten_update_rows`` — turns detect-only ``CheckResult``s into the
    FSM-storable review rows shared by the manual flow and the scheduled notify.
  • ``make_monthly_update_notify_job`` — detect-only sweep that DMs the Gojo
    admins the reviewable list (never auto-creates); a no-op when nothing's new.
  • ``make_monthly_bancheck_job`` — probes channels, auto-recovers down
    distribution channels, DMs a summary.

The jobs are exercised through fakes (no Telegram / DB), asserting the
create=False contract and that a missing client / empty result never raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kurosoden.bots.gojo.handlers.tasks import (
    _flatten_update_rows,
    make_monthly_bancheck_job,
    make_monthly_update_notify_job,
)

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, doc, title, entries):
        self.anime_doc_id = doc
        self.title = title
        self.new_entries = entries


class _Entry:
    def __init__(self, aid, fmt, title, season, eps, rel=""):
        self.anilist_id = aid
        self.format = fmt
        self.english_title = title
        self.season_number = season
        self.episode_count = eps
        self.relation = rel


async def test_flatten_update_rows_shape():
    results = [
        _Result("doc1", "Attack on Titan", [
            _Entry(1, "TV", "AoT S2", 2, 12, "SEQUEL"),
            _Entry(2, "MOVIE", "AoT Movie", None, 1),
        ]),
        _Result("doc2", "Naruto", []),  # no new entries → contributes nothing
    ]
    rows = _flatten_update_rows(results)
    assert len(rows) == 2
    assert rows[0] == {
        "doc": "doc1", "title": "Attack on Titan", "aid": 1, "fmt": "TV",
        "t": "AoT S2", "season": 2, "eps": 12, "rel": "SEQUEL",
    }
    assert rows[1]["fmt"] == "MOVIE" and rows[1]["season"] is None


# ── monthly update notify ───────────────────────────────────────────────────────

class _FakeFSMRedis:
    """Minimal async redis stub so FSM.set/get work in-memory."""

    def __init__(self):
        self.store: dict = {}

    async def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = val
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeGojo:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=1, edit_text=self._edit)

    async def _edit(self, *a, **k):
        return None


def _container(monkeypatch, *, results, gojo, admin_ids=(555,)):
    redis = _FakeFSMRedis()
    mgr = SimpleNamespace(gojo=gojo)
    container = SimpleNamespace(
        redis=redis,
        pipeline_manager=mgr,
        pg_sessionmaker=None,
        env=SimpleNamespace(admin_ids=list(admin_ids)),
    )

    async def fake_scan(self):
        return results

    import nekofetch.services.maintenance_service as ms
    monkeypatch.setattr(ms.MaintenanceService, "scan_updates", fake_scan)

    # No admin pool → the configured owner is the human recovery recipient.
    async def fake_list_admins(self, *, stage=None, _session=None):
        return []

    import kurosoden.shared.management_service as mgmt
    monkeypatch.setattr(mgmt.ManagementService, "list_admins", fake_list_admins)
    return container


async def test_update_notify_dms_reviewable_list(monkeypatch):
    gojo = _FakeGojo()
    results = [_Result("doc1", "AoT", [_Entry(1, "TV", "AoT S2", 2, 12)])]
    container = _container(monkeypatch, results=results, gojo=gojo)

    await make_monthly_update_notify_job(container)()

    # DMed the fallback env admin, and armed their review FSM state.
    assert len(gojo.sent) == 1 and gojo.sent[0][0] == 555
    from nekofetch.bots.fsm import FSM
    state, data = await FSM(container.redis, bot="gojo").get(555)
    assert state == "gojo:await_updates_review"
    assert data["rows"][0]["t"] == "AoT S2"


async def test_update_notify_noop_when_nothing_new(monkeypatch):
    gojo = _FakeGojo()
    container = _container(monkeypatch, results=[], gojo=gojo)
    await make_monthly_update_notify_job(container)()
    assert gojo.sent == []


async def test_update_notify_survives_missing_client(monkeypatch):
    results = [_Result("doc1", "AoT", [_Entry(1, "TV", "AoT S2", 2, 12)])]
    container = _container(monkeypatch, results=results, gojo=None)
    # Must not raise even though the gojo client isn't up.
    await make_monthly_update_notify_job(container)()


# ── monthly ban check ───────────────────────────────────────────────────────────

async def test_bancheck_job_recovers_and_dms(monkeypatch):
    gojo = _FakeGojo()
    from nekofetch.services.maintenance_service import BanCheckResult, ChannelProbe

    probe = ChannelProbe(anime_doc_id="doc1", chat_id=-100, name="AoT Ch",
                         reachable=False, error="CHANNEL_INVALID")
    result = BanCheckResult(checked=3, banned=[probe])

    async def fake_probe(self):
        return result

    import nekofetch.services.maintenance_service as ms
    monkeypatch.setattr(ms.MaintenanceService, "probe_channels", fake_probe)

    recovered: list[str] = []

    async def fake_recreate(self, anime_doc_id):
        recovered.append(anime_doc_id)
        return SimpleNamespace(username="aot_new", name="AoT")

    import nekofetch.services.bot_orchestrator as bo
    monkeypatch.setattr(bo.BotOrchestratorService, "recreate_bot", fake_recreate)

    container = SimpleNamespace(
        redis=_FakeFSMRedis(),
        pipeline_manager=SimpleNamespace(gojo=gojo),
        pg_sessionmaker=None,
        env=SimpleNamespace(admin_ids=[555]),
    )

    async def fake_list_admins(self, *, stage=None, _session=None):
        return []
    import kurosoden.shared.management_service as mgmt
    monkeypatch.setattr(mgmt.ManagementService, "list_admins", fake_list_admins)

    # Capture the real recovery card without needing Telegram media methods.
    import kurosoden.bots.gojo.handlers.tasks as tasks

    async def capture_screen(client, chat_id, screen, **kwargs):
        client.sent.append((chat_id, screen.caption))

    monkeypatch.setattr(tasks, "send_screen", capture_screen)
    await make_monthly_bancheck_job(container)()

    assert recovered == []                 # human handoff takes priority
    from nekofetch.bots.fsm import FSM
    state, data = await FSM(container.redis, bot="gojo").get(555)
    assert state == "gojo:await_recovery_channel"
    assert data["anime_doc_id"] == "doc1"
    assert len(gojo.sent) == 2              # recovery card + monthly summary
    assert gojo.sent[-1][0] == 555


async def test_bancheck_job_noop_when_clear(monkeypatch):
    gojo = _FakeGojo()
    from nekofetch.services.maintenance_service import BanCheckResult

    async def fake_probe(self):
        return BanCheckResult(checked=5, banned=[])
    import nekofetch.services.maintenance_service as ms
    monkeypatch.setattr(ms.MaintenanceService, "probe_channels", fake_probe)

    container = SimpleNamespace(
        redis=_FakeFSMRedis(),
        pipeline_manager=SimpleNamespace(gojo=gojo),
        pg_sessionmaker=None,
        env=SimpleNamespace(admin_ids=[555]),
    )
    await make_monthly_bancheck_job(container)()
    assert gojo.sent == []  # nothing down → no DM
