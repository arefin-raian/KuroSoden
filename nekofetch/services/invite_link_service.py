"""Bot-minted private invite links for distribution channels.

The public catalog surfaces (main-channel Download button, index hyperlink)
deliberately point at a **private** invite link (``t.me/+…``) rather than the
channel's public ``t.me/<username>`` link, so all inbound traffic flows through a
link we own and can revoke/replace. The channel's creating userbot is its owner,
so it can mint links via ``create_chat_invite_link``.

Lifecycle:
  • On channel create, :func:`ensure_for_bot` mints a link and stores it on the
    ``bots`` row (:attr:`DistributionBot.invite_link`).
  • On recreate, the old channel — and its link — is gone; :func:`mint_for_channel`
    is called fresh and the new link is swapped into the main-channel Download
    button and the index hyperlink by the recovery path.

Bots (not channels) never get an invite link — they use a ``?start`` deep link —
so every method is a graceful no-op for them.
"""

from __future__ import annotations

from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import DistributionBot
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)


class InviteLinkService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._pool = None

    def _userbot(self):
        if self._pool is None:
            from nekofetch.sources.telegram.userbot import UserbotPool

            self._pool = UserbotPool.from_env(
                self._c.env.telegram_api_id, self._c.env.telegram_api_hash,
                str(self._c.env.session_path),
            )
        return self._pool

    def _bot_client(self):
        """The Gojo (publisher) client, else the NekoFetch admin client.

        A BOT that is an admin of the channel can mint invite links itself via
        ``create_chat_invite_link`` — no userbot needed. Gojo is an admin of both
        the distribution channels and the index channel, so it's the preferred
        minter; the NekoFetch admin client is the fallback."""
        pm = getattr(self._c, "pipeline_manager", None)
        gojo = getattr(pm, "gojo", None) if pm else None
        return gojo or getattr(self._c, "admin_client", None)

    async def mint_for_chat(self, chat_id: int) -> str | None:
        """Mint a fresh private invite link for ANY chat we administer.

        Tries the Gojo BOT first (bots can create invite links where they're
        admin — no userbot dependency), then the userbot pool as a fallback.
        Returns the ``t.me/+…`` link or None (best-effort). Used for both the
        distribution channel and the index channel."""
        bot = self._bot_client()
        if bot is not None:
            try:
                link = await bot.create_chat_invite_link(chat_id)
                url = getattr(link, "invite_link", None)
                if url:
                    log.info("invitelink.minted", chat_id=chat_id, via="bot")
                    return url
            except Exception as exc:  # noqa: BLE001 — fall back to userbot
                log.warning("invitelink.mint.bot_failed",
                            chat_id=chat_id, error=str(exc))
        try:
            link = await self._userbot().execute(
                lambda c: c.create_chat_invite_link(chat_id)
            )
        except Exception as exc:  # noqa: BLE001 — link is best-effort
            log.warning("invitelink.mint.failed", chat_id=chat_id, error=str(exc))
            return None
        url = getattr(link, "invite_link", None)
        if not url:
            log.warning("invitelink.mint.empty", chat_id=chat_id)
            return None
        log.info("invitelink.minted", chat_id=chat_id, via="userbot")
        return url

    async def mint_for_channel(self, chat_id: int) -> str | None:
        """Mint a fresh private invite link for a distribution ``chat_id``.

        Bot-first (Gojo), userbot fallback — see :meth:`mint_for_chat`. Returns the
        ``t.me/+…`` link, or None if minting failed (best-effort — the caller falls
        back to the public username link)."""
        return await self.mint_for_chat(chat_id)

    async def ensure_for_bot(self, bot_id: str) -> str | None:
        """Ensure the distribution *channel* row ``bot_id`` has an invite link.

        Mints one if the row is a channel with a ``chat_id`` and no link yet, stores
        it, and returns it. Returns the existing link if already present, or None
        for bots / rows without a chat_id. Never raises.
        """
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(DistributionBot).where(DistributionBot.id == bot_id)
                )
            ).scalar_one_or_none()
            if row is None or not row.is_channel or not row.chat_id:
                return None
            if row.invite_link:
                return row.invite_link

        link = await self.mint_for_channel(row.chat_id)
        if link:
            await self.store(bot_id, link)
        return link

    async def store(self, bot_id: str, invite_link: str | None) -> None:
        """Persist ``invite_link`` on the ``bots`` row (durable across restarts)."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            row = (
                await session.execute(
                    select(DistributionBot).where(DistributionBot.id == bot_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.invite_link = invite_link
        log.info("invitelink.stored", bot_id=bot_id, present=bool(invite_link))
