"""Userbot infrastructure — a pool of Telegram **user** sessions.

Many actions we need (reading another bot's history, joining/requesting private
channels, and future automation like creating/renaming bots) require a *user*
account, not a bot account. This module manages a pool of user sessions: it
selects whichever account is available and gracefully falls back to another if
one cannot log in or hits a limitation (flood-wait, auth failure, ban).

Initially one account is configured; the architecture takes an arbitrary list.

NOTE: a user session must be created once interactively (phone + code, producing
a ``session_string``); thereafter the pool starts non-interactively. Session
creation is therefore an out-of-band setup step, not part of normal runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from nekofetch.core.logging import get_logger

if TYPE_CHECKING:
    from pyrogram import Client

log = get_logger(__name__)

T = TypeVar("T")


def is_transport_error(exc: BaseException) -> bool:
    """Return whether an error indicates a dead Telegram transport/handler."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    text = str(exc).casefold()
    return any(marker in text for marker in (
        "tcptransport closed",
        "transport closed",
        "handler is closed",
        "connection reset",
        "connection aborted",
        "broken pipe",
    ))


@dataclass
class Account:
    """One user account. Prefer ``session_string`` (portable); a file session
    under ``workdir`` also works once created."""
    name: str
    session_string: str | None = None
    phone: str | None = None


