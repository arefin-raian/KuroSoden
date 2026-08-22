"""Vercel serverless entrypoint for the mapping editor Mini App.

Vercel loads the module-level ``app`` (an ASGI FastAPI instance) named by
``[tool.vercel] entrypoint`` in ``pyproject.toml``. It builds a *slim* container
from environment variables — Redis + the two bot tokens + the admin/owner id
whitelist — so the function NEVER imports the bot stack (Pyrogram / SQLAlchemy /
Pillow). That keeps the serverless bundle tiny and cold starts fast.

Why this stays light: ``build_app`` and the whole editor request path
(mapping_session → torrent_mapping → franchise_flow, miniapp_auth, redis_safe)
only import ``fastapi``, ``redis``, ``structlog`` and the stdlib. The one heavy
import — ``nekofetch.core.container`` — is ``TYPE_CHECKING``-guarded in the
services it annotates, so it is never pulled at runtime here.

Auth is env-whitelist based (see :func:`nekofetch.web.app._authenticate`): the
owner/admin ids in ``ADMIN_IDS``/``OWNER_ID`` are authorized directly — matching
the bots' rule that the env whitelist always wins. DB-backed STAFF is only
consulted when a ``pg_sessionmaker`` is present, which it deliberately is NOT
here (``pg_sessionmaker=None``), so no database is required on Vercel.

Required environment variables (set in the Vercel project → Settings → Env):
  REDIS_URL            the SAME Redis the bots use (rediss:// for TLS). This is
                       mandatory and non-negotiable: the editor reads the session
                       the bot minted and writes the mapping the bot consumes, so
                       both must share one Redis instance.
  DOWNLOADER_BOT_TOKEN used to validate the Telegram initData HMAC (DDL editor).
  ADMIN_BOT_TOKEN      same, for the torrent editor.
  ADMIN_IDS            comma/space-separated Telegram ids allowed to use it.
  OWNER_ID             the owner's Telegram id (also allowed).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from redis.asyncio import Redis

from nekofetch.web.app import build_app


def _int_list(raw: str | None) -> list[int]:
    """Parse "111, 222 333" → [111, 222, 333]; skip non-numeric tokens."""
    out: list[int] = []
    for tok in (raw or "").replace(",", " ").split():
        try:
            out.append(int(tok.strip()))
        except ValueError:
            continue
    return out


_redis = Redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    decode_responses=True,
)

_env = SimpleNamespace(
    downloader_bot_token=os.environ.get("DOWNLOADER_BOT_TOKEN", ""),
    admin_bot_token=os.environ.get("ADMIN_BOT_TOKEN", ""),
    admin_ids=_int_list(os.environ.get("ADMIN_IDS")),
    owner_id=int(os.environ.get("OWNER_ID") or 0),
)

# pg_sessionmaker=None → the DB-backed STAFF branch is skipped; env whitelist only.
_container = SimpleNamespace(redis=_redis, env=_env, pg_sessionmaker=None)

app = build_app(_container)
