"""Senku Ishigami — the distribution bot's voice.

Every user-facing line Senku speaks lives here so his tone stays consistent and
can be re-tuned in one place. He is the scientist of the pipeline: exuberant,
exacting, relentlessly curious. He treats each distribution step like an
experiment with a measurable, reproducible result — logos, posters, backdrops,
watch order, the final published card. Ten billion percent precision, delivered
with a grin. First person, a little emoji (🧪⚗️🔬📊), never breaking character.
Generic NekoFetch copy stays in the JSON catalog (``localization.messages``);
this module holds only what is distinctly *Senku*.

All strings are authored as Telegram HTML (the pipeline's default parse mode).
Callables take runtime values and return finished HTML; plain strings are used
as-is. Handlers reference these — never inline character copy — so a rewrite of
his voice is a single-file edit. Mirrors ``levi_voice`` / ``lelouch_voice``
structurally.
"""

from __future__ import annotations

import html

ICON = "🧪"  # the flask that heads every Senku card


def esc(text: str) -> str:
    """HTML-escape a runtime value before it lands in a caption."""
    return html.escape(str(text or ""), quote=False)


# ── Welcome / home ────────────────────────────────────────────────────────────

def home_title(name: str) -> str:
    who = esc(name) or "researcher"
    return f"{ICON} <b>Senku Ishigami</b> — distribution lab's open, {who}."


HOME_BODY = (
    "<i>\"Ten billion percent — this channel will be perfect.\"</i>\n\n"
    "Levi hands me a finished pack; I turn it into a channel people actually want "
    "to open. Build the channel, forge the thumbnails, lock the watch order, then "
    "publish the info card. Every step is reproducible. Every result is clean. "
    "Pick a title and let's run the experiment."
)

HOME_ADMIN_TAG = (
    "<blockquote>Distribution is method, not vibes. Follow each step, confirm each "
    "result, and the channel comes out identical every single time.</blockquote>"
)


# ── Task list ─────────────────────────────────────────────────────────────────

TASKS_EMPTY = (
    f"{ICON} <b>No experiments queued.</b>\n\n"
    "Nothing assigned to the distribution lab right now. The instant Levi finishes "
    "a pack it lands here — and then the real fun starts."
)


def tasks_title(count: int) -> str:
    n = "title" if count == 1 else "titles"
    return f"{ICON} <b>{count} {n}</b> ready for distribution. Pick one — let's get exhilarated."


# ── Handoff / franchise intro card ──────────────────────────────────────────────

def handoff_card(title: str, code: str, entry_count: int | None = None) -> str:
    lines = [
        f"{ICON} <b>{esc(title)}</b>",
        f"<code>{esc(code)}</code>",
    ]
    if entry_count:
        unit = "entry" if entry_count == 1 else "entries"
        lines.append(f"📊 Franchise map: <b>{entry_count} {unit}</b>")
    lines.append(
        "\nDownloaded, renamed, spotless. Now we distribute. Tap <b>Begin</b> and I'll "
        "walk you through it one measured step at a time."
    )
    return "\n".join(lines)


def franchise_map_card(title: str, tree_html: str) -> str:
    return (
        f"{ICON} <b>{esc(title)}</b> — the full franchise, mapped.\n\n"
        f"{tree_html}\n\n"
        "<i>This is the watch order we'll distribute in. Confirmed canonical entries "
        "only — spin-offs and recaps stay out of the sequence.</i>"
    )


# ── Channel-creation step ───────────────────────────────────────────────────────

def channel_intro(title: str) -> str:
    return (
        f"{ICON} <b>Step 1 — Build the channel</b>\n\n"
        f"We're setting up the distribution channel for <b>{esc(title)}</b>. "
        "I'll hand you every piece you need in order — title, poster, description, "
        "admins. Do them top to bottom, then tell me you're done."
    )


def channel_scope_prompt(title: str) -> str:
    return (
        f"{ICON} <b>Step 1 — Who creates the channel?</b>\n\n"
        f"Setting up the distribution channel for <b>{esc(title)}</b>. Two ways:\n\n"
        "👤 <b>You create it</b> — you make the channel and add our bots as admins. "
        "I'll hand you the title, description and username to paste.\n\n"
        "🤖 <b>A user-bot creates it</b> — a pooled user-bot account makes it, sets the "
        "title, description and username itself. You only add the profile photo.\n\n"
        "<blockquote>⚠️ Prefer creating it yourself. Only let a user-bot do it once your "
        "own account has hit the channel-creation limit — user-bot accounts have a hard "
        "cap too, so we save them for when you're out of room.</blockquote>"
    )


