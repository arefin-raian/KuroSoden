"""Gojo Satoru — the publisher bot's voice.

Every user-facing line Gojo speaks lives here so his tone stays consistent and
can be re-tuned in one place. He is the strongest — effortless, playful,
supremely confident, a little smug, but never careless. Publishing is the final
flourish: the channel goes public, the index updates, the catalog grows. He
treats it like the closing move of a fight already won. First person, a little
flair (🔮✨🌀), never breaking character. Generic NekoFetch copy stays in the
JSON catalog (``localization.messages``); this module holds only what is
distinctly *Gojo*.

All strings are authored as Telegram HTML (the pipeline's default parse mode).
Callables take runtime values and return finished HTML; plain strings are used
as-is. Handlers reference these — never inline character copy — so a rewrite of
his voice is a single-file edit. Mirrors ``senku_voice`` / ``levi_voice`` /
``lelouch_voice`` structurally.
"""

from __future__ import annotations

import html

ICON = "🔮"  # the infinity that heads every Gojo card


def esc(text: str) -> str:
    """HTML-escape a runtime value before it lands in a caption."""
    return html.escape(str(text or ""), quote=False)


# ── Welcome / home ────────────────────────────────────────────────────────────

def home_title(name: str) -> str:
    who = esc(name) or "sorcerer"
    return f"{ICON} <b>Gojo Satoru</b> — the strongest's on the clock, {who}."


HOME_BODY = (
    "<i>\"Throughout heaven and earth, I alone am the honored one.\"</i>\n\n"
    "Senku hands me a finished channel; I take it public. Build the main-channel "
    "card, average the ratings, drop the synopsis, wire the buttons, and light up "
    "the index. Nothing gets past me and nothing goes out sloppy. Pick a title — "
    "let's make it official."
)

HOME_ADMIN_TAG = (
    "<blockquote>Publishing is the last word. Once it's live, the whole catalog "
    "sees it — so we get it right, every card, every button, every time.</blockquote>"
)


# ── Task list ─────────────────────────────────────────────────────────────────

TASKS_EMPTY = (
    f"{ICON} <b>Nothing to publish.</b>\n\n"
    "No titles waiting on me right now. The moment Senku finishes a channel it "
    "lands here — and then I take it public."
)


def tasks_title(count: int) -> str:
    n = "title" if count == 1 else "titles"
    return f"{ICON} <b>{count} {n}</b> ready to go public. Pick one — this part's easy."


def task_row(code: str, title: str, *, in_progress: bool = False) -> str:
    icon = "🌀" if in_progress else "⏳"
    return f"{icon} <code>{esc(code)}</code> — <b>{esc(title)}</b>"


# ── Publish review card ─────────────────────────────────────────────────────────

def review_card(title: str, code: str, anime_doc_id: str | None = None) -> str:
    lines = [
        f"{ICON} <b>Ready to go public</b>",
        "",
        f"🎬 <b>{esc(title)}</b>",
        f"<code>{esc(code)}</code>",
    ]
    if anime_doc_id:
        lines.append(f"🆔 <code>{esc(anime_doc_id)}</code>")
    lines.append(
        "\nThis is the main-channel card — franchise synopsis, averaged rating, "
        "the season-one art. Publish it as-is, send it quietly, schedule it for "
        "later, or tweak the caption first. Your call."
    )
    return "\n".join(lines)


EDIT_CAPTION_PROMPT = (
    f"{ICON} <b>Rewrite the caption.</b>\n\n"
    "Send the new version — Telegram styling, Markdown, or raw HTML all work, and "
    "your line breaks are kept exactly. I'll publish with your text instead of the "
    "generated one. <code>/cancel</code> to back out."
)


def published(title: str, *, silent: bool = False) -> str:
    how = "quietly" if silent else "loud and clear"
    return (
        f"{ICON} <b>Live.</b>\n\n"
        f"🎬 <b>{esc(title)}</b> is on the main channel, posted {how}. Index is "
        "updated, buttons are wired, catalog's one bigger. Told you this part was easy."
    )


def scheduled(title: str, when_text: str) -> str:
    return (
        f"{ICON} <b>Locked in for {esc(when_text)}.</b>\n\n"
        f"🎬 <b>{esc(title)}</b> will go public then — nothing for you to do. "
        "I don't miss."
    )


