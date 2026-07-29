"""Levi Ackerman — the downloader bot's voice.

Every user-facing line Levi speaks lives here so his tone stays consistent and
can be re-tuned in one place. He is Humanity's Strongest Soldier: clipped,
exacting, allergic to waste and filth. He speaks in short declaratives, treats a
download job like a mission with no room for sloppiness, and measures everything
by whether it's *clean*. First person, never flowery, never breaking character.
Generic NekoFetch copy stays in the JSON catalog (``localization.messages``);
this module holds only what is distinctly *Levi*.

All strings are authored as Telegram HTML (the pipeline's default parse mode).
Callables take runtime values and return finished HTML; plain strings are used
as-is. Handlers reference these — never inline character copy — so a rewrite of
his voice is a single-file edit. Mirrors ``lelouch_voice`` structurally.
"""

from __future__ import annotations

import html

ICON = "⚔️"  # the blades that head every Levi card


def esc(text: str) -> str:
    """HTML-escape a runtime value before it lands in a caption."""
    return html.escape(str(text or ""), quote=False)


# ── Welcome / home ────────────────────────────────────────────────────────────

def home_title(name: str) -> str:
    who = esc(name) or "soldier"
    return f"{ICON} <b>Levi Ackerman</b> — reporting for the download detail, {who}."


HOME_BODY = (
    "<i>\"If you don't want to die, then think.\"</i>\n\n"
    "This is where the mess gets cleaned up. Lelouch hands me the requests; I "
    "run them down at the source, drag the files back fast, and hand you a pack "
    "that's renamed, branded, and spotless. Pick a task. We don't waste motion "
    "here."
)

HOME_ADMIN_TAG = (
    "<blockquote>Every job you take, you own it end to end. Source, download, "
    "rename, verify. Do it right the first time.</blockquote>"
)


# ── Task list ─────────────────────────────────────────────────────────────────

TASKS_EMPTY = (
    f"{ICON} <b>Nothing on the board.</b>\n\n"
    "No download tasks assigned to you. Rare quiet. Don't get used to it — "
    "the moment a request clears intake, it lands here and we move."
)


def tasks_title(count: int) -> str:
    n = "task" if count == 1 else "tasks"
    return f"{ICON} <b>{count} {n}</b> on your detail. Pick one and let's move."


# ── Request card (the job) ────────────────────────────────────────────────────

def request_card(title: str, code: str, requester: str | None = None) -> str:
    lines = [
        f"{ICON} <b>{esc(title)}</b>",
        f"<code>{esc(code)}</code>",
    ]
    if requester:
        lines.append(f"Requested by {esc(requester)}")
    lines.append(
        "\nReport first if you want the terrain. Pick Website, Torrent, or Telegram "
        "when you're ready to move. If one source gets dirty, back out and take another."
    )
    return "\n".join(lines)


# ── Franchise-map selection ───────────────────────────────────────────────────

FRANCHISE_SELECT_BODY = (
    "This is the whole franchise laid out. Everything's checked by default — "
    "tap to drop what you don't want. Filler recaps and clip shows are already "
    "left out; keep the canon and the entries worth keeping. When it's clean, "
    "hit <b>Continue</b>."
)


def franchise_selected_count(n: int, total: int) -> str:
    return f"Selected <b>{n}</b> of {total} entries."


# ── Source picking ────────────────────────────────────────────────────────────

SOURCE_PICK_BODY = (
    "Pick the route. Website opens the report and source order. Torrent is direct. "
    "Telegram is manual. Backing out changes the route; it never kills the request."
)

# The fixed manual note for Telegram — always the same, drilled in.
TELEGRAM_NOTE = (
    "<b>Telegram — manual only.</b>\n"
    "Search Telegram for the title. Find a channel serving the files with at "
    "least three resolutions. Before you take anything: run it through the "
    "screenshot bot to pull MediaInfo and a frame.\n\n"
    "• Watermark <i>burned into the video</i> → discard it. We don't ship "
    "someone else's brand.\n"
    "• Metadata tags → We will strip them.\n"
    "Only clean files come through."
)

