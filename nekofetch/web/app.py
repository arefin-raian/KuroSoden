"""FastAPI app for the mapping editor (Telegram Mini App).

Three routes:
  * ``GET  /map/{token}``       — serve the single-page editor (static HTML).
  * ``GET  /api/map/{token}``   — the editor payload (files + seasons) as JSON.
  * ``POST /api/map/{token}``   — validate initData, rebuild the mapping from the
                                  posted layout, commit it, and release the flow.

Every API request is authenticated by the Telegram ``initData`` the Web App sends
(HMAC-signed by the bot token) AND gated on staff membership; the token in the URL
scopes the request to one mapping session. Save commits differ per flow:
  * ``ddlmap`` — write the rebuilt mapping into the DDL worker's ``ddlmap_data``
    and release the confirm gate (the worker proceeds with it).
  * ``torrent`` — persist ``franchise_data["_torrent_mapping"]`` and enqueue, the
    same as the torrent card's Confirm.

The app is created with the live ``Container`` so it shares Redis/Postgres with
the bots; ``build_app`` is import-safe (FastAPI is only imported here).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nekofetch.core.logging import get_logger
from nekofetch.web.mapping_session import (
    apply_layout, build_editor_payload, delete_session, load_session,
)
from nekofetch.web.miniapp_auth import MiniAppAuthError, verify_init_data

log = get_logger(__name__)


def _staff_bot_tokens(container) -> list[str]:
    """The bot tokens whose Mini App initData we accept — the two bots that open
    a mapping card (Levi/downloader for DDL, admin for torrent). initData is signed
    by the OPENING bot, so we validate against each and accept the first match."""
    env = container.env
    tokens = [getattr(env, "downloader_bot_token", ""),
              getattr(env, "admin_bot_token", "")]
    return [t for t in tokens if t]


async def _authenticate(container, init_data: str) -> dict:
    """Validate initData against any staff bot token → return the user dict, or
    raise MiniAppAuthError. Then require the user to be staff (role check)."""
    user = None
    last_err: Exception | None = None
    for token in _staff_bot_tokens(container):
        try:
            user = verify_init_data(init_data, token)
            break
        except MiniAppAuthError as exc:
            last_err = exc
    if user is None:
        raise MiniAppAuthError(str(last_err) or "no bot token matched")

    # Staff gate: resolve the Telegram id and require STAFF/ADMIN.
    from nekofetch.services.auth_service import AuthService
    from nekofetch.domain.enums import Role
    nf_user = await AuthService(container).resolve_user(int(user["id"]))
    try:
        if Role(getattr(nf_user, "role", None)) not in (Role.STAFF, Role.ADMIN):
            raise MiniAppAuthError("not staff")
    except ValueError:
        raise MiniAppAuthError("unknown role")
    return user


async def _commit_and_release(container, sess, mapping_dict: dict) -> None:
    """Persist the rebuilt mapping + release the flow this session belongs to."""
    rel = sess.release or {}
    kind = rel.get("kind")
    redis = container.redis
    if kind == "ddlmap":
        # Write the corrected mapping into the worker's stash, then release the
        # gate with the "use it" sentinel — the worker reads mapping back from
        # ddlmap_data and proceeds. (Mirrors the text Fix flow's commit path.)
        import json as _json
        from nekofetch.services.naming_confirm import (
            ddlmap_data_key, value_key, await_key, _USE_DEFAULT,
        )
        from nekofetch.core.redis_safe import (
            safe_redis_get, safe_redis_set, safe_redis_delete,
        )
        job_id = int(rel["job_id"])
        raw = await safe_redis_get(redis, ddlmap_data_key(job_id),
                                   label="mapedit.ddl.read")
        data = _json.loads(raw) if raw else dict(sess.working_set)
        data["mapping"] = mapping_dict
        await safe_redis_set(redis, ddlmap_data_key(job_id), _json.dumps(data),
                             label="mapedit.ddl.write", ex=15 * 60)
        # Release: value first, THEN clear await flag (worker never sees an empty
        # value) — identical ordering to naming_confirm_handler._release.
        await safe_redis_set(redis, value_key(job_id, "ddlmap"), _USE_DEFAULT,
                             label="mapedit.ddl.release_value", ex=15 * 60)
        await safe_redis_delete(redis, await_key(job_id, "ddlmap"),
                                label="mapedit.ddl.release")
    elif kind == "torrent":
        # Write the rebuilt mapping back into the admin's FSM working set so the
        # torrent card's existing "Confirm" stays the SINGLE enqueue point (its
        # _torrent_map_confirm reads data["torrent_mapping"] from the FSM). We do
        # NOT enqueue here — that would double-enqueue against Confirm.
        from nekofetch.bots.fsm import FSM
        user_id = int(rel["user_id"])
        await FSM(redis, bot=rel.get("bot", "admin")).update(
            user_id, torrent_mapping=mapping_dict)
    else:
        raise ValueError(f"unknown release kind: {kind!r}")


def build_app(container) -> "Any":
    """Construct the FastAPI app bound to the live container. Imported lazily so
    a deployment without the editor never pays the FastAPI import."""
    app = FastAPI(title="Kuro Sōden — Mapping Editor", docs_url=None, redoc_url=None)
    _static = Path(__file__).resolve().parent / "static"

    @app.get("/healthz")
    async def _health():
        return {"ok": True}

    @app.get("/map/{token}", response_class=HTMLResponse)
    async def _editor_page(token: str):
        # The page itself carries no secrets; it fetches data via the API using
        # the initData the Telegram Web App injects. A bad token just renders an
        # empty editor that the API will 404.
        html = (_static / "editor.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/map/{token}")
    async def _get_map(token: str, request: Request):
        init_data = request.headers.get("X-Init-Data", "")
        try:
            await _authenticate(container, init_data)
        except MiniAppAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        sess = await load_session(container.redis, token)
        if sess is None:
            raise HTTPException(status_code=404, detail="session expired")
        return JSONResponse(build_editor_payload(sess))

    @app.post("/api/map/{token}")
    async def _save_map(token: str, request: Request):
        body = await request.json()
        init_data = (body or {}).get("init_data") or request.headers.get("X-Init-Data", "")
        try:
            await _authenticate(container, init_data)
        except MiniAppAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        sess = await load_session(container.redis, token)
        if sess is None:
            raise HTTPException(status_code=404, detail="session expired")
        layout = (body or {}).get("layout") or {}
        try:
            mapping_dict = apply_layout(sess.working_set, layout)
        except Exception as exc:  # noqa: BLE001 — bad layout → 400, not 500
            log.warning("mapedit.apply_failed", token=token[:6], error=str(exc))
            raise HTTPException(status_code=400, detail=f"bad layout: {exc}")
        try:
            await _commit_and_release(container, sess, mapping_dict)
        except Exception as exc:  # noqa: BLE001
            log.warning("mapedit.commit_failed", token=token[:6], error=str(exc))
            raise HTTPException(status_code=500, detail="commit failed")
        await delete_session(container.redis, token)
        log.info("mapedit.saved", token=token[:6], kind=(sess.release or {}).get("kind"))
        return {"ok": True}

    return app
