from __future__ import annotations

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)


class NotificationService:
    def __init__(self, container: Container) -> None:
        self._c = container

    @property
    def _client(self):
        """The bot that sends requester-facing DMs.

        MUST be a crew bot the user actually talks to — the requester made their
        request through Lelouch, so notifications ("published", "removed",
        "requeued", …) come from Lelouch. Using ``admin_client`` here was the bug
        that leaked DMs from the internal NekoFetch admin bot (which the owner
        should never hear from). Falls back to ``admin_client`` only if the
        pipeline/Lelouch client isn't up yet (e.g. early startup / standalone).
        """
        mgr = getattr(self._c, "pipeline_manager", None)
        lelouch = getattr(mgr, "lelouch", None) if mgr is not None else None
        return lelouch or getattr(self._c, "admin_client", None)

    async def _send(self, user_id: int, text: str) -> None:
        if not self._client:
            return
        try:
            from pyrogram.enums import ParseMode
            await self._client.send_message(user_id, text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.debug("notification.send.failed", user_id=user_id, error=str(exc))

    async def download_complete(self, user_id: int, anime_title: str, request_code: str) -> None:
        await self._send(
            user_id,
            f"<blockquote><b>✅ download complete</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>request:</b> <code>#{request_code}</code>\n\n"
            f"download finished! it's now being processed."
            f"</blockquote>"
        )

    async def processing_complete(self, user_id: int, anime_title: str, request_code: str, needs_approval: bool = False) -> None:
        body = (
            f"processing complete! "
            f"{'its awaiting staff approval.' if needs_approval else 'check the distribution bot to access your files.'}"
        )
        await self._send(
            user_id,
            f"<blockquote><b>📦 content ready</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>request:</b> <code>#{request_code}</code>\n\n"
            f"{body}"
            f"</blockquote>"
        )

    async def processing_failed(self, user_id: int, anime_title: str, request_code: str, error: str) -> None:
        display_error = error[:200] + "…" if len(error) > 200 else error
        await self._send(
            user_id,
            f"<blockquote><b>⚙️ processing failed</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>request:</b> <code>#{request_code}</code>\n\n"
            f"the download finished but processing encountered an error. please contact staff.\n\n"
            f"<code>{display_error}</code>"
            f"</blockquote>"
        )

    async def download_failed(self, user_id: int, anime_title: str, request_code: str, error: str) -> None:
        display_error = error[:200] + "…" if len(error) > 200 else error
        await self._send(
            user_id,
            f"<blockquote><b>❌ download failed</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>request:</b> <code>#{request_code}</code>\n\n"
            f"something went wrong during download. please contact staff.\n\n"
            f"<code>{display_error}</code>"
            f"</blockquote>"
        )

    async def request_published(self, user_id: int, anime_title: str, request_code: str) -> None:
        await self._send(
            user_id,
            f"<blockquote><b>🎉 content published!</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>request:</b> <code>#{request_code}</code>\n\n"
            f"your content is now live! open the distribution bot and search for it."
            f"</blockquote>"
        )

    async def processing_stage_warning(self, user_id: int, anime_title: str, stage: str, note: str) -> None:
        await self._send(
            user_id,
            f"<blockquote><b>⚠️ processing warning</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>stage:</b> {stage}\n"
            f"<b>note:</b> {note}\n\n"
            f"this is a non-fatal warning. processing will continue."
            f"</blockquote>"
        )

    async def request_removed(self, user_id: int, anime_title: str, request_code: str) -> None:
        """Tell a requester their request was removed by staff (owner delete)."""
        await self._send(
            user_id,
            f"<blockquote><b>🗑 Request Removed</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>Anime :</b> {anime_title}\n"
            f"<b>Request :</b> <code>#{request_code}</code>\n\n"
            f"Your request was removed by staff. If you think this was a mistake, "
            f"just request it again — no hard feelings."
            f"</blockquote>"
        )

    async def request_requeued(self, user_id: int, anime_title: str,
                               old_code: str, new_code: str) -> None:
        """Tell a requester their request was re-fetched under a fresh ticket."""
        await self._send(
            user_id,
            f"<blockquote><b>♻️ request restarted</b></blockquote>\n\n"
            f"<blockquote expandable>"
            f"<b>anime:</b> {anime_title}\n"
            f"<b>old ticket:</b> <code>#{old_code}</code>\n"
            f"<b>new ticket:</b> <code>#{new_code}</code>\n\n"
            f"we're re-fetching your request from scratch under a new ticket. "
            f"the old one has been cleared."
            f"</blockquote>"
        )