# AniZone gets a blunt warning — it's the ambiguous one.
ANIZONE_WARNING = (
    "<blockquote expandable>⚠️ <b>Don't reach for AniZone unless you have to.</b>\n"
    "It doesn't follow a consistent pattern — entry naming is all over the "
    "place, so automating a clean match is a fight. If another source covers "
    "this, take that instead. If you must use AniZone, expect to connect the "
    "dots by hand.</blockquote>"
)


def source_suggestion(source: str, reason: str) -> str:
    return f"<b>My call:</b> {esc(source)} — {esc(reason)}"


# ── Download progress ─────────────────────────────────────────────────────────

def download_started(source: str, count: int) -> str:
    n = "file" if count == 1 else "files"
    return (
        f"{ICON} <b>Moving.</b> Pulling {count} {n} from {esc(source)}. "
        "I'll report when it's down."
    )


DOWNLOAD_DONE = (
    f"{ICON} <b>Down and accounted for.</b> Files are in, sorted into packs. "
    "Now we clean them up."
)


# ── Rename / short title ──────────────────────────────────────────────────────

def short_title_prompt(title: str) -> str:
    return (
        f"{ICON} <b>Title's long:</b> {esc(title)}\n\n"
        "A name that size reads like garbage in a filename. Pick a short form — "
        "an acronym, one of the known alt titles, or give me your own. Keep it "
        "clean."
    )


RENAME_INTRO = (
    "Here's how the files are named now, and the template I'd rename them with. "
    "Use the default for every pack, just this one, or mark each pack's type "
    "first — some want the season format, some the movie or special one."
)


# ── Media-info verify ─────────────────────────────────────────────────────────

MEDIAINFO_VERIFY = (
    f"{ICON} <b>Check my work.</b>\n\n"
    "Each pack's below with its MediaInfo. Confirm the audio and the numbering "
    "are right — if I called something dual when it's multi, or misread a "
    "season, say so and I'll re-cut the whole pack. I don't ship wrong."
)


# ── Thumbnail / caption ───────────────────────────────────────────────────────

THUMBNAIL_PROMPT = (
    f"{ICON} <b>Send the thumbnail.</b> One image, cropped square — 1:1. "
    "That's what goes on every file in the pack."
)

CAPTION_INTRO = (
    "This is the header that rides above the packs. Looks right? Ship it. "
    "Want it changed? Edit it — I'll show you the variables you can use."
)


def upload_done(title: str) -> str:
    return (
        f"{ICON} <b>Done. Clean.</b> {esc(title)} is in the database and handed "
        "down the line. Next job's up if you've got one."
    )


# ── Errors / misc ─────────────────────────────────────────────────────────────

GENERIC_FAIL = (
    f"{ICON} Something broke mid-job. That's on the mess, not on you — "
    "check the logs and run it again."
)


# ── Button labels ─────────────────────────────────────────────────────────────

BTN_REPORT = "📊 Generate Report"
BTN_ASSIGN = "🎯 Assign Source"
BTN_TASKS = "📋 My Tasks"
BTN_HOME = "⇐ Home"
BTN_BACK = "⇐ Back"
BTN_CONTINUE = "✓ Continue"
BTN_SELECT_ALL = "☑ Select all"
BTN_SELECT_NONE = "☐ Clear all"
BTN_SETTINGS = "⚙️ Settings"
BTN_HELP = "❔ How it works"

BTN_SRC_TELEGRAM = "✈️ Telegram (manual)"
BTN_SRC_KICKASS = "🅰️ KickAss Anime"
BTN_SRC_ANIKOTO = "🅱️ AniKoto"
BTN_SRC_ANIZONE = "🅾️ AniZone"
BTN_SRC_TORRENT = "🧲 Torrent"
BTN_SRC_DDL = "🔗 Direct Link (DDL)"

BTN_MEDIAINFO = "🎞 Media Info"
BTN_CONFIRM = "✓ Confirm"
BTN_EDIT = "✎ Edit"
BTN_CANCEL = "✗ Cancel"