def recovery_channel_prompt(title: str, old_name: str | None = None) -> str:
    """The normal channel setup card, reworded for a banned-channel replacement.

    Recovery deliberately uses Senku's voice and channel-creation vocabulary so
    an operator who already knows the normal wizard does not get a second UI to
    learn. The caller supplies the inline buttons and the recurring artwork.
    """
    old = f"\n\n<b>Previous channel:</b> {esc(old_name)}" if old_name else ""
    return (
        f"{ICON} <b>Restoring a banned channel</b>{old}\n\n"
        f"The old distribution channel for <b>{esc(title)}</b> is unavailable. "
        "Create a <b>public replacement channel</b> (or use the user-bot option), "
        "add <b>Senku</b> and <b>Gojo</b> as administrators with full rights, set a "
        "clean profile picture, and remove the Telegram service messages about the "
        "channel/photo being changed.\n\n"
        "When that is done, send me the replacement <b>@username</b>, link, or numeric "
        "ID. I'll verify both bots, restore the saved cards, relink the Download "
        "buttons, and notify the main post."
    )


def recovery_waiting() -> str:
    return (
        f"{ICON} <b>Replacement channel assigned.</b>\n\n"
        "Create the channel using the recovery card, then send its @username or ID "
        "back to me. The saved content stays untouched until I verify the new channel."
    )


def recovery_done(handle: str) -> str:
    return (
        f"{ICON} <b>Recovery complete.</b>\n\n"
        f"The replacement <b>{esc(handle)}</b> is verified, restored, and linked into "
        "the catalog. The old channel was never used for new posts."
    )


def recovery_failed(handle: str, reason: str) -> str:
    return (
        f"{ICON} <b>Recovery is not ready.</b>\n\n"
        f"I couldn't accept <b>{esc(handle)}</b>: {esc(reason)}\n\n"
        "Add both bots as administrators and send the replacement handle again."
    )


CHANNEL_SCOPE_NO_USERBOT = (
    f"{ICON} <b>No bot slots left.</b>\n\n"
    "Every pooled account is at its channel cap, so I can't have one create this. "
    "Create the channel yourself and add our bots as admins — tap the other option."
)


def userbot_creating(title: str) -> str:
    return (
        f"{ICON} <b>Creating the channel…</b>\n\n"
        f"Spinning up the distribution channel for <b>{esc(title)}</b> — setting the "
        "title, username and description. One moment."
    )


def userbot_created(handle: str, invite_link: str | None) -> str:
    link = f"\n\n🔗 <a href=\"{esc(invite_link)}\">Open the channel</a>" if invite_link else ""
    return (
        f"{ICON} <b>Channel's up.</b> I created <b>{esc(handle)}</b> and set its title, "
        f"username and description.{link}\n\n"
        "🖼 <b>Your turn:</b> open it, set a profile photo (pick one you did <b>not</b> "
        "use as a file thumbnail), and delete the “channel photo changed” service "
        "message so the feed stays clean. Then tap <b>Done</b>."
    )


def userbot_join(handle: str, invite_link: str | None) -> str:
    link = f"\n\n🔗 <a href=\"{esc(invite_link)}\">Join the channel</a>" if invite_link else ""
    return (
        f"{ICON} <b>Channel's up.</b> I created <b>{esc(handle)}</b> and set its title, "
        f"username and description. I've also added myself and Gojo as admins.{link}\n\n"
        "👉 <b>First, join the channel</b> using the link above — I can only hand you "
        "admin rights once you're in. Then tap <b>I've joined</b>."
    )


def userbot_promote_failed() -> str:
    return (
        f"{ICON} <b>I can't see you in the channel yet.</b>\n\n"
        "Make sure you tapped the join link and actually joined, then hit "
        "<b>I've joined</b> again. I need you inside before I can give you admin rights."
    )


def userbot_set_photo() -> str:
    return (
        f"{ICON} <b>You're an admin now.</b> 🖼\n\n"
        "Open the channel and set its <b>profile picture</b> (pick one you did "
        "<b>not</b> use as a file thumbnail), then delete the “channel photo changed” "
        "service message so the feed stays clean. Tap <b>Done</b> when it's set."
    )


CHANNEL_USERBOT_FAILED = (
    f"{ICON} <b>Couldn't create it.</b>\n\n"
    "The pooled account hit a snag (flood-wait or a cap we didn't see). Create the "
    "channel yourself instead and add our bots as admins — tap the other option."
)


