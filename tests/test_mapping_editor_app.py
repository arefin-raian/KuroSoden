"""Mapping-editor FastAPI app: auth gate, GET payload, POST save→release.

Uses a fake container (in-memory redis + stubbed AuthService) so no DB/network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nekofetch.domain.enums import ContentKind  # noqa: E402
from nekofetch.services.franchise_flow import FranchiseMapping, MappingEntry  # noqa: E402
from nekofetch.services.torrent_mapping import (  # noqa: E402
    TorrentMapping, build_torrent_mapping,
)
from nekofetch.web import app as webapp  # noqa: E402
from nekofetch.web.mapping_session import MappingSession, _session_key  # noqa: E402

_TOKEN = "111:DL_token"


class _Redis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return False
        self.d[k] = v
        return True

    async def delete(self, k):
        self.d.pop(k, None)


def _container():
    return SimpleNamespace(
        redis=_Redis(),
        env=SimpleNamespace(downloader_bot_token=_TOKEN, admin_bot_token="222:ADM"),
        # Truthy sentinel → the DB-backed STAFF branch runs (and hits the stubbed
        # AuthService below). The commit paths never touch it. Env whitelist is
        # empty here, so these tests exercise the DB path; the env-whitelist path
        # is covered by test_env_whitelist_authorizes_without_db.
        pg_sessionmaker=object(),
    )


def _sign(uid: int, token: str = _TOKEN) -> str:
    fields = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": uid, "first_name": "R"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _working_set() -> dict:
    fr = FranchiseMapping(anime_doc_id="1", root_title="Show", entries=[
        MappingEntry(anilist_id=10, kind=ContentKind.SEASON, season_number=1,
                     season_part=None, title="", episodes=2, included=True),
        MappingEntry(anilist_id=20, kind=ContentKind.SEASON, season_number=2,
                     season_part=None, title="", episodes=2, included=True),
    ])
    of = []
    i = 0
    for s in (1, 2):
        for e in (1, 2):
            of.append({"index": i, "name": f"Show S{s:02d}E{e:02d}", "season": s,
                       "episode": e, "kind": "episode", "season_explicit": True,
                       "seq": i + 1, "resolutions": ["1080p"]})
            i += 1
    m = build_torrent_mapping(of, fr)
    return {"mapping": m.to_dict(), "ordered_files": of, "episode_titles": {},
            "title": "Show"}


@pytest.fixture
def client_and_container(monkeypatch):
    container = _container()

    # Stub the staff gate: any resolved user is ADMIN.
    from nekofetch.domain.enums import Role
    import nekofetch.services.auth_service as auth_mod

    class _FakeAuth:
        def __init__(self, _c):
            pass

        async def resolve_user(self, tid, **kw):
            return SimpleNamespace(role=Role.ADMIN.value)

    monkeypatch.setattr(auth_mod, "AuthService", _FakeAuth)
    app = webapp.build_app(container)
    return TestClient(app), container


def _seed(container, release: dict, token: str = "tok-test") -> str:
    """Seed the fake redis directly (sync) with a session for ``token``."""
    sess = MappingSession(token=token, working_set=_working_set(), release=release)
    container.redis.d[_session_key(token)] = sess.to_json()
    return token


def test_get_requires_valid_initdata(client_and_container):
    client, container = client_and_container
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    # No initData → 401.
    r = client.get(f"/api/map/{token}", headers={"X-Init-Data": ""})
    assert r.status_code == 401
    # Valid initData → payload with files + seasons.
    r = client.get(f"/api/map/{token}", headers={"X-Init-Data": _sign(1)})
    assert r.status_code == 200
    body = r.json()
    assert len(body["files"]) == 4
    # Payload now exposes a section per franchise entry (seasons here).
    assert {s["season_number"] for s in body["sections"]
            if s["kind"] == "episode"} == {1, 2}


def test_post_save_ddl_resumes_parked_job(client_and_container, monkeypatch):
    client, container = client_and_container
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    # Capture the resume call (real resume needs a DB; here we assert it's invoked
    # with the rebuilt mapping — all four files moved into S1).
    calls = {}
    import nekofetch.web.app as appmod

    async def _fake_resume(cont, job_id, mapping_dict):
        calls["job_id"] = job_id
        calls["mapping"] = mapping_dict
        return True

    monkeypatch.setattr(appmod, "resume_parked_ddl_mapping", _fake_resume, raising=False)
    # app.py imports it lazily inside _commit_and_release; patch the source too.
    import nekofetch.services.naming_confirm as ncmod
    monkeypatch.setattr(ncmod, "resume_parked_ddl_mapping", _fake_resume)

    layout = {"files": [
        {"index": 3, "season": 1, "position": 1},
        {"index": 0, "season": 1, "position": 2},
        {"index": 1, "season": 1, "position": 3},
        {"index": 2, "season": 1, "position": 4},
    ], "excluded": []}
    r = client.post(f"/api/map/{token}",
                    json={"init_data": _sign(1), "layout": layout})
    assert r.status_code == 200 and r.json()["ok"] is True

    # The parked job was resumed with the rebuilt mapping (four files in S1).
    assert calls["job_id"] == 7
    m = TorrentMapping.from_dict(calls["mapping"])
    s1 = next(e for e in m.entries if e.franchise_entry.season_number == 1)
    assert s1.actual == 4
    # Session consumed.
    assert not any(k.startswith("nf:mapedit:") for k in container.redis.d)


def test_post_rejects_bad_initdata(client_and_container):
    client, container = client_and_container
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    r = client.post(f"/api/map/{token}",
                    json={"init_data": _sign(1, token="999:WRONG"), "layout": {}})
    assert r.status_code == 401


def test_editor_page_is_served(client_and_container):
    client, _container = client_and_container
    r = client.get("/map/whatever-token")
    assert r.status_code == 200
    html = r.text
    # Key hooks the editor JS relies on + the Telegram Web App SDK.
    assert "telegram-web-app.js" in html
    assert 'id="save"' in html and 'id="root"' in html
    assert "/api/map/" in html            # it fetches the payload
    assert "buildLayout" in html          # the layout serializer exists


def test_healthz(client_and_container):
    client, _container = client_and_container
    assert client.get("/healthz").json() == {"ok": True}


def test_env_whitelist_authorizes_without_db(monkeypatch):
    """The Vercel deployment mode: no database (pg_sessionmaker=None), auth by the
    env owner/admin whitelist alone. AuthService must NOT be consulted."""
    import nekofetch.services.auth_service as auth_mod

    class _Boom:
        def __init__(self, _c):
            raise AssertionError("AuthService must not be constructed on the env path")

    monkeypatch.setattr(auth_mod, "AuthService", _Boom)

    uid = 424242
    container = SimpleNamespace(
        redis=_Redis(),
        env=SimpleNamespace(downloader_bot_token=_TOKEN, admin_bot_token="222:ADM",
                            admin_ids=[uid], owner_id=0),
        pg_sessionmaker=None,          # no DB — the Vercel case
    )
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    client = TestClient(webapp.build_app(container))

    # In the whitelist → authorized without touching AuthService.
    r = client.get(f"/api/map/{token}", headers={"X-Init-Data": _sign(uid)})
    assert r.status_code == 200
    # NOT in the whitelist + no DB → 401.
    r2 = client.get(f"/api/map/{token}", headers={"X-Init-Data": _sign(999999)})
    assert r2.status_code == 401


def test_post_save_torrent_writes_fsm_not_enqueue(client_and_container):
    client, container = client_and_container
    # Seed the admin FSM working set (as _show_torrent_mapping would) + a session.
    import json as _json
    from nekofetch.core.constants import REDIS_FSM
    from nekofetch.web.mapping_session import MappingSession, _session_key
    uid = 6161189904
    fsm_key = REDIS_FSM.format(bot="admin", user_id=uid)
    container.redis.d[fsm_key] = _json.dumps({
        "state": "staff:torrent_map",
        "data": {"code": "REQ-T1", "torrent_mapping": {"stale": True}},
    })
    token = "tok-torrent"
    container.redis.d[_session_key(token)] = MappingSession(
        token=token, working_set=_working_set(),
        release={"kind": "torrent", "code": "REQ-T1", "user_id": uid, "bot": "admin"},
    ).to_json()

    layout = {"files": [{"index": i, "season": 1, "position": i + 1}
                        for i in range(4)], "excluded": []}
    r = client.post(f"/api/map/{token}", json={"init_data": _sign(uid), "layout": layout})
    assert r.status_code == 200

    # The admin FSM's torrent_mapping was updated in place; the torrent commit
    # path uses FSM/redis only and never enqueues (Confirm stays the single
    # enqueue point), so pg_sessionmaker is irrelevant to it.
    fsm_data = _json.loads(container.redis.d[fsm_key])["data"]
    assert fsm_data["torrent_mapping"] != {"stale": True}
    m = TorrentMapping.from_dict(fsm_data["torrent_mapping"])
    assert sum(e.actual for e in m.entries) >= 1
