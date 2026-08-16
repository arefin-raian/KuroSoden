from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from nekofetch.core.container import Container
from nekofetch.domain.enums import Permission
from nekofetch.localization.messages import M
from nekofetch.services.auth_service import AuthService
from nekofetch.services.franchise_flow import FranchiseFlowService
from nekofetch.ui.components import cb, keyboard
from nekofetch.ui.franchise_screens import post_processing_confirmation
from nekofetch.ui.screens import show, send_screen


async def _show_post_processing_confirmation(
    client: Client, q: CallbackQuery, container: Container, code: str,
) -> bool:
    """Show the post-processing confirmation screen for a franchise request.

    If the request has franchise data, this shows the per-entry mapping
    for admin confirmation before publishing. Otherwise falls back to
    the standard approval screen.
    """
    from nekofetch.services.publishing_service import PublishingService
    from nekofetch.services.request_service import RequestService

    L = container.localizer.get

    try:
        req = await RequestService(container).get(code)
    except Exception:
        await q.answer(L(M.ERR_GENERIC), show_alert=True)
        return False

    fr = req.franchise_data or {}
    ff = FranchiseFlowService(container)
    mapping = await ff.build_mapping_resolved(fr, req.anime_doc_id or "")

    # Only show the post-processing confirmation if there are multiple entries
    if mapping.entry_count > 1:
        backdrop_url = None
        try:
            tmdb_result = await container.tmdb.search(
                fr.get("title") or req.anime_title
            )
            if tmdb_result:
                backdrop_url = tmdb_result.backdrop_url
        except Exception:
            pass
        screen = post_processing_confirmation(mapping, code, backdrop_url=backdrop_url)
        await send_screen(client, q.message.chat.id, screen, old_msg=q.message)
        return True
    return False


def register(client: Client, container: Container) -> None:
    auth = AuthService(container)
    L = container.localizer.get

    def _allowed(q: CallbackQuery) -> bool:
        user = getattr(q, "nf_user", None)
        return bool(user and auth.has_permission(user, Permission.UPLOAD_CONTENT))

    @client.on_callback_query(filters.regex(r"^approve\|panel"))
    async def _panel(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        from nekofetch.services.publishing_service import PublishingService

        await q.answer()
        ready = await PublishingService(container).list_ready()
        back = [(L(M.BTN_BACK), cb("admin", "home"))]
        if not ready:
            caption = f"{L(M.APPROVALS_TITLE)}\n\n{L(M.APPROVALS_EMPTY)}"
            await show(client, q.message, caption, keyboard(back))
            return
        item = ready[0]

        # Try post-processing confirmation for franchise requests first
        shown = await _show_post_processing_confirmation(client, q, container, item.code)
        if shown:
            return

        # Fall back to standard approval screen
        thumb = L(M.APPROVALS_VALUE_YES) if item.has_thumbnail else L(M.APPROVALS_VALUE_NO)
        caption = (
            f"{L(M.APPROVALS_DETAIL_TITLE)}\n\n"
            + L(M.APPROVALS_DETAIL_BODY, anime=item.title, files=item.files,
                resolution=item.resolution or "—", language=item.audio or "—",
                thumbnail=thumb)
        )
        kb = keyboard(
            [(L(M.BTN_PUBLISH), cb("approve", "pub", item.code)),
             (L(M.BTN_REPROCESS), cb("approve", "reproc", item.code))],
            [(L(M.BTN_CANCEL), cb("approve", "cancel", item.code))],
            back,
        )
        await show(client, q.message, caption, kb)

    @client.on_callback_query(filters.regex(r"^approve\|(pub|reproc|cancel)"))
    async def _action(_: Client, q: CallbackQuery) -> None:
        if not _allowed(q):
            await q.answer(L(M.ACCESS_DENIED), show_alert=True)
            return
        from nekofetch.services.publishing_service import PublishingService

        _, action, code = q.data.split("|", 2)
        svc = PublishingService(container)
        back = keyboard([(L(M.BTN_BACK), cb("admin", "home"))])
        if action == "pub":
            count = await svc.publish(code)
            await q.answer(L(M.APPROVALS_TOAST_PUBLISHED, count=count), show_alert=True)
            await show(client, q.message, L(M.APPROVALS_PUBLISHED, code=code, count=count), back)
        elif action == "reproc":
            await svc.reprocess(code)
            await q.answer(L(M.APPROVALS_TOAST_REPROCESSED))
        else:
            await svc.cancel(code)
            await q.answer(L(M.APPROVALS_TOAST_CANCELLED))
            await show(client, q.message, L(M.APPROVALS_CANCELLED, code=code), back)