def channel_create_card(name: str, candidates: list[str]) -> str:
    """Step 1 — everything the admin needs to CREATE the channel in one card:
    a name suggestion + a menu of valid @username options to pick from."""
    lines = [
        f"{ICON} <b>Create the channel</b>\n",
        "① <b>Name</b> — tap to copy:",
        f"<code>{esc(name)}</code>",
        "<i>(I'll set the final decorated title myself later — this is just so the "
        "channel exists.)</i>\n",
        "② <b>Public link</b> — Telegram won't hand out an exact @handle, so pick "
        "one of these (they're built from the title + <code>axw</code>):",
    ]
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  <b>{i}.</b> <code>{c}</code>")
    lines.append(
        "\nCreate a <b>public channel</b> with one of those usernames, then tap "
        "<b>Next</b>."
    )
    return "\n".join(lines)


def channel_pfp_line() -> str:
    return (
        "🖼 <b>Add the profile picture</b>\n"
        "Open the TMDB poster page below, download a clean poster, and set it as the "
        "channel photo.\n\n"
        "<i>Tip: after setting it, delete Telegram's \"channel photo updated\" service "
        "message so the channel stays clean.</i>\n\n"
        "Tap <b>Next</b> once the photo's on."
    )


CHANNEL_ADMINS_LINE = (
    "👥 <b>Add both bots as admins</b>\n"
    "Add <b>Senku</b> (me) and <b>Gojo</b> as administrators with <b>all rights</b>. "
    "I post the info card, watch guide and set the title/description; Gojo handles "
    "publishing. Without admin rights, neither of us can touch it.\n\n"
    "Tap <b>Next</b> when we're both admins."
)


def channel_missing(what: str) -> str:
    return (
        f"{ICON} <b>Not so fast.</b> I still need: {esc(what)}. "
        "Fix that and send the link again — I don't publish half-built experiments."
    )


CHANNEL_ASK_LINK = (
    f"{ICON} <b>Send the channel link</b>\n\n"
    "Paste the channel's <b>link</b> (<code>t.me/yourchannel</code>), its "
    "<b>@username</b>, or numeric ID. I'll confirm both bots are admins, then set "
    "the title and description myself. Reply /cancel to abort."
)


# ── Edit Posts (link-based post editor) ──────────────────────────────────────

POSTEDIT_ASK_LINK = (
    f"{ICON} <b>Edit a post</b>\n\n"
    "Paste the <b>link</b> of the post you want to change — copy it from the "
    "distribution channel (<code>t.me/c/…/123</code> for a private channel, or "
    "<code>t.me/yourchannel/123</code> for a public one).\n\n"
    "I'll make sure it's a channel I can edit, then let you rewrite the "
    "caption, swap the buttons, or replace the image."
)


def postedit_bad_link() -> str:
    return (
        f"{ICON} <b>That's not a post link I can read.</b>\n\n"
        "Send a message link like <code>https://t.me/c/1699000000/42</code> or "
        "<code>https://t.me/yourchannel/42</code>. Tap a post → <i>Copy Link</i>."
    )


def postedit_not_mine() -> str:
    return (
        f"{ICON} <b>I can't edit that one.</b>\n\n"
        "That post is in a channel where I'm not an admin (or it isn't one of my "
        "distribution channels). Add me as an admin with edit rights, then send "
        "the link again."
    )


def postedit_choose(preview: str) -> str:
    body = esc(preview[:120]) if preview else "<i>(no caption)</i>"
    return (
        f"{ICON} <b>Found it.</b>\n\n"
        f"Current caption: {body}\n\n"
        "What do you want to change?"
    )


POSTEDIT_ASK_CAPTION = (
    f"{ICON} <b>Send the new caption.</b>\n\n"
    "HTML, Markdown or Telegram-formatted text all work — I render it properly "
    "and save it. Reply /cancel to keep the current one."
)


def postedit_ask_caption(current_html: str | None) -> str:
    """Ask-for-caption prompt that shows the CURRENT caption in RAW HTML.

    Staff copy the source out of the code block, tweak it, and paste it back —
    so the exact markup is visible, not a rendered/truncated preview. Falls back
    to the plain prompt when the current caption couldn't be read.
    """
    if not current_html:
        return POSTEDIT_ASK_CAPTION
    shown = esc(current_html[:3500])
    return (
        f"{ICON} <b>Send the new caption.</b>\n\n"
        "Here is the current caption in raw HTML — copy, edit and paste it back:\n"
        f"<pre>{shown}</pre>\n"
        "HTML, Markdown or Telegram-formatted text all work. Reply /cancel to keep it."
    )


