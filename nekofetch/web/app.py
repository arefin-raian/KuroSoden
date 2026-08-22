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


def _env_principal_ids(container) -> set[int]:
    """Owner + admin Telegram ids from env — the whitelist that ALWAYS wins.

    Mirrors :class:`AuthService` (whose docstring states "the .env admin whitelist
    always wins"). Consulting it here lets the editor authorize the owner/admins
    WITHOUT a database — the deployment mode used on Vercel, where the function is
    intentionally DB-less (``pg_sessionmaker is None``) to stay a tiny bundle."""
    env = container.env
    ids: set[int] = set()
    for aid in (getattr(env, "admin_ids", None) or []):
        try:
            ids.add(int(aid))
        except (TypeError, ValueError):
            continue
    try:
        owner = int(getattr(env, "owner_id", 0) or 0)
    except (TypeError, ValueError):
        owner = 0
    if owner:
        ids.add(owner)
    return ids


async def _authenticate(container, init_data: str) -> dict:
    """Validate initData against any staff bot token → return the user dict, or
    raise MiniAppAuthError. Then require the user to be staff.

    Staff gate, in order:
      1. The env owner/admin whitelist always wins — no DB round-trip (and the
         only path available on the DB-less Vercel deployment).
      2. DB-backed STAFF check via :class:`AuthService`, consulted ONLY when a
         database is wired (``pg_sessionmaker`` present) — i.e. the full bot
         deployment, where DB-stored STAFF (not just env admins) may use it.
    """
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

    tid = int(user["id"])
    # 1) Env whitelist — authoritative, DB-free.
    if tid in _env_principal_ids(container):
        return user
    # 2) DB-backed STAFF, only when a database is actually configured.
    if getattr(container, "pg_sessionmaker", None) is not None:
        from nekofetch.services.auth_service import AuthService
        from nekofetch.domain.enums import Role
        nf_user = await AuthService(container).resolve_user(tid)
        try:
            if Role(getattr(nf_user, "role", None)) in (Role.STAFF, Role.ADMIN):
                return user
        except ValueError:
            pass
    raise MiniAppAuthError("not staff")


async def _commit_and_release(container, sess, mapping_dict: dict) -> None:
    """Persist the rebuilt mapping + release the flow this session belongs to."""
    rel = sess.release or {}
    kind = rel.get("kind")
    redis = container.redis
    if kind == "ddlmap":
        # The DDL mapping gate PARKS the job (status PAUSED) — the worker has
        # already returned, so there's no block-poll to release. Saving the editor
        # RESUMES it: persist the rebuilt mapping onto the request + re-enqueue
        # (code-keyed, so the DDL cache is reused and nothing re-downloads).
        from nekofetch.services.naming_confirm import resume_parked_ddl_mapping
        job_id = int(rel["job_id"])
        await resume_parked_ddl_mapping(container, job_id, mapping_dict)
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