def schedule_prompt(tz_label: str) -> str:
    """The 'when?' prompt, stamped with the admin's own timezone."""
    return (
        f"{ICON} <b>When should it drop?</b>\n\n"
        f"Send a time as <code>YYYY-MM-DD HH:MM</code> (24-hour) in <b>your</b> "
        f"timezone (<b>{esc(tz_label)}</b>). <code>/cancel</code> to back out.\n\n"
        "<i>The current queue is below so you don't land on top of another post.</i>"
    )


# Kept for back-compat; timezone-aware callers use ``schedule_prompt(...)``.
SCHEDULE_PROMPT = (
    f"{ICON} <b>When should it drop?</b>\n\n"
    "Send a time as <code>YYYY-MM-DD HH:MM</code> (24-hour, your timezone). "
    "<code>/cancel</code> to back out."
)


def schedule_bad_time(raw: str) -> str:
    return (
        f"{ICON} <b>That time doesn't parse.</b> I read <code>{esc(raw)}</code> but "
        "I need <code>YYYY-MM-DD HH:MM</code> — and it has to be in the future."
    )


def schedule_table(rows: list[tuple[str, str]], tz_label: str) -> str:
    """A friendly list of every pending scheduled post in the admin's timezone.

    ``rows`` is ``[(when_text, title), …]`` already converted + sorted. All times
    are shown in the reading admin's zone so nobody has to do mental math.
    """
    if not rows:
        return (
            f"{ICON} <b>Nothing scheduled yet.</b> The queue is clear — "
            f"times shown in <b>{esc(tz_label)}</b>."
        )
    lines = [
        f"{ICON} <b>Scheduled queue</b>  <i>(all times in {esc(tz_label)})</i>",
        "",
    ]
    for when_text, title in rows:
        lines.append(f"🕒 <code>{esc(when_text)}</code> — <b>{esc(title)}</b>")
    lines.append("")
    lines.append(f"<i>{len(rows)} post{'s' if len(rows) != 1 else ''} queued across all admins.</i>")
    return "\n".join(lines)


def schedule_collision(rows: list[tuple[str, str]], tz_label: str) -> str:
    """Warn that the chosen time is close to an existing post."""
    clash = "\n".join(
        f"🕒 <code>{esc(w)}</code> — <b>{esc(t)}</b>" for w, t in rows
    )
    return (
        f"{ICON} <b>Heads up — that's crowded.</b>\n\n"
        f"There's already something near that slot (times in <b>{esc(tz_label)}</b>):\n"
        f"{clash}\n\n"
        "Send a different time to space them out, or <code>/cancel</code>."
    )


# ── Footer edit (universal) ──────────────────────────────────────────────────────

FOOTER_EDIT_PROMPT = (
    f"{ICON} <b>New footer, every channel.</b>\n\n"
    "Send the footer text — Telegram styling, Markdown, or raw HTML, line breaks "
    "kept. I'll rewrite the footer caption across every distribution channel we "
    "run, all at once. <code>/cancel</code> to back out."
)


def footer_updated(ok: bool, footers: int, bots: int = 0) -> str:
    if not ok:
        return (
            f"{ICON} <b>Nothing to rewrite.</b>\n\n"
            "That came through empty, so I left the footer as-is. Send the new "
            "text and I'll fan it out."
        )
    return (
        f"{ICON} <b>Footer rewritten.</b>\n\n"
        f"Updated <b>{footers}</b> footer post(s) across <b>{bots}</b> channel(s). "
        "Returning users get the new footer on their next start — future posts "
        "already use it."
    )


# ── Updates / maintenance ─────────────────────────────────────────────────────────

UPDATES_RUNNING = (
    f"{ICON} <b>Sweeping the catalog…</b> checking every franchise for finished "
    "seasons, movies, and extras that aren't up yet. Nothing's queued until you say so."
)

BANCHECK_RUNNING = (
    f"{ICON} <b>Probing channels…</b> pinging every distribution channel and the main "
    "one to see who's still reachable."
)


def updates_found(count: int) -> str:
    n = "new entry" if count == 1 else "new entries"
    return (
        f"{ICON} <b>{count} {n} across the catalog.</b>\n\n"
        "Finished seasons, movies, and extras that aren't up yet. Review the list, "
        "trim or add whatever you want, then submit — I'll push each one back "
        "through the pipeline and update its channel when it's ready."
    )


