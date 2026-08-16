"""Lelouch vi Britannia — the request bot's voice.

Every user-facing line Lelouch speaks lives here so his tone stays consistent
and can be re-tuned in one place. He is imperious but charismatic, a strategist
who talks in terms of the board, pieces, and the endgame — first person, never
terse, never breaking character. Generic NekoFetch copy stays in the JSON
catalog (``localization.messages``); this module holds only what is distinctly
*Lelouch*.

All strings are authored as Telegram HTML (the pipeline's default parse mode).
Callables take runtime values and return finished HTML; plain strings are used
as-is. Handlers reference these — never inline character copy — so a rewrite of
his voice is a single-file edit.
"""

from __future__ import annotations

import html

ICON = "♟️"  # the chess piece that heads every Lelouch card


def esc(text: str) -> str:
    """HTML-escape a runtime value before it lands in a caption."""
    return html.escape(str(text or ""), quote=False)


# ── Welcome / home ────────────────────────────────────────────────────────────

def home_title(name: str) -> str:
    who = esc(name) or "you"
    return f"{ICON} <b>Lelouch vi Britannia</b> — at your command, {who}."


HOME_BODY = (
    "<i>\"The world isn't changed by prayers — it's changed by action.\"</i>\n\n"
    "This is where the game begins. Name an anime and I set the whole machine "
    "in motion: I hunt down its true form across every archive, confirm the "
    "franchise down to the last OVA, and hand it to the ones who bring it home. "
    "You need only make the request — I handle the rest of the board."
)

HOME_ADMIN_TAG = (
    "<blockquote>You hold command here. Batch the work, marshal the ranks, and "
    "watch the queue bend to your will.</blockquote>"
)


# ── Force-join gate ─────────────────────────────────────────────────────────

JOIN_TITLE = f"{ICON} <b>A pawn cannot cross the board uninvited.</b>"

JOIN_BODY = (
    "Before you may command a request, take your place among us. Join the "
    "channel below — it costs you nothing and grants you everything. "
    "Once you've done so, tap <b>I've joined</b> and we'll begin.\n\n"
    "<i>I don't deal in half-measures. Stand with the cause, and the cause "
    "stands with you.</i>"
)

JOIN_STILL_MISSING = (
    "I see no allegiance yet. Join the channel first — then the board is yours."
)


# ── Request gate / limits ───────────────────────────────────────────────────

REQUESTS_PAUSED = (
    f"{ICON} <b>The board is frozen.</b>\n\n"
    "I don't move pieces I can't win with. We're consolidating the queue right "
    "now, so new requests wait. Return shortly — when the game resumes, you'll "
    "have my full attention.\n\n"
    "<i>Patience is a weapon few know how to wield. Wield it.</i>"
)


def limit_reached(active_title: str | None = None) -> str:
    line = (
        "You already hold a piece in play"
        + (f" — <b>{esc(active_title)}</b>" if active_title else "")
        + ". One move at a time; that's how a strategist wins."
    )
    return (
        f"{ICON} <b>One request at a time.</b>\n\n"
        f"{line}\n\n"
        "<i>See your current request through, and the next is yours. Fairness "
        "isn't a limitation — it's the rule that keeps the game worth playing.</i>"
    )


def not_found(query: str) -> str:
    return (
        f"{ICON} <b>That name eludes even me.</b>\n\n"
        f"I searched every archive I command and found nothing matching "
        f"<b>{esc(query)}</b>. Check the spelling, or give me the title as it's "
        f"truly known — an alias, the romaji, the original.\n\n"
        "<i>A commander is only as good as his intelligence. Give me better, and "
        "I'll deliver.</i>"
    )


def sources_unreachable(query: str) -> str:
    """Shown when the WHOLE metadata chain missed — could be a genuine miss, but
    just as likely every source (AniList, the datasets, Jikan, Kitsu) is down or
    rate-limited at once. Offer a retry rather than a dead end."""
    return (
        f"{ICON} <b>The archives won't answer — for now.</b>\n\n"
        f"I couldn't pull <b>{esc(query)}</b> from a single source. Either the "
        f"name is off, or every intelligence line is down at once. Check the "
        f"spelling if you like — otherwise the lines usually clear in a moment.\n\n"
        "<i>Strike again when you're ready; press the button and I'll send the "
        "same order back down the wire.</i>"
    )