def postedit_caption_working() -> str:
    return f"{ICON} <b>Updating the caption…</b>"


def postedit_caption_done(ok: bool, detail: str) -> str:
    head = "✅ <b>Caption updated.</b>" if ok else "⚠️ <b>Couldn't update the caption.</b>"
    return f"{head}\n\n{esc(detail)}"


# Generic working/done cards for the image + buttons replace flows, so all three
# post-edit modes share the same consume → working → done → back-to-menu feel.
_POSTEDIT_NOUNS = {
    "image": ("Replacing the image…", "Image replaced.", "Couldn't replace the image."),
    "buttons": ("Updating the buttons…", "Buttons updated.", "Couldn't update the buttons."),
    "caption": ("Updating the caption…", "Caption updated.", "Couldn't update the caption."),
}


def postedit_working(mode: str) -> str:
    working, _, _ = _POSTEDIT_NOUNS.get(mode, _POSTEDIT_NOUNS["caption"])
    return f"{ICON} <b>{working}</b>"


def postedit_done(mode: str, ok: bool, detail: str) -> str:
    _, ok_head, fail_head = _POSTEDIT_NOUNS.get(mode, _POSTEDIT_NOUNS["caption"])
    head = f"✅ <b>{ok_head}</b>" if ok else f"⚠️ <b>{fail_head}</b>"
    return f"{head}\n\n{esc(detail)}"


POSTEDIT_ASK_BUTTONS = (
    f"{ICON} <b>Send the buttons.</b>\n\n"
    "One per line as <code>Label | https://link</code>. Send <code>none</code> "
    "to remove every button. Reply /cancel to leave them as they are."
)

POSTEDIT_ASK_IMAGE = (
    f"{ICON} <b>Send the new image.</b>\n\n"
    "Upload a photo (or an image file) and I'll swap it into the post, keeping "
    "the caption and buttons as they are. Best for the info card, footer, or any "
    "picture post. Reply /cancel to keep the current image."
)


BTN_POSTEDIT_CAPTION = "📝 Edit caption"
BTN_POSTEDIT_BUTTONS = "🔘 Replace buttons"
BTN_POSTEDIT_IMAGE = "🖼 Replace image"


# ── Edit Thumbnail (Senku-native thumbnail editor) ───────────────────────────

def editthumb_intro() -> str:
    return (
        f"{ICON} <b>Edit a thumbnail</b>\n\n"
        "Pick the anime whose card you want to change. Every title I've stored is "
        "here — tap one to edit its values (synopsis, rating, genres, artwork) or "
        "regenerate it from scratch."
    )


def editthumb_empty() -> str:
    return (
        f"{ICON} <b>Nothing to edit yet.</b>\n\n"
        "No anime have been stored, so there are no thumbnails to work on."
    )


def editthumb_choose_surface(title: str) -> str:
    return (
        f"{ICON} <b>{esc(title)}</b>\n\n"
        "This franchise has several entries. Which thumbnail do you want to edit?\n\n"
        "• <b>Main-channel post</b> — the single card on the main channel "
        "(franchise synopsis, averaged rating).\n"
        "• <b>Distribution entries</b> — the per-season / per-extra cards in the "
        "distribution channel."
    )


def editthumb_entry_list(title: str) -> str:
    return (
        f"{ICON} <b>{esc(title)} — entries</b>\n\n"
        "Pick the entry to edit, or send a channel post link to jump straight to one."
    )


def editthumb_not_saved(label: str) -> str:
    return (
        f"{ICON} <b>{esc(label)} — not saved yet</b>\n\n"
        "This card was made before I started storing thumbnail values, so there's "
        "nothing to edit. <b>Regenerate</b> it first — pick a logo, poster and "
        "backdrop — and then every value becomes editable."
    )


def editthumb_edit_menu(label: str, fields: dict) -> str:
    """The edit page: show the current values, then a button per field."""
    def _line(name: str, value) -> str:
        shown = str(value).strip() if value not in (None, "") else "—"
        if len(shown) > 60:
            shown = shown[:59].rstrip() + "…"
        return f"• <b>{name}</b>: {esc(shown)}"

    rows = [
        _line("Synopsis", fields.get("synopsis")),
        _line("TMDB rating", fields.get("tmdb_rating")),
        _line("AniList score", fields.get("anilist_score")),
        _line("Genres", ", ".join(fields.get("genres") or [])
              if isinstance(fields.get("genres"), (list, tuple)) else fields.get("genres")),
        _line("Studio", fields.get("studio")),
        _line("Language", fields.get("language")),
    ]
    return (
        f"{ICON} <b>{esc(label)}</b>\n\n"
        + "\n".join(rows)
        + "\n\nTap a value to change it. I'll re-render the card and update the "
        "live post right away."
    )