UPDATES_NONE = (
    f"{ICON} <b>Everything's current.</b>\n\n"
    "Swept the whole catalog — no finished entries missing. Nothing to do."
)


def remove_entry_label(title: str) -> str:
    """A per-entry drop button — trimmed so the row stays readable."""
    short = title if len(title) <= 34 else title[:33].rstrip() + "…"
    return f"✖ {short}"


def entry_dropped(title: str) -> str:
    return f"Dropped {title} — won't be requested."

UPDATES_EDIT_PROMPT = (
    f"{ICON} <b>Edit the list.</b>\n\n"
    "Copy the text below, remove any lines you don't want, and add new ones if you "
    "like — one per line. For adds, use the <b>official AniList English title</b> so "
    "I match the right entry. Send it back when it's ready. <code>/cancel</code> to back out."
)


def updates_unresolved(titles: list[str]) -> str:
    """Warn that some hand-added titles couldn't be matched on AniList."""
    listing = "\n".join(f"• {t}" for t in titles)
    n = "title" if len(titles) == 1 else "titles"
    return (
        f"{ICON} <b>Couldn't match {len(titles)} {n}.</b>\n\n"
        f"<pre>{listing}</pre>\n"
        "Check the spelling against the official AniList English title. "
        "The rest of the list is ready below."
    )


def updates_submitted(count: int) -> str:
    n = "entry" if count == 1 else "entries"
    return (
        f"{ICON} <b>{count} {n} back in the pipeline.</b>\n\n"
        "Each one runs the normal course — download, thumbnail, then its channel "
        "gets the new card. No main-channel repost; these just extend what's already up."
    )


# The monthly sweep pushes this before the reviewable list (detect-only — nothing
# is requested until the admin taps Submit).
UPDATES_SCHEDULED_INTRO = (
    f"{ICON} <b>Monthly update sweep.</b>\n\n"
    "I checked every published franchise for finished entries that aren't up yet. "
    "Review the list below — drop anything you don't want, add titles I missed, "
    "then hit <b>Submit</b>. Nothing's requested until you do."
)


def bancheck_scheduled_summary(
    checked: int, banned: int, recovered: list[str],
) -> str:
    """Monthly ban-check result DM: what was probed, down, and auto-recovered."""
    if not banned:
        return (
            f"{ICON} <b>Monthly ban check — all clear.</b>\n\n"
            f"Probed {checked} channels, every one reachable."
        )
    lines = [
        f"{ICON} <b>Monthly ban check.</b>\n",
        f"Probed {checked} · <b>{banned} down</b>.",
    ]
    if recovered:
        listing = "\n".join(f"• {name}" for name in recovered)
        lines.append(
            f"\n♻️ Auto-recovered {len(recovered)} distribution "
            f"{'channel' if len(recovered) == 1 else 'channels'}:\n{listing}"
        )
    down_unrecovered = banned - len(recovered)
    if down_unrecovered > 0:
        lines.append(
            f"\n⚠️ {down_unrecovered} need a manual move "
            "(main/index channel) — use Change Main / Change Index."
        )
    return "\n".join(lines)


# ── Stats dashboard ────────────────────────────────────────────────────────────────

STATS_RUNNING = f"{ICON} <b>Crunching the numbers…</b>"

STATS_FAILED = (
    f"{ICON} <b>Couldn't build the stats screen.</b>\n\n"
    "Something hiccuped gathering the counts — try again in a moment."
)


# ── Ban check / recovery ──────────────────────────────────────────────────────────

def ban_check_result(banned: int, checked: int) -> str:
    if not banned:
        return (
            f"{ICON} <b>All clear.</b>\n\n"
            f"Checked {checked} channels — every one's reachable. Nothing's down."
        )
    n = "channel" if banned == 1 else "channels"
    return (
        f"{ICON} <b>{banned} {n} down.</b>\n\n"
        f"Out of {checked} checked. I've pinged Senku to rebuild them — once the new "
        "channel's up, I repost everything from backup, exactly as it was. No re-render."
    )


def ban_recovered(old_name: str, new_handle: str) -> str:
    return (
        f"{ICON} <b>{old_name} — back up.</b>\n\n"
        f"Rebuilt as <b>@{new_handle}</b> and every backed-up post is restored, "
        "buttons repointed across the main and index channels. Nothing re-rendered."
    )


