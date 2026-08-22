"""Telegram Mini App initData validation (nekofetch.web.miniapp_auth)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from nekofetch.web.miniapp_auth import MiniAppAuthError, verify_init_data

_BOT_TOKEN = "123456:ABCDEF_test_token"


def _sign(fields: dict, token: str = _BOT_TOKEN) -> str:
    """Build a correctly-signed initData query string (mirrors Telegram)."""
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _fields(uid: int = 6161189904, auth_date: int | None = None) -> dict:
    return {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAF",
        "user": json.dumps({"id": uid, "first_name": "Rai", "username": "rai"}),
    }


def test_valid_init_data_returns_user():
    init = _sign(_fields(uid=42))
    user = verify_init_data(init, _BOT_TOKEN)
    assert user["id"] == 42
    assert user["username"] == "rai"


def test_tampered_hash_is_rejected():
    init = _sign(_fields()) + "0"  # corrupt the trailing hash
    with pytest.raises(MiniAppAuthError):
        verify_init_data(init, _BOT_TOKEN)


def test_wrong_bot_token_is_rejected():
    init = _sign(_fields(), token="999:OTHER")
    with pytest.raises(MiniAppAuthError, match="signature"):
        verify_init_data(init, _BOT_TOKEN)


def test_tampered_user_field_is_rejected():
    # Re-signing is impossible without the token; changing user after signing
    # breaks the hash.
    init = _sign(_fields(uid=1))
    tampered = init.replace("%22id%22%3A+1", "%22id%22%3A+999")
    with pytest.raises(MiniAppAuthError):
        verify_init_data(tampered, _BOT_TOKEN)


def test_expired_auth_date_is_rejected():
    init = _sign(_fields(auth_date=int(time.time()) - 48 * 3600))
    with pytest.raises(MiniAppAuthError, match="expired"):
        verify_init_data(init, _BOT_TOKEN, max_age_seconds=24 * 3600)


def test_freshness_check_can_be_disabled():
    init = _sign(_fields(auth_date=1))  # ancient, but max_age=0 skips the check
    assert verify_init_data(init, _BOT_TOKEN, max_age_seconds=0)["id"]


def test_missing_hash_or_token_raises():
    with pytest.raises(MiniAppAuthError):
        verify_init_data("auth_date=1&user=%7B%7D", _BOT_TOKEN)
    with pytest.raises(MiniAppAuthError):
        verify_init_data(_sign(_fields()), "")