def editthumb_ask_value(field_label: str) -> str:
    return (
        f"{ICON} <b>New {esc(field_label)}</b>\n\n"
        "Send the new value. For <b>genres</b>, separate them with commas. Reply "
        "/cancel to keep the current one."
    )


def editthumb_ask_link(title: str) -> str:
    return (
        f"{ICON} <b>{esc(title)} — send a post link</b>\n\n"
        "Paste the link of the distribution-channel post you want to edit. I'll "
        "confirm it belongs to this franchise's channel, then open its editor."
    )


def editthumb_synopsis_trimmed(shown: str) -> str:
    return (
        f"{ICON} That synopsis was longer than the card's space, so I trimmed it to "
        f"fit:\n\n<i>{esc(shown)}</i>"
    )


BTN_EDITTHUMB_MAIN = "🏠 Main-channel post"
BTN_EDITTHUMB_DIST = "📺 Distribution entries"
BTN_EDITTHUMB_REGEN = "🔄 Regenerate"
BTN_EDITTHUMB_PROVIDE_LINK = "🔗 Provide link"
BTN_EDITTHUMB_BACK = "⇐ Back"


def channel_setup_progress(steps: list[tuple[str, str]]) -> str:
    """A small progress card for the bot-driven finalisation (title, desc, etc.).

    ``steps`` is a list of ``(label, state)`` where state ∈ {"done","active",
    "todo"} — rendered like the download bar's stage list."""
    icon = {"done": "✅", "active": "⏳", "todo": "▫️"}
    lines = [f"{ICON} <b>Setting up the channel…</b>\n"]
    for label, state in steps:
        lines.append(f"{icon.get(state, '▫️')} {esc(label)}")
    return "\n".join(lines)


def channel_setup_done(handle: str, title_text: str) -> str:
    return (
        f"{ICON} <b>Channel is ready.</b>\n\n"
        f"<b>{esc(handle)}</b>\n"
        f"Title set to:\n<code>{esc(title_text)}</code>\n\n"
        "Now let's forge the thumbnails."
    )


def channel_verified(handle: str) -> str:
    return (
        f"{ICON} <b>Verified.</b> I can see <b>{esc(handle)}</b> and I've got admin rights. "
        "Onto the fun part — thumbnails."
    )


def channel_verify_failed(handle: str, missing: list[str] | None = None) -> str:
    if missing:
        who = " and ".join(missing)
        return (
            f"{ICON} <b>Almost — {esc(handle)} still needs {esc(who)} as admin.</b>\n\n"
            "Both <b>Senku</b> (me) and <b>Gojo</b> must be administrators: I post the "
            "info card + watch guide, Gojo runs publishing. Add the missing bot with "
            "full permissions, then <b>send the @username again</b> — the link field is "
            "still open."
        )
    return (
        f"{ICON} <b>Can't reach {esc(handle)}.</b> Either the handle's wrong or I'm not an "
        "admin there yet. Add me as admin, double-check the username, and <b>send the "
        "@username again</b> — the link field is still open."
    )


def channel_banned(handle: str) -> str:
    """Telegram rejected edits on the channel with CHAT_INVALID — the channel is
    banned/restricted/deleted and can't be used. Ask for a fresh one rather than
    pretending setup succeeded."""
    return (
        f"{ICON} <b>{esc(handle)} looks banned or restricted.</b> Telegram won't let me "
        "edit it (title/description rejected), which almost always means the channel is "
        "banned, restricted, or deleted — nothing I can fix from here.\n\n"
        "<b>Create a fresh channel and send me its @username (or invite link)</b> — the "
        "link field is still open. Make sure <b>Senku</b> (me) and <b>Gojo</b> are admins "
        "there with full permissions."
    )


# ── Thumbnail loop ──────────────────────────────────────────────────────────────

def thumb_intro(title: str, total: int) -> str:
    unit = "entry" if total == 1 else "entries"
    return (
        f"{ICON} <b>Step 2 — Forge the thumbnails</b>\n\n"
        f"<b>{esc(title)}</b> has <b>{total} {unit}</b>. For each one we pick a logo, a "
        "poster and a backdrop, then I render the card. We'll go in order — one clean "
        "result at a time."
    )


