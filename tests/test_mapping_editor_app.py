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
        pg_sessionmaker=None,
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
    assert {s["season_number"] for s in body["seasons"]} == {1, 2}


def test_post_save_ddl_releases_gate(client_and_container):
    client, container = client_and_container
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    # Move everything into S1 in a custom order; exclude nothing.
    layout = {"files": [
        {"index": 3, "season": 1, "position": 1},
        {"index": 0, "season": 1, "position": 2},
        {"index": 1, "season": 1, "position": 3},
        {"index": 2, "season": 1, "position": 4},
    ], "excluded": []}
    r = client.post(f"/api/map/{token}",
                    json={"init_data": _sign(1), "layout": layout})
    assert r.status_code == 200 and r.json()["ok"] is True

    # The DDL gate was released: value set to the "use it" sentinel, await flag
    # cleared, and the rebuilt mapping written into ddlmap_data.
    from nekofetch.services.naming_confirm import (
        value_key, await_key, ddlmap_data_key, _USE_DEFAULT,
    )
    d = container.redis.d
    assert d.get(value_key(7, "ddlmap")) == _USE_DEFAULT
    assert await_key(7, "ddlmap") not in d
    saved = json.loads(d[ddlmap_data_key(7)])
    m = TorrentMapping.from_dict(saved["mapping"])
    s1 = next(e for e in m.entries if e.franchise_entry.season_number == 1)
    assert s1.actual == 4          # all four files landed in S1 per the layout
    # Session consumed.
    assert not any(k.startswith("nf:mapedit:") for k in d)


def test_post_rejects_bad_initdata(client_and_container):
    client, container = client_and_container
    token = _seed(container, {"kind": "ddlmap", "job_id": 7})
    r = client.post(f"/api/map/{token}",
                    json={"init_data": _sign(1, token="999:WRONG"), "layout": {}})
    assert r.status_code == 401
