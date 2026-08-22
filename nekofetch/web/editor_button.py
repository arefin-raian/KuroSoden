"""Shared 'Edit mapping' WebApp-button builder for the mapping cards.

Both the DDL confirm card (naming_confirm) and the torrent card (review) call
:func:`mapping_editor_button` to add an "Edit mapping" Telegram Mini App button.
It mints a :class:`MappingSession` (working set + which gate to release on save)
and returns an ``InlineKeyboardButton`` opening ``{base_url}/map/{token}`` — or
``None`` when ``MAPPING_EDITOR_BASE_URL`` is unset, so callers keep their legacy
text-flow button.
"""

from __future__ import annotations

from typing import Any

from nekofetch.core.logging import get_logger
from nekofetch.web.mapping_session import create_session

log = get_logger(__name__)


async def mapping_editor_button(
    container: Any, *, working_set: dict, release: dict, label: str = "🎬 Edit mapping",
) -> "Any | None":
    """Return an 'Edit mapping' WebApp button, or ``None`` if the editor is off.

    ``working_set`` is the mapping stash ({mapping, ordered_files, episode_titles,
    …}); ``release`` records the gate to commit on save ({"kind":"ddlmap","job_id"}
    or {"kind":"torrent","code"}). Best-effort: any failure (no base URL, redis
    down) returns ``None`` so the card still renders with its text-flow button."""
    base = (getattr(container.env, "mapping_editor_base_url", "") or "").rstrip("/")
    if not base:
        return None
    try:
        from pyrogram.types import InlineKeyboardButton, WebAppInfo

        token = await create_session(container.redis, working_set, release)
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=f"{base}/map/{token}"))
    except Exception as exc:  # noqa: BLE001 — never block the card on the editor
        log.warning("mapping_editor.button_failed", error=str(exc))
        return None