def ban_recover_failed(name: str, error: str) -> str:
    return (
        f"{ICON} <b>{name} — recovery stalled.</b>\n\n"
        f"<code>{error[:300]}</code>\n\n"
        "The backup's intact, so it's safe to retry from the task's 🛡 Recover button."
    )


def recovered(title: str) -> str:
    return (
        f"{ICON} <b>Back from backup.</b>\n\n"
        f"🎬 <b>{esc(title)}</b> is restored — every card, divider, and footer posted "
        "again just as it was. Buttons across the main channel and index point at the "
        "new channel now."
    )


# ── Backup / restore ──────────────────────────────────────────────────────────────

BACKUP_RUNNING = f"{ICON} <b>Backing up…</b> snapshotting every post + mirroring images."


def backup_done(backed_up: int, total: int, mirrored: int) -> str:
    return (
        f"{ICON} <b>Catalog backed up.</b>\n\n"
        f"Snapshotted <b>{backed_up}</b> of <b>{total}</b> posts — caption, buttons, "
        f"dividers, all of it. <b>{mirrored}</b> image(s) mirrored to durable hosts so "
        "they outlive the original channel. If we ever get banned, I rebuild from this "
        "byte-for-byte, no re-render."
    )


BACKUP_EMPTY = (
    f"{ICON} <b>Nothing to back up yet.</b>\n\n"
    "No live main-channel posts on the board. Publish something first."
)


# ── Change main channel / restore ──────────────────────────────────────────────────

CHANGE_MAIN_PROMPT = (
    f"{ICON} <b>New main channel.</b>\n\n"
    "Send the new channel's ID (like <code>-1001234567890</code>). Make me an admin "
    "there first. I'll repost every saved card from backup — same caption, photo, "
    "buttons, dividers — and repoint the ID. <code>/cancel</code> to back out."
)


def change_main_bad(raw: str) -> str:
    return (
        f"{ICON} <b>That's not a channel ID.</b> I need a numeric id like "
        f"<code>-1001234567890</code>, not “{esc(raw)}”. Try again."
    )


RESTORE_RUNNING = (
    f"{ICON} <b>Restoring from backup…</b>\n\n"
    "Reposting every saved card to the new channel. No re-render — straight from "
    "the snapshot. This can take a bit on a big catalog."
)


def restore_done(restored: int, total: int, failed: int) -> str:
    tail = f" <b>{failed}</b> couldn't be reposted — check the logs." if failed else ""
    return (
        f"{ICON} <b>Channel restored.</b>\n\n"
        f"Reposted <b>{restored}</b> of <b>{total}</b> saved posts and repointed the "
        f"main-channel ID.{tail}"
    )


# ── Index channel management ──────────────────────────────────────────────────

CHANGE_INDEX_PROMPT = (
    f"{ICON} <b>New index channel.</b>\n\n"
    "Send the new channel's ID (like <code>-1001234567890</code>). Make me an admin "
    "there first. I'll rebuild the whole index from backup — poster, every letter "
    "slot, reserved slots — remap each slot's message id and repoint the config. "
    "<code>/cancel</code> to back out."
)

INDEX_INTRO = f"{ICON} <b>Index slots.</b> Loading the current layout…"

# Button labels for the slot-management UI.
BTN_CHANGE_INDEX = "🆕 Change Index"
BTN_IDX_BACK = "◀ Slots"
BTN_IDX_EDIT_TEXT = "✏️ Edit text"
BTN_IDX_EDIT_IMAGE = "🖼 Replace image"
BTN_IDX_EDIT_BUTTONS = "🔘 Edit buttons"
BTN_IDX_MARK_REPURPOSED = "🚫 Not a slot"
BTN_IDX_MARK_RESERVED = "♻️ Mark reserved"

_IDX_KIND_ICON = {"letter": "🔤", "reserved": "▫️", "repurposed": "🚫"}


def index_slots_body(slots: list[dict]) -> str:
    """Render the slot list: order, kind icon, label/state, per-slot tap hint."""
    if not slots:
        return (
            f"{ICON} <b>No index slots tracked.</b>\n\n"
            "The index channel hasn't been seeded yet, or it's disabled."
        )
    lines = [f"{ICON} <b>Index slots</b> — tap one to edit.\n"]
    for s in slots:
        icon = _IDX_KIND_ICON.get(s["kind"], "▫️")
        if s["kind"] == "letter":
            what = f"letter <b>{esc(s.get('label') or '?')}</b>"
        elif s["kind"] == "repurposed":
            what = "<i>repurposed — auto-index skips this</i>"
        else:
            what = "<i>reserved</i>"
        lines.append(f"{icon} <code>#{s['order']}</code> — {what}")
    lines.append(
        "\n<blockquote>A slot marked <b>repurposed</b> is treated as a normal "
        "post: auto-indexing never overwrites or shifts it.</blockquote>"
    )
    return "\n".join(lines)