# ── Receipt / dedup ─────────────────────────────────────────────────────────

RECEIPT_CLOSER = (
    "<i>Consider it done. The machine is already turning — you'll know the "
    "moment it's yours.</i>"
)


def already_requested(title: str, code: str, stage: str) -> str:
    return (
        f"{ICON} <b>This piece is already in play.</b>\n\n"
        f"<b>{esc(title)}</b> was claimed before you — it's <b>{esc(stage)}</b> "
        f"as we speak.\n"
        f"<b>Reference</b> : <code>{esc(code)}</code>\n\n"
        "<i>No need to command the same move twice. When it's published, every "
        "soul who wanted it — you included — receives the link. That's a "
        "promise, not a maybe.</i>"
    )


def already_available(title: str) -> str:
    return (
        f"{ICON} <b>Already in our arsenal.</b>\n\n"
        f"<b>{esc(title)}</b> is done — it's already yours to take. Open it "
        f"below.\n\n"
        "<i>Why conquer ground we already hold? Go — it's waiting for you.</i>"
    )


# ── Admin notifications ─────────────────────────────────────────────────────

def new_request_ping(title: str, code: str, requester: str, requester_id: int,
                     breakdown: str) -> str:
    return (
        f"{ICON} <b>New orders from the front.</b>\n\n"
        f"<b>{esc(title)}</b>\n"
        f"<code>{esc(code)}</code>  ·  {esc(breakdown)}\n\n"
        f"<b>Requested by</b> : {esc(requester) or 'a subject'} "
        f"(<code>{requester_id}</code>)\n\n"
        "<i>The board is set. Take your position at the downloader and choose "
        "a source — the rest follows.</i>"
    )


def new_work_ping(title: str, code: str, added_by: str) -> str:
    return (
        f"{ICON} <b>Work added to the line.</b>\n\n"
        f"<b>{esc(title)}</b>\n"
        f"<code>{esc(code)}</code>\n\n"
        f"<b>Marshalled by</b> : {esc(added_by) or 'command'}\n\n"
        "<i>This isn't a request — it's a directive. Pull it when you're ready; "
        "the queue won't wait, and neither should we.</i>"
    )


def idle_nudge(admin_name: str, pending: int) -> str:
    who = esc(admin_name) or "soldier"
    plural = "pieces" if pending != 1 else "piece"
    return (
        f"{ICON} <b>{who}, the board is waiting.</b>\n\n"
        f"There {'are' if pending != 1 else 'is'} <b>{pending}</b> {plural} still "
        f"in the line and your hand has gone still. I don't ask for miracles — "
        f"only movement.\n\n"
        "<i>Momentum wins wars. Take the next task and keep the machine turning.</i>"
    )


# ── Batch (admin) ───────────────────────────────────────────────────────────

BATCH_PROMPT = (
    f"{ICON} <b>Marshal the ranks.</b>\n\n"
    "Send me the titles you want brought in — one per line, or separated by "
    "commas. I'll parade each one before you for confirmation, and every name "
    "you approve joins the work line.\n\n"
    "<i>An army moves as one. Name them all; I'll sort the order.</i>"
)


def batch_processing(n: int) -> str:
    plural = "titles" if n != 1 else "title"
    return (
        f"{ICON} <b>Marshalling {n} {plural}…</b>\n\n"
        "<i>I'm scouring every archive I command — AniList, the userbot line, "
        "TMDB — and confirming each one's true form before it reaches you.</i>"
    )


def batch_confirm_kicker(index: int, total: int) -> str:
    return f"<i>Reviewing {index} of {total} — approve, skip, or finish.</i>"


def batch_choose_franchise(query: str, index: int, total: int) -> str:
    """Franchise-selection card: several distinct franchises answer to one name."""
    tail = (f"<i>Line {index} of {total} awaiting a name.</i>"
            if total > 1 else "<i>Pick the one you mean.</i>")
    return (
        f"{ICON} <b>Several banners answer to “{esc(query)}”.</b>\n\n"
        "These are separate <b>franchises</b> — not seasons of one line. Choose the "
        "one you want brought in; its seasons and films come with it.\n\n"
        f"{tail}"
    )


