"""Lelouch screen builders — voice + state → :class:`Screen`.

Pure builders (no Telegram I/O) so the dispatcher in ``app.py`` stays a thin
router. Every card goes through :func:`nekofetch.ui.screens.card`, so all of
Lelouch's surfaces share one grammar: a Lelouch-voiced HTML caption (from
``shared.lelouch_voice``), an image that is never omitted (recurring Lelouch
art here; per-anime backdrops on the request/dedup/receipt cards built in the
request handler), and a callback keyboard.
"""

from __future__ import annotations

from kurosoden.shared import lelouch_voice as V
from nekofetch.ui.components import cb
from nekofetch.ui.screens import Screen, card

BOT = "lelouch"


def home(name: str, *, is_staff: bool = False, is_admin: bool = False,
         is_owner: bool = False) -> Screen:
    """The request bot's front door — exactly the buttons the viewer needs.

    Three audiences:
      • Regular user → the two request actions only.
      • Non-owner admin → Batch Work + **Settings** (their personal profile and
        the Board). They do NOT get Command — admin management (pausing requests,
        managing ranks, force-sub) is the owner's alone.
      • Owner → Batch Work + **Command** (the full war table: stats, ranks,
        availability, hours, config settings).

    Settings/Command are never both shown, and neither appears at the top level
    for a plain user, so the request desk stays clean.
    """
    caption = f"{V.home_title(name)}\n\n{V.HOME_BODY}"
    rows = [[(V.BTN_REQUEST, cb("req", "new")),
             (V.BTN_MY_REQUESTS, cb("req", "mine", 0))]]
    if is_owner:
        caption += f"\n\n{V.HOME_ADMIN_TAG}"
        rows.append([(V.BTN_BATCH, cb("batch", "new")),
                     (V.BTN_ADMIN, cb(BOT, "admin"))])
    elif is_admin:
        caption += f"\n\n{V.HOME_ADMIN_TAG}"
        rows.append([(V.BTN_BATCH, cb("batch", "new")),
                     (V.BTN_PROFILE, cb(BOT, "profile"))])
    return card(caption, bot_name=BOT, buttons=rows)


def admin_panel(*, mode: str, requests_open: bool, total: int,
                working: int, is_owner: bool = False) -> Screen:
    caption = V.admin_panel(mode, requests_open, total, working)
    toggle = (V.BTN_PAUSE, cb(BOT, "reqtoggle")) if requests_open \
        else (V.BTN_RESUME, cb(BOT, "reqtoggle"))
    rows = [
        [toggle],
        [(V.BTN_PENDING, cb(BOT, "pending", 0)),
         (V.BTN_QUEUE, cb(BOT, "queue", 0))],
        [(V.BTN_MANAGE, cb(BOT, "manage")),
         (V.BTN_AVAIL, cb(BOT, "avail"))],
        [(V.BTN_HOURS, cb(BOT, "hours")),
         (V.BTN_SETTINGS, cb(BOT, "settings"))],
        [("🗂 Manage REQ/WRK", cb("mg", "reqs", 0))],
    ]
    if is_owner:
        rows.append([("🔁 Redo", cb("redo", "new")),
                     (V.BTN_CLEAR_DATABASE, cb(BOT, "dbclear"))])
    rows.append([(V.BTN_HOME, cb(BOT, "home"))])
    return card(caption, bot_name=BOT, buttons=rows)


def queue(*, stats, admins_total: int = 0, admins_on: int = 0,
          back: str = "home") -> Screen:
    caption = V.queue_view(stats, admins_total=admins_total, admins_on=admins_on)
    return card(caption, bot_name=BOT,
                buttons=[[(V.BTN_HOME if back == "home" else V.BTN_BACK_ADMIN,
                           cb(BOT, back))]])


def _panel(body_title: str, body: str, back: str = "admin") -> Screen:
    caption = f"{V.ICON} <b>{body_title}</b>\n\n{body}"
    return card(caption, bot_name=BOT,
                buttons=[[(V.BTN_BACK_ADMIN, cb(BOT, back))]])


def manage() -> Screen:
    return _panel("Manage Ranks", V.MANAGE_BODY)


def availability() -> Screen:
    return _panel("Availability", V.AVAIL_BODY)


def working_hours() -> Screen:
    return _panel("Working Hours", V.HOURS_BODY)


def coming_soon(what: str, *, back: str = "admin") -> Screen:
    return card(V.coming_soon(what), bot_name=BOT,
                buttons=[[(V.BTN_BACK_ADMIN, cb(BOT, back))]])