class UserbotPool:
    """Holds user accounts and hands out a working, started client.

    The pool is lazy: clients are only started on first use. The first account
    that starts successfully becomes ``active``; failures roll over to the next.
    """

    def __init__(self, api_id: int, api_hash: str, accounts: list[Account],
                 workdir: str = "sessions") -> None:
        if not accounts:
            raise ValueError("UserbotPool requires at least one account")
        self.api_id = api_id
        self.api_hash = api_hash
        self.accounts = accounts
        self.workdir = workdir
        self._clients: dict[str, Client] = {}
        self._active: Client | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, api_id: int, api_hash: str, workdir: str = "sessions") -> UserbotPool:
        """Load accounts from one of (in priority order):

        1. ``TELEGRAM_USERBOT_ACCOUNTS_FILE`` — path to a JSON file containing an
           array of ``{"name", "session_string"}`` objects.
        2. ``TELEGRAM_USERBOT_ACCOUNTS`` — inline JSON array (single-line in .env).
        3. ``TELEGRAM_USERBOT_SESSION`` — single session string.

        ``.env`` is loaded first: pydantic-settings reads ``.env`` into the config
        model but NOT into ``os.environ``, so without this the session string is
        invisible here and the pool would fall back to an interactive login."""
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:  # noqa: BLE001 - dotenv optional; real env vars still work
            pass

        accounts: list[Account] = []

        # 1. JSON file (best for multi-line / complex session data)
        file_path = os.getenv("TELEGRAM_USERBOT_ACCOUNTS_FILE")
        if file_path:
            try:
                with open(file_path, encoding="utf-8") as f:
                    entries = json.load(f)
                for entry in entries:
                    accounts.append(Account(
                        name=entry.get("name", "account"),
                        session_string=entry.get("session_string"),
                        phone=entry.get("phone"),
                    ))
                log.info("userbot.accounts.loaded", file=file_path, count=len(accounts))
                return cls(api_id, api_hash, accounts, workdir)
            except Exception as exc:
                log.warning("userbot.accounts.file_failed", file=file_path, error=str(exc))

        # 2. Inline JSON env var (single-line in .env)
        raw = os.getenv("TELEGRAM_USERBOT_ACCOUNTS")
        if raw:
            for entry in json.loads(raw):
                accounts.append(Account(name=entry["name"],
                                        session_string=entry.get("session_string"),
                                        phone=entry.get("phone")))
        elif os.getenv("TELEGRAM_USERBOT_SESSION"):
            # 3. Single session string
            accounts.append(Account(name="primary",
                                    session_string=os.getenv("TELEGRAM_USERBOT_SESSION")))
        else:
            accounts.append(Account(name="primary"))  # file session in workdir
        return cls(api_id, api_hash, accounts, workdir)

    def _build(self, acc: Account) -> Client:
        from pyrogram import Client
        kwargs: dict[str, Any] = {
            "api_id": self.api_id, "api_hash": self.api_hash, "workdir": self.workdir,
        }
        if acc.session_string:
            kwargs["session_string"] = acc.session_string
        if acc.phone:
            kwargs["phone_number"] = acc.phone
        return Client(acc.name, **kwargs)

    async def acquire(self) -> Client:
        """Return a started, responsive client, trying each account until one works.

        ``is_connected`` can remain true briefly after Pyrogram's underlying
        handler/transport has closed. Probe the cached active client before
        returning it; otherwise one stale session can poison every caller with
        repeated ``TCPTransport closed`` errors.
        """
        async with self._lock:
            retired_names: set[str] = set()
            active = self._active
            if active is not None and active.is_connected:
                try:
                    await active.get_me()
                    return active
                except Exception as exc:  # noqa: BLE001 — stale transport/session
                    log.warning("userbot.active_unhealthy", error=str(exc))
                    if len(self.accounts) > 1:
                        for account in self.accounts:
                            if self._clients.get(account.name) is active:
                                retired_names.add(account.name)
                                break
                    await self._retire(active)
            errors: list[str] = []
            for acc in self.accounts:
                if acc.name in retired_names:
                    continue
                client = self._clients.get(acc.name) or self._build(acc)
                self._clients[acc.name] = client
                try:
                    if not client.is_connected:
                        await client.start()
                    me = await client.get_me()
                    log.info("userbot.active", account=acc.name, user_id=me.id)
                    self._active = client
                    return client
                except Exception as exc:  # noqa: BLE001 - try the next account
                    errors.append(f"{acc.name}: {exc}")
                    log.warning("userbot.account.failed", account=acc.name, error=str(exc))
                    retired_names.add(acc.name)
                    await self._retire(client)
            raise RuntimeError(f"no usable userbot account: {errors}")

    async def execute(
        self,
        fn: Callable[[Client], Awaitable[T]],
        *,
        retries: int = 1,
        retry_on: Callable[[BaseException], bool] | None = None,
        max_attempts: int | None = None,
    ) -> T:
        """Run ``fn`` with a working client; on failure, fall back to another
        account and retry (handles flood-wait / session death mid-operation).

        ``max_attempts`` hard-caps the TOTAL number of tries regardless of how
        many accounts are configured (e.g. @acutebot is capped at 3 so a title
        that simply isn't there doesn't hammer the bot across every account)."""
        last: Exception | None = None
        attempts = len(self.accounts) * max(1, retries)
        if max_attempts is not None:
            attempts = min(attempts, max(1, max_attempts))
        for _ in range(attempts):
            client = await self.acquire()
            try:
                return await fn(client)
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("userbot.execute.failed", error=str(exc))
                if retry_on is not None and not retry_on(exc):
                    raise
                # drop the active client so acquire() rolls to the next account
                await self._retire(client)
        raise RuntimeError(f"userbot.execute exhausted all accounts: {last}")

    async def execute_on(
        self, account_name: str, fn: Callable[[Client], Awaitable[T]],
    ) -> T:
        """Run ``fn`` on a *specific* account by name (no fallback to others).

        The quota picker chooses which session should own a new channel, so the
        creation must run on exactly that account — falling back to another would
        create the channel under the wrong session and corrupt the slot tally.
        Raises if that account isn't configured or can't start."""
        acc = next((a for a in self.accounts if a.name == account_name), None)
        if acc is None:
            raise RuntimeError(f"userbot account not found: {account_name}")
        async with self._lock:
            client = self._clients.get(acc.name) or self._build(acc)
            self._clients[acc.name] = client
            if not client.is_connected:
                await client.start()
            await client.get_me()
        return await fn(client)

    async def _retire(self, client: Client) -> None:
        if self._active is client:
            self._active = None
        for name, c in list(self._clients.items()):
            if c is client:
                try:
                    await c.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._clients.pop(name, None)

    async def close(self) -> None:
        for c in self._clients.values():
            try:
                if c.is_connected:
                    await c.stop()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._active = None