def batch_approval_summary(entries: list[tuple[str, str]]) -> str:
    """Final approval card: every resolved franchise, ready to commit.

    ``entries`` is a list of (title, detail) pairs — one per franchise the admin
    settled on (auto-approved singles + picked ambiguous ones)."""
    n = len(entries)
    plural = "franchises" if n != 1 else "franchise"
    lines = [f"{ICON} <b>The line is ready.</b>\n",
             f"<b>{n}</b> {plural} settled and approved for the line:\n"]
    for title, detail in entries[:20]:
        lines.append(f"✓ <b>{esc(title)}</b>\n   <i>{esc(detail)}</i>")
    if n > 20:
        lines.append(f"<i>…and {n - 20} more.</i>")
    lines.append("\n<b>Verdict</b> : ✓ <b>approved for the line</b>")
    lines.append("<i>Commit to send them down, or stand the batch down.</i>")
    return "\n".join(lines)


def batch_review(title: str, detail: str, index: int, total: int,
                 approved: bool) -> str:
    """Per-item review card in the batch carousel."""
    verdict = ("✓ <b>approved for the line</b>" if approved
               else "awaiting your word")
    return (
        f"{ICON} <b>{esc(title)}</b>\n"
        f"<i>{esc(detail)}</i>\n\n"
        f"<b>Verdict</b> : {verdict}\n\n"
        f"{batch_confirm_kicker(index, total)}"
    )


def batch_done(confirmed: int, skipped: list[str] | None = None) -> str:
    plural = "titles" if confirmed != 1 else "title"
    body = [
        f"{ICON} <b>The line is drawn.</b>\n",
        f"<b>{confirmed}</b> {plural} committed to the work queue. The downloaders "
        "will pull them in order — nothing stalls, nothing waits on ceremony.",
    ]
    if skipped:
        passed = ", ".join(f"<b>{esc(s)}</b>" for s in skipped[:8])
        more = f" (+{len(skipped) - 8} more)" if len(skipped) > 8 else ""
        body.append(f"\n<i>Set aside: {passed}{more}.</i>")
    body.append("\n<i>A plan set in motion is a plan already half-won.</i>")
    return "\n".join(body)


def batch_admin_summary(entries: list[tuple[str, str]]) -> str:
    """DM to admins after a batch commits — (code, title) pairs."""
    n = len(entries)
    plural = "directives" if n != 1 else "directive"
    lines = [f"{ICON} <b>{n} {plural} added to the line.</b>\n"]
    for code, title in entries[:12]:
        lines.append(f"<code>{esc(code)}</code>  ·  {esc(title)}")
    if n > 12:
        lines.append(f"<i>…and {n - 12} more.</i>")
    lines.append(
        "\n<i>These aren't requests — they're orders. Pull them when you're "
        "ready; the queue won't wait, and neither should we.</i>"
    )
    return "\n".join(lines)


BATCH_EMPTY = (
    f"{ICON} <b>Nothing to commit.</b>\n\n"
    "You approved none of them. Send a fresh batch when you're ready to move."
)


def batch_none_found(skipped: list[str] | None = None) -> str:
    body = [
        f"{ICON} <b>Not a single one held up.</b>\n",
        "I searched every archive and none of those names resolved to a true "
        "franchise. Check the spellings, or give me the titles as they're truly "
        "known.",
    ]
    if skipped:
        passed = ", ".join(f"<b>{esc(s)}</b>" for s in skipped[:8])
        body.append(f"\n<i>Missed: {passed}.</i>")
    body.append("\n<i>A commander is only as good as his intelligence.</i>")
    return "\n".join(body)


# ── Admin panel / management ────────────────────────────────────────────────

def admin_panel(mode: str, requests_open: bool, total: int, working: int) -> str:
    """Command — the War Table. Deliberately lean: the gate, the tempo, and the
    single figure that matters at a glance (total requests ever). The rich
    breakdown lives on The Board."""
    gate = ("🟢 <b>Accepting requests</b>" if requests_open
            else "🔴 <b>Requests paused</b>")
    return (
        f"{ICON} <b>Command — Lelouch's War Table</b>\n\n"
        f"{gate}\n"
        f"<b>Mode</b> : {esc(mode)}\n"
        f"<b>Total requests</b> : {total}\n"
        f"<b>Currently working</b> : {working}\n\n"
        "<i>Every piece answers to you here. Set the tempo, marshal the ranks, "
        "and open the Board when you want the whole picture.</i>"
    )