def index_slot_detail(order: int) -> str:
    return (
        f"{ICON} <b>Slot #{order}.</b>\n\n"
        "Edit its text, replace its image, or set its buttons. You can also mark it "
        "as <b>not a slot</b> (a normal post auto-indexing will leave alone) or hand "
        "it back to the reserved pool."
    )


def index_edit_prompt(what: str) -> str:
    if what == "caption":
        return (
            f"{ICON} <b>New slot text.</b>\n\n"
            "Send the replacement text (Markdown or HTML). <code>/cancel</code> to back out."
        )
    if what == "image":
        return (
            f"{ICON} <b>New slot image.</b>\n\n"
            "Send a photo, or paste an image URL / Telegram file_id. "
            "<code>/cancel</code> to back out."
        )
    return (
        f"{ICON} <b>Slot buttons.</b>\n\n"
        "Send one button per line as <code>Label | https://link</code>. "
        "Send <code>none</code> to clear all buttons. <code>/cancel</code> to back out."
    )


def index_edit_result(ok: bool, what: str) -> str:
    if ok:
        return f"{ICON} <b>Slot {what} updated.</b>"
    return (
        f"{ICON} <b>Couldn't update the slot {what}.</b> The slot may be missing or "
        "the index channel unreachable — check the logs."
    )


def index_buttons_result(ok: bool, count: int) -> str:
    if not ok:
        return (
            f"{ICON} <b>Couldn't set the buttons.</b> Check the slot exists and the "
            "index channel is reachable."
        )
    if count == 0:
        return f"{ICON} <b>Buttons cleared.</b>"
    n = "button" if count == 1 else "buttons"
    return f"{ICON} <b>Set {count} {n}.</b>"


index_image_bad = (
    f"{ICON} <b>No image found.</b> Send a photo, or paste an image URL / file_id."
)


# ── Errors / misc ───────────────────────────────────────────────────────────────

GENERIC_FAIL = (
    f"{ICON} Something misfired. Not a problem I can't handle — check the logs and "
    "run it again."
)

NO_TASK = (
    f"{ICON} <b>That one's not on my board.</b> It isn't in my publish queue — it "
    "may not have finished distribution yet."
)


def fail(reason: str) -> str:
    return f"{ICON} <b>That didn't land:</b> {esc(reason)}"


def task_aborted(title: str) -> str:
    """Abort-but-keep: the task is parked, not deleted — still in /tasks."""
    return (
        f"{ICON} <b>Aborted.</b> {esc(title)} is parked — not gone. The task is still "
        "in your list, exactly where you left it. Open it whenever you want to pick it "
        "back up."
    )


# ── Button labels ───────────────────────────────────────────────────────────────

BTN_PUBLISH_NOW = "🚀 Publish Now"
BTN_PUBLISH_SILENT = "🔕 Silent Publish"
BTN_SCHEDULE = "📅 Schedule"
BTN_EDIT_CAPTION = "✏️ Edit Caption"
BTN_CANCEL = "✗ Cancel"

BTN_TASKS = "📋 My Titles"
BTN_OPEN_TASKS = "📋 Open Tasks"
BTN_HOME = "⇐ Home"
BTN_BACK = "⇐ Back"
BTN_SETTINGS = "⚙️ Settings"
BTN_HELP = "❔ How it works"

BTN_CHECK_UPDATES = "🔁 Check Updates"
BTN_CHECK_BANNED = "🛡 Check Banned"
BTN_EDIT_FOOTER = "✏️ Edit Footer (all channels)"
BTN_EDIT_LIST = "✏️ Edit list"
BTN_SUBMIT = "✅ Submit"
BTN_STATS = "📊 Stats"
BTN_RECOVER = "🛡 Recover"
BTN_CHANGE_MAIN = "📡 Change Main Channel"
BTN_BACKUP_NOW = "💾 Back up catalog"
BTN_RESTORE = "♻️ Restore to new channel"