def thumb_entry_header(label: str, index: int, total: int) -> str:
    # Single entry → the label alone is enough (no redundant "Entry 1 / 1").
    # Multiple → keep the counter but put the label INLINE in brackets, not on a
    # separate line, so it reads "Entry 2 / 5 · (Season 2)" instead of stacking.
    if total <= 1:
        head = f"{ICON} <b>{esc(label)}</b>"
    else:
        head = f"{ICON} <b>Entry {index} / {total}</b>  ·  <b>{esc(label)}</b>"
    return (
        f"{head}\n\n"
        "Pick the assets below. Tap to open the gallery, then tap the number you want."
    )


def thumb_generate_header(label: str, index: int, total: int) -> str:
    """Header for the Generate-Thumbnail card — all assets are chosen, so there
    is NO gallery to open here (that wording belongs to the asset-pick card).
    Reuses the same title line but tells the admin the card is ready to render."""
    if total <= 1:
        head = f"{ICON} <b>{esc(label)}</b>"
    else:
        head = f"{ICON} <b>Entry {index} / {total}</b>  ·  <b>{esc(label)}</b>"
    return (
        f"{head}\n\n"
        "All assets picked. Tap <b>Generate Thumbnail</b> to render this card."
    )


def thumb_pick_prompt(asset: str) -> str:
    words = {"logo": "logo", "poster": "poster", "bg": "backdrop"}
    a = words.get(asset, asset)
    return (
        f"🔬 <b>Choose a {a}</b>\n"
        f"Open the gallery, find the {a} you like, and tap its number. "
        "The clean choice beats the flashy one nine times out of ten."
    )


def thumb_selected(asset: str, number: int) -> str:
    words = {"logo": "Logo", "poster": "Poster", "bg": "Backdrop"}
    return f"✅ {words.get(asset, asset.title())} #{number} locked in."


def thumb_text_prompt() -> str:
    return (
        f"{ICON} <b>Type your logo text</b>\n"
        "Send the words exactly as you want them to appear. You can use up to three "
        "short lines. Tap <b>Cancel</b> to return to the asset choices."
    )


def thumb_text_categories() -> str:
    return (
        f"{ICON} <b>Choose a lettering style</b>\n\n"
        "Pick a font family category. I'll show the best bundled free fonts in that "
        "style next."
    )


def thumb_text_fonts(category: str, fonts: list[str] | None = None) -> str:
    return (
        f"{ICON} <b>{esc(category)}</b>\n\n"
        "Choose a font to preview your logo. Every option here is bundled under the "
        "SIL Open Font License."
    )


def thumb_text_preview(font_name: str, text: str) -> str:
    return (
        f"{ICON} <b>Text logo preview</b>\n\n"
        f"Font: <b>{esc(font_name)}</b>\n"
        f"Text: <code>{esc(text)}</code>\n\n"
        "If it looks right, use it. Otherwise go back and choose another font."
    )


def thumb_text_weights(font_name: str, weights: list[str], italic: bool = False) -> str:
    mode = "on" if italic else "off"
    return (
        f"{ICON} <b>Choose text weight</b>\n\n"
        f"Font: <b>{esc(font_name)}</b>\n"
        "Pick a native weight — no fake bolding, no muddy glyphs.\n"
        + "\n".join(f"• {esc(label)}" for label in weights)
        + (f"\n\nItalic: <b>{mode}</b>" if weights else "")
    )


def thumb_text_reuse_prompt() -> str:
    return (
        f"{ICON} <b>Use the previously generated logo?</b>\n\n"
        "I found the latest logo from this request. It is shown above. "
        "Use it for this entry, or make a new one."
    )


def thumb_logo_reuse_prompt() -> str:
    return (
        f"{ICON} <b>Use the previously uploaded logo?</b>\n\n"
        "This request already has a logo from an earlier entry. It is shown above. "
        "Use it again, or upload a different logo for this entry."
    )


def thumb_text_error() -> str:
    return (
        f"{ICON} <b>I couldn't make that logo.</b>\n\n"
        "Use a short title with normal letters and try again. Your current asset "
        "choices are still safe."
    )


def thumb_text_colors() -> str:
    return (
        f"{ICON} <b>Pick the logo color</b>\n\n"
        "White and black lead, then the full spectrum. The swatch is the button "
        "— choose the fill and I'll render the preview."
    )


def thumb_text_font_upload_prompt() -> str:
    return (
        f"{ICON} <b>Send me a font file</b>\n\n"
        "Send a <code>.ttf</code> or <code>.otf</code> file and I'll use it for "
        "this logo only — nothing gets saved to the bundled set. Then we pick a "
        "color."
    )