def queue_view(stats, admins_total: int = 0, admins_on: int = 0) -> str:
    """The Board — every real statistic at a glance. ``stats`` is a
    :class:`RequestStats`; ``admins_*`` summarise the pool."""
    return (
        f"{ICON} <b>The Board</b>\n\n"
        f"<b>Total requests</b> : {stats.total}\n"
        f"<b>⏳ Awaiting review</b> : {stats.pending}\n"
        f"<b>⚙️ Working</b> : {stats.working}\n"
        f"<b>✅ Published</b> : {stats.published}\n"
        f"<b>🚫 Rejected</b> : {stats.rejected}\n"
        f"<b>💥 Failed</b> : {stats.failed}\n\n"
        f"<b>👥 Ranks</b> : {admins_on}/{admins_total} on the field\n\n"
        "<i>Everything in motion, at a glance. A stalled stage never freezes "
        "the queue — the board keeps turning.</i>"
    )


MANAGE_BODY = (
    "Marshal the ranks across all four stages. Assign soldiers to a bot, weight "
    "the ones you trust, grant leave, and set their hours.\n\n"
    "<i>An army is only as sharp as its command. Choose wisely.</i>"
)

AVAIL_BODY = (
    "Who stands ready and who's at rest. Toggle availability, and the assignment "
    "engine routes around anyone off the field.\n\n"
    "<i>A rested blade cuts cleaner. Rotate them well.</i>"
)

HOURS_BODY = (
    "Set each soldier's active window and the campaign's tempo — normal, "
    "catch-up, or a full halt. I won't rouse anyone off the clock.\n\n"
    "<i>Time is the one resource even I can't reclaim. Spend it deliberately.</i>"
)


_STAGE_NAMES = {
    "lelouch": "Requests", "levi": "Download",
    "senku": "Process", "gojo": "Publish",
}


def stage_label(stage: str) -> str:
    return _STAGE_NAMES.get(stage, stage.title())


def _admin_status_glyph(v) -> str:
    if not v.is_available:
        return "🔴"
    if v.on_break:
        return "☕"
    if v.active_tasks > 0:
        return "⚔️"
    return "🟢"


def manage_roster(admins: list) -> str:
    """The pool overview — one line per admin with load + coverage."""
    if not admins:
        return (
            f"{ICON} <b>Manage Ranks</b>\n\n"
            "No soldiers in the pool yet. Add an admin by their Telegram ID and "
            "assign them the stages they'll cover.\n\n"
            "<i>An empty command tent wins no wars. Muster someone.</i>"
        )
    lines = [f"{ICON} <b>Manage Ranks</b>\n"]
    for v in admins:
        name = esc(v.name or str(v.telegram_id))
        bots = "·".join(stage_label(b) for b in v.assigned_bots) or "unassigned"
        wt = f" ×{v.weight}" if v.weight != 1 else ""
        lines.append(
            f"{_admin_status_glyph(v)} <b>{name}</b>{wt} — {esc(bots)}  "
            f"({v.active_tasks} active · {v.total_completed} done)"
        )
    lines.append("\n<i>Tap a soldier to weight them, grant leave, or reassign "
                 "their stages.</i>")
    return "\n".join(lines)


def manage_admin_detail(v) -> str:
    """The per-admin control card."""
    name = esc(v.name or str(v.telegram_id))
    avail = ("🟢 available" if v.is_available else "🔴 off-duty")
    if v.on_break and v.break_until:
        until = esc(v.break_until[11:16])
        avail += f" · ☕ on break until {until} UTC"
    bots = ", ".join(stage_label(b) for b in v.assigned_bots) or "none"
    if v.working_hours:
        hrs = f"{v.working_hours['start']:02d}:00–{v.working_hours['end']:02d}:00 UTC"
    else:
        hrs = "always on"
    return (
        f"{ICON} <b>{name}</b>\n\n"
        f"<b>Status</b> : {avail}\n"
        f"<b>Stages</b> : {esc(bots)}\n"
        f"<b>Weight</b> : ×{v.weight}\n"
        f"<b>Hours</b> : {esc(hrs)}\n"
        f"<b>Load</b> : {v.active_tasks} active · {v.total_completed} completed\n\n"
        "<i>Set their coverage and tempo. The assignment engine obeys "
        "instantly.</i>"
    )


