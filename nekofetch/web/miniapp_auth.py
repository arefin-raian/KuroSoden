"""Telegram Mini App auth — validate ``initData`` (HMAC), no network.

The mapping editor is a Telegram Web App opened from a bot button. Telegram signs
the launch parameters (``initData``) so the server can trust *who* opened it
without a session cookie. This module implements the documented validation
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
as a pure function so it is fully unit-testable offline.

Validation:
  secret_key = HMAC_SHA256(key="WebAppData", msg=<bot_token>)
  computed   = HMAC_SHA256(key=secret_key, msg=<data_check_string>)
  valid  ⇔  hex(computed) == hash  AND  auth_date is fresh

``data_check_string`` = every ``initData`` field EXCEPT ``hash``, sorted by key,
joined as ``k=v`` with ``\n``. Returns the parsed ``user`` dict on success so the
caller can gate on staff membership; raises ``MiniAppAuthError`` otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class MiniAppAuthError(Exception):
    """initData failed validation (bad signature, stale, or malformed)."""


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    """Sorted ``k=v`` lines for every field except ``hash`` (Telegram spec)."""
    return "\n".join(
        f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]) if k != "hash"
    )


def verify_init_data(
    init_data: str, bot_token: str, *, max_age_seconds: int = 24 * 3600,
) -> dict:
    """Validate a Mini App ``initData`` string; return its parsed ``user`` dict.

    ``max_age_seconds`` bounds replay via the signed ``auth_date`` (0 disables the
    freshness check — used only in tests). Raises :class:`MiniAppAuthError` on any
    failure. The returned dict is the decoded ``user`` field (``{"id", ...}``)."""
    if not init_data or not bot_token:
        raise MiniAppAuthError("missing initData or bot token")
    # keep_blank_values so an empty field still contributes to the check string.
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = dict(pairs)
    their_hash = fields.get("hash")
    if not their_hash:
        raise MiniAppAuthError("no hash in initData")

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(
        secret_key, _data_check_string(pairs).encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, their_hash):
        raise MiniAppAuthError("bad initData signature")

    if max_age_seconds:
        try:
            auth_date = int(fields.get("auth_date", "0"))
        except ValueError:
            raise MiniAppAuthError("bad auth_date")
        if auth_date <= 0 or (time.time() - auth_date) > max_age_seconds:
            raise MiniAppAuthError("initData expired")

    try:
        user = json.loads(fields.get("user", "") or "{}")
    except (TypeError, ValueError):
        raise MiniAppAuthError("bad user field")
    if not isinstance(user, dict) or not user.get("id"):
        raise MiniAppAuthError("no user id in initData")
    return user
