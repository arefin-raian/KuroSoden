from __future__ import annotations

from pyrogram.types import InlineKeyboardMarkup

from nekofetch.domain.enums import Role
from nekofetch.localization.i18n import Localizer
from nekofetch.localization.messages import M
from nekofetch.ui.components import cb, keyboard


def welcome_keyboard(localizer: Localizer, role: Role, lang: str = "en") -> InlineKeyboardMarkup:
    g = localizer.get
    rows = [
        [
            (g(M.BTN_REQUEST_ANIME, lang), cb("req", "new")),
            (g(M.BTN_MY_REQUESTS, lang), cb("req", "mine", 0)),
        ]
    ]
    if role in (Role.STAFF, Role.ADMIN):
        rows.append([(g(M.BTN_REVIEW_REQUESTS, lang), cb("staff", "requests", 0)),
                     (g(M.ADMIN_BTN_QUEUE, lang), cb("queue", "view", 0))])
    if role is Role.ADMIN:
        rows.append([(g(M.ADMIN_BTN_PANEL, lang), cb("admin", "home"))])
    return keyboard(*rows)