def availability_board(admins: list) -> str:
    if not admins:
        return (
            f"{ICON} <b>Availability</b>\n\n"
            "No one's in the pool yet — nothing to stand ready.\n\n"
            "<i>Muster a rank first from Manage Ranks.</i>"
        )
    ready = [v for v in admins if v.is_available and not v.on_break]
    resting = [v for v in admins if not v.is_available or v.on_break]
    lines = [f"{ICON} <b>Availability</b>\n"]
    lines.append(f"<b>On the field</b> ({len(ready)}):")
    if ready:
        lines.extend(
            f"  🟢 {esc(v.name or str(v.telegram_id))} — {v.active_tasks} active"
            for v in ready
        )
    else:
        lines.append("  <i>none</i>")
    lines.append(f"\n<b>At rest</b> ({len(resting)}):")
    if resting:
        lines.extend(
            f"  {'☕' if v.on_break else '🔴'} {esc(v.name or str(v.telegram_id))}"
            for v in resting
        )
    else:
        lines.append("  <i>none</i>")
    lines.append("\n<i>Tap a name to flip them on or off the field.</i>")
    return "\n".join(lines)


def hours_board(admins: list, mode: str) -> str:
    lines = [
        f"{ICON} <b>Working Hours & Tempo</b>\n",
        f"<b>Campaign mode</b> : {esc(mode)}\n",
    ]
    if admins:
        for v in admins:
            if v.working_hours:
                w = (f"{v.working_hours['start']:02d}:00–"
                     f"{v.working_hours['end']:02d}:00 UTC")
            else:
                w = "always on"
            lines.append(f"🕰 <b>{esc(v.name or str(v.telegram_id))}</b> — {esc(w)}")
    else:
        lines.append("<i>No soldiers in the pool yet.</i>")
    lines.append(
        "\n<i>Set the tempo for the whole campaign, or tap a soldier to bound "
        "their hours. I won't rouse anyone off the clock.</i>"
    )
    return "\n".join(lines)


def break_scheduled(name: str, hours: float) -> str:
    h = int(hours) if hours == int(hours) else round(hours, 1)
    return (
        f"{ICON} <b>Leave granted.</b>\n\n"
        f"<b>{esc(name)}</b> is off the field for <b>{h}h</b>. The engine routes "
        "around them until they're back — no task will find them.\n\n"
        "<i>Even a commander rests. Return sharp.</i>"
    )


def reassigned(code: str, to_name: str) -> str:
    return (
        f"{ICON} <b>Order reissued.</b>\n\n"
        f"<code>{esc(code)}</code> now answers to <b>{esc(to_name)}</b>. The board "
        "shifts to match.\n\n"
        "<i>A stalled piece is a wasted one. Keep them all moving.</i>"
    )


def coming_soon(what: str) -> str:
    return (
        f"{ICON} <b>{esc(what)}</b>\n\n"
        "The controls for this are being forged. Tap Back and press on — the "
        "rest of the board is fully yours.\n\n"
        "<i>Even Britannia wasn't built in a day.</i>"
    )


UNKNOWN_ACTION = "That move isn't on the board yet."


# ── Buttons ─────────────────────────────────────────────────────────────────

BTN_JOINED = "✓ I've joined"
BTN_RECHECK = "↻ Check again"
BTN_BATCH_YES = "✓ This is it"
BTN_BATCH_UNDO = "↶ Rescind"
BTN_BATCH_SKIP = "✗ Skip"
BTN_BATCH_DONE = "⚑ Commit the line & send down"
BTN_BATCH_CANCEL = "✗ Stand down"
BTN_PREV = "◀"
BTN_NEXT = "▶"
# Franchise-picker pager — paired arrows, one facing each way, in the same style.
BTN_FR_PREV = "◀ Prev"
BTN_FR_NEXT = "Next ▶"

BTN_REQUEST = "🎬 Request Anime"
BTN_MY_REQUESTS = "📥 My Requests"
BTN_SETTINGS = "⚙️ Settings"
BTN_ADMIN = "🛡 Command"
BTN_PROFILE = "👤 My Profile"
BTN_BATCH = "📦 Batch Work"
BTN_QUEUE = "📋 The Board"
BTN_OPEN_TASKS = "📋 Open Tasks"  # deep-link to Levi's board after a batch commit
BTN_HOME = "⇐ Home"
BTN_BACK_ADMIN = "⇐ Back to Command"
BTN_PAUSE = "🔴 Pause Requests"
BTN_RESUME = "🟢 Resume Requests"
BTN_PENDING = "📋 Pending Requests"
BTN_MANAGE = "👥 Manage Ranks"
BTN_AVAIL = "📊 Availability"
BTN_HOURS = "🕰 Working Hours"
BTN_CLEAR_DATABASE = "🧨 Clear Database"
BTN_CLEAR_DATABASE_CONFIRM = "Yes — clear operational data"
BTN_CLEAR_DATABASE_CANCEL = "No — keep it"