def thumb_text_font_upload_bad() -> str:
    return (
        f"{ICON} <b>That's not a font file.</b>\n\n"
        "Send a <code>.ttf</code> or <code>.otf</code> file and I'll use it for "
        "this logo only."
    )


def thumb_upload_prompt(asset: str) -> str:
    words = {"logo": "logo", "poster": "poster", "bg": "backdrop"}
    a = words.get(asset, asset)
    return (
        f"📤 <b>Send your own {a}</b>\n"
        f"Drop the {a} image right here — as a photo or an image file. "
        "I'll wire it straight into the render."
    )


def thumb_uploaded(asset: str) -> str:
    words = {"logo": "Logo", "poster": "Poster", "bg": "Backdrop"}
    return f"✅ Your {words.get(asset, asset.title())} is locked in."


THUMB_UPLOAD_BAD = (
    "🔬 That's not an image. Send a photo or an image file, "
    "or tap a number from the gallery instead."
)
THUMB_UPLOAD_FAILED = (
    "⚗️ The upload host choked on that one. Try again, "
    "or pick a number from the gallery."
)


def thumb_generated(index: int, total: int) -> str:
    if index >= total:
        return (
            f"{ICON} <b>All thumbnails rendered.</b> Every entry's got a card. "
            "Now let's make sure the watch order is exactly right."
        )
    return (
        f"⚗️ <b>Rendered.</b> Entry {index} of {total} is done — "
        f"moving to entry {index + 1}."
    )


THUMB_GALLERY_FAIL = (
    f"{ICON} <b>Gallery didn't load.</b> That's the network, not the method — "
    "tap the button again and it'll come through."
)

THUMB_RENDER_FAIL = (
    f"{ICON} <b>Couldn't render the card.</b> The picks are saved — this is the "
    "renderer, not your choices. If this is a fresh box, the headless browser "
    "isn't installed yet: run <code>playwright install chromium</code> "
    "(and <code>playwright install-deps chromium</code> on Linux), then tap "
    "Generate again."
)


# ── Watch-order confirm / edit ──────────────────────────────────────────────────

def watch_order_card(title: str, order_html: str, *, rendered: bool = False) -> str:
    # When we arrive here straight from the thumbnail loop, fold the old "All
    # thumbnails rendered" beat into this card's header so it's ONE message, not
    # a separate "rendered → tap Order is correct → order card" hop.
    head = (
        f"{ICON} <b>All thumbnails rendered — now confirm the watch order</b>"
        if rendered else
        f"{ICON} <b>Step 3 — Confirm the watch order</b>"
    )
    return (
        f"{head}\n\n"
        f"<b>{esc(title)}</b>\n\n"
        f"{order_html}\n\n"
        "<i>Season 3 Part 2 is not Season 4 — I've kept them straight, but you have "
        "the final call. If it's right, confirm. If not, edit it.</i>"
    )


WATCH_ORDER_EDIT_PROMPT = (
    f"{ICON} <b>Send the corrected order.</b>\n\n"
    "Reply with the watch order in Markdown or HTML — whichever you like. I'll parse "
    "it, re-map the entries, and show you the result before anything's published."
)


def watch_order_edit_failed() -> str:
    return (
        f"{ICON} <b>Couldn't parse that.</b> Give me one entry per line — "
        "season/part or movie/OVA labels — and I'll re-map it clean."
    )


# ── Publishing ──────────────────────────────────────────────────────────────────

def publishing(title: str) -> str:
    return (
        f"{ICON} <b>Publishing {esc(title)}.</b> Posting the info card, dropping the "
        "divider sticker, pinning the watch guide. Give me a few seconds — precision "
        "takes a moment."
    )


def published_done(title: str) -> str:
    return (
        f"{ICON} <b>Done. Ten billion percent clean.</b> {esc(title)} is live — info card "
        "pinned, watch guide pinned, notices cleared. Handed to Gojo for publishing. "
        "Next experiment's up whenever you are."
    )


def task_aborted(title: str, *, by_cancel: bool = True) -> str:
    """Shown when a pipeline task is aborted (user cancel) but NOT deleted.

    The task stays assigned and still appears in /tasks — aborting only parks the
    in-progress work so the admin can pick it back up any time. The recurring
    artwork is attached by the caller (each bot uses its own character art)."""
    if by_cancel:
        return (
            f"{ICON} <b>Aborted.</b> {esc(title)} is parked — not gone. The task is "
            "still in your list, exactly where you left it. Open it whenever you want "
            "to pick the experiment back up."
        )
    return (
        f"{ICON} <b>Task parked.</b> {esc(title)} is still assigned to you and waiting "
        "in your task list. No work is lost — open it whenever you're ready."
    )