# ── Management controls ──────────────────────────────────────────────────────
BTN_ADD_ADMIN = "➕ Muster a rank"
BTN_REMOVE_ADMIN = "✗ Discharge"
BTN_WEIGHT_UP = "▲ Weight"
BTN_WEIGHT_DOWN = "▼ Weight"
BTN_TAKE_OFF = "🔴 Stand down"
BTN_PUT_ON = "🟢 Send in"
BTN_BREAK = "☕ Grant leave (1h)"
BTN_END_BREAK = "↺ End leave"
BTN_SET_HOURS = "🕰 Set hours"
BTN_CLEAR_HOURS = "∞ Always on"
BTN_BACK_MANAGE = "⇐ Back to Ranks"
BTN_BACK_AVAIL = "⇐ Back to Availability"
BTN_MODE_NORMAL = "⚔️ Normal"
BTN_MODE_CATCHUP = "🏃 Catch-up"
BTN_MODE_PAUSED = "🛑 Halt"


# ── Admin self-service profile ────────────────────────────────────────────────
# Non-owner admins get a personal profile they can read and edit: the country,
# timezone, daily-hours cap and preferred time slots the assignment engine uses
# to route work to them at the right local time.

def _tz_label(tz_name: str | None) -> str:
    from nekofetch.core.timefmt import tz_offset_label
    return tz_offset_label(tz_name) if tz_name else "not set"


def profile_card(v, *, weekday_str: str, weekend_str: str) -> str:
    """The admin's own profile view. ``v`` is a ManagementService AdminView."""
    name = esc(v.name or str(v.telegram_id))
    country = esc(v.country) if v.country else "—"
    tz = f"{esc(v.timezone)} ({_tz_label(v.timezone)})" if v.timezone else "—"
    hours = f"{v.max_hours_per_day}h/day" if v.max_hours_per_day else "—"
    return (
        f"{ICON} <b>My Profile — {name}</b>\n\n"
        f"🌍 <b>Country</b> : {country}\n"
        f"🕎 <b>Timezone</b> : {tz}\n"
        f"⏰ <b>Hours/day</b> : {hours}\n\n"
        f"🕒 <b>Weekday slots</b>\n<blockquote>{esc(weekday_str)}</blockquote>\n"
        f"🕒 <b>Weekend slots</b>\n<blockquote>{esc(weekend_str)}</blockquote>\n"
        "<i>These decide when — and in whose local time — work is routed to you. "
        "Keep them honest and I'll keep your rest sacred.</i>"
    )


PROFILE_ASK_COUNTRY = (
    f"{ICON} <b>Your country</b>\n\n"
    "Send the country you're in — like <code>Bangladesh</code>. I'll use it for "
    "context and to suggest your timezone.\n\n<code>/cancel</code> to keep it."
)
PROFILE_ASK_HOURS = (
    f"{ICON} <b>Hours per day</b>\n\n"
    "How many hours a day do you want to take work? Send a number, like "
    "<code>3</code> (1–24).\n\n<code>/cancel</code> to keep it."
)


def profile_ask_slots(kind: str) -> str:
    when = "weekday" if kind == "weekday" else "weekend"
    return (
        f"{ICON} <b>Your {when} time slots</b>\n\n"
        "Send your available times in <b>your own local clock</b>, one slot per "
        "line. For example:\n"
        "<blockquote>6:00 PM - 8:00 PM\n10:30 PM - 12:30 AM</blockquote>\n"
        "I'll read 12-hour (6pm) or 24-hour (18:00), and a slot may cross "
        "midnight. Send <code>none</code> to clear them, or <code>/cancel</code> "
        "to keep them."
    )