PUBLISH_FAIL = (
    f"{ICON} <b>Something broke mid-publish.</b> The method's sound — it's the wire. "
    "Check the logs and run it again; the flow picks up where it left off."
)


def filestore_missing(is_owner: bool) -> str:
    """Blocked-publish card when no file-store bots are configured.

    The quality buttons resolve through file-store bots; with none set, the
    channel would post links that lead nowhere. Owners get the settings path;
    everyone else is told to ask an admin. Both keep a Continue button so a
    fix-then-retry is one tap."""
    if is_owner:
        fix = (
            "Add them in <b>Settings → File-store bots</b> (paste the bot "
            "@usernames — commas are fine), then tap <b>Continue</b>."
        )
    else:
        fix = (
            "Ask your admin to add the file-store bots in Settings, then tap "
            "<b>Continue</b> to publish."
        )
    return (
        f"{ICON} <b>Hold on — no file-store bots configured.</b>\n\n"
        "The quality buttons need at least one file-store bot to hand out the "
        f"download links, or the channel ships dead buttons. {fix}"
    )



# ── Errors / misc ───────────────────────────────────────────────────────────────

GENERIC_FAIL = (
    f"{ICON} A step misfired. That's data, not defeat — check the logs and re-run it."
)

NO_TASK = (
    f"{ICON} <b>No such experiment.</b> That title isn't in my distribution queue — "
    "it may not have finished downloading yet."
)


# ── Button labels ───────────────────────────────────────────────────────────────

BTN_BEGIN = "▶️ Begin"
BTN_CONTINUE = "✓ Continue"
BTN_TASKS = "📋 My Titles"
BTN_OPEN_TASKS = "📋 Open Tasks"
BTN_HOME = "⇐ Home"
BTN_BACK = "⇐ Back"
BTN_SETTINGS = "⚙️ Settings"
BTN_HELP = "❔ How it works"
BTN_CANCEL = "✗ Cancel"

BTN_CHANNEL_DONE = "✅ I've created it"
BTN_TMDB_POSTER = "🖼 Open TMDB Poster Page"
BTN_NEXT = "➡️ Next"
BTN_SEND_LINK = "🔗 I've added both — send link"

# Two-scope channel creation (feature #41).
BTN_SCOPE_OWN = "👤 I'll create it"
BTN_SCOPE_USERBOT = "🤖 Let a user-bot create it"
BTN_USERBOT_JOIN = "🔗 Join the channel"
BTN_USERBOT_JOINED = "✅ I've joined"
BTN_USERBOT_DONE = "✅ Done — I added the photo"

# Human ban-recovery wizard actions. These deliberately live beside the normal
# channel-creation labels so recovery keeps the same Senku UI language.
BTN_RECOVERY_OWN = "👤 I'll create the replacement"
BTN_RECOVERY_AUTO = "🤖 Let a user-bot create it"
BTN_RECOVERY_CANCEL = "✗ Cancel recovery"

BTN_SHOW_LOGOS = "🔬 Show Logos"
BTN_SHOW_POSTERS = "🖼 Show Posters"
BTN_SHOW_BACKDROPS = "🌄 Show Backdrops"
BTN_UPLOAD_OWN = "⬆️ Upload"
BTN_TEXT_LOGO = "✍️ Text"
BTN_TEXT_CANCEL = "✗ Cancel"
BTN_TEXT_USE = "✅ Use this"
BTN_TEXT_BACK = "⇐ Back"
BTN_TEXT_UPLOAD_FONT = "⬆️ Upload your own"
BTN_TEXT_REUSE_YES = "✅ Use previous"
BTN_TEXT_REUSE_NO = "✍️ New text"
BTN_LOGO_REUSE_YES = "✅ Use previous"
BTN_LOGO_REUSE_NO = "⬆️ Upload another"
BTN_TEXT_ITALIC_OFF = "𝑰 Italic: off"
BTN_TEXT_ITALIC_ON = "𝑰 Italic: on"
TEXT_FONT_CUSTOM = "Uploaded font"
BTN_GENERATE = "⚗️ Generate Thumbnail"
BTN_THUMB_APPROVE = "✅ Approve"
BTN_THUMB_REDO = "↻ Redo"

BTN_ORDER_CORRECT = "✅ Order correct"
BTN_ORDER_EDIT = "✏️ Edit order"
BTN_PUBLISH = "📢 Publish"