BTN_EDIT_COUNTRY = "🌍 Country"
BTN_EDIT_TIMEZONE = "🕎 Timezone"
BTN_EDIT_HOURS = "⏰ Hours/day"
BTN_EDIT_WEEKDAY = "🕒 Weekday slots"
BTN_EDIT_WEEKEND = "🕒 Weekend slots"
BTN_VIEW_BOARD = "📋 The Board"


# ── Redo (owner-only re-processing) ─────────────────────────────────────────

REDO_ASK_TITLE = (
    f"{ICON} <b>Order a redo.</b>\n\n"
    "Name the series to re-process — corrupted files, a bad encode, a branding "
    "slip. Send the title and I'll find it.\n\n"
    "<i>This overrides the usual 'already have it' refusal. Use it deliberately.</i>"
)

BTN_REDO_CANCEL = "✗ Cancel"

REDO_CANCELLED = (
    f"{ICON} <b>Stood down.</b> The redo was cancelled — nothing was touched."
)

REDO_RETRY_TITLE = (
    f"{ICON} <b>Not that one? Name it again.</b>\n\n"
    "Send the title of the series you want redone."
)


def redo_confirm(title: str, state_label: str, action: str) -> str:
    """The 'this is it' card before a redo commits — shows the detected state and
    exactly what will happen, since a redo is destructive."""
    return (
        f"{ICON} <b>{esc(title)}</b>\n\n"
        f"<b>Current state</b> : {esc(state_label)}\n"
        f"<b>Plan</b> : {esc(action)}\n\n"
        "<i>Confirm and the machine turns again. This is the piece you meant?</i>"
    )


def redo_receipt(title: str, state_label: str, action: str) -> str:
    """Shown after the owner confirms a redo."""
    return (
        f"{ICON} <b>Redo set in motion.</b>\n\n"
        f"<b>{esc(title)}</b>\n"
        f"<b>State</b> : {esc(state_label)}\n"
        f"<b>Action</b> : {esc(action)}\n\n"
        "<i>The line reclaims it. Levi takes it first — the rest follows.</i>"
    )


def redo_task_notice(title: str, stage: str | None) -> str:
    """DM to the admin who was on a task that's being redone out from under them."""
    where = f" at the <b>{esc(stage)}</b> stage" if stage else ""
    return (
        f"{ICON} <b>Stand down — this one's being redone.</b>\n\n"
        f"<b>{esc(title)}</b>{where} is being re-processed from the top by the "
        f"owner's command. Your current task on it is void — drop it, a fresh "
        f"order is on its way.\n\n"
        "<i>No fault of yours. The board simply resets this piece.</i>"
    )


def redo_new_seasons_notice(count: int) -> str:
    """DM to the owner when a redo surfaces season(s) not yet published."""
    s = "season" if count == 1 else "seasons"
    return (
        f"{ICON} <b>New ground spotted.</b>\n\n"
        f"While planning this redo I found <b>{count}</b> {s} that aren't on the "
        f"channel yet.\n\n"
        "<i>The redo handles what's already there. Adding the new "
        f"{s} is a separate campaign — I'll flag it for the next update pass.</i>"
    )


def redo_update_prompt(title: str, season_labels: list[str]) -> str:
    """K5 prompt — a redo surfaced season(s) the channel doesn't have yet."""
    ground = ", ".join(season_labels) or "a new season"
    return (
        f"{ICON} <b>New ground spotted.</b>\n\n"
        f"<b>{esc(title)}</b> now has <b>{esc(ground)}</b> that aren't on the "
        f"channel yet.\n\n"
        "Include it in this redo? The new season downloads, its card appends "
        "to the channel, and the main post gets a reply.\n\n"
        "<i>Say no to redo only what's already published.</i>"
    )


def redo_update_queued(title: str, count: int) -> str:
    """K5 receipt — the owner chose to include the new season(s)."""
    s = "season" if count == 1 else "seasons"
    return (
        f"{ICON} <b>Included.</b>\n\n"
        f"<b>{esc(title)}</b>: <b>{count}</b> new {s} queued as an update. The "
        f"entry downloads, its card appends to the channel, and the main post "
        f"gets a reply when it lands.\n\n"
        "<i>The channel grows, not resets.</i>"
    )


BTN_REDO_YES = "♻️ Yes — redo this"
BTN_REDO_NO = "✖️ Not it"

OWNER_ONLY = (
    f"{ICON} <b>Owner's command only.</b>\n\n"
    "This order answers to one voice alone."
)
