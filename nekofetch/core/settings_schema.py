"""Human-readable documentation for every configurable field.

The Settings panel uses this so an admin never has to read source code to know
what a setting does, what values are valid, or which variables a template
supports. Keyed by ``"<section>.<field>"``. Fields without an entry fall back to
a description derived from their name + current type.

Authoring rules (so the rendered edit prompt stays clean and never tangles):
  • ``desc`` is ONE line — no embedded newlines. Explain the effect plainly.
  • ``example`` is a PURE sample value — never cram an explanation in parens;
    put the explanation in ``desc`` or, for choices, in ``option_notes``.
  • ``option_notes`` documents each valid value on its own line.
  • ``placeholders`` lists EVERY variable the template actually supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Widgets the settings panel knows how to render. ``toggle``/``text``/``number``/
# ``list``/``template`` are the type-inferred defaults; the rest are explicit
# hints that turn a raw free-text prompt into something a non-coder can operate:
#   ``choice``   — the value is one of a fixed set → tap-to-pick buttons (a typo
#                  can no longer store garbage into an enum field).
#   ``channel``  — a Telegram chat id → guided capture (forward a message / paste
#                  an id), raw still accepted.
#   ``sticker``  — a sticker file id → "send me the sticker" capture.
#   ``timezone`` — an IANA zone → the shared timezone picker.
WIDGETS = frozenset(
    {"toggle", "text", "number", "list", "template", "choice", "channel",
     "sticker", "timezone"}
)


@dataclass(frozen=True)
class FieldDoc:
    desc: str                                   # what the setting does (one line)
    label: str | None = None                    # friendly name (else derived from slug)
    widget: str | None = None                   # explicit widget (else inferred from type)
    options: tuple[str, ...] = ()               # valid values (enum-like fields)
    option_notes: dict[str, str] = field(default_factory=dict)  # value -> meaning
    placeholders: dict[str, str] = field(default_factory=dict)  # template vars
    example: str | None = None                  # a pure sample value
    html: bool = False                          # template supports HTML

    def __post_init__(self) -> None:
        # Keep ``options`` and ``option_notes`` in sync: if only notes were given,
        # derive the value list from them so both render paths have data.
        if self.option_notes and not self.options:
            object.__setattr__(self, "options", tuple(self.option_notes.keys()))
        # A field with a fixed value set is a choice unless told otherwise.
        if self.widget is None and self.options:
            object.__setattr__(self, "widget", "choice")
        if self.widget is not None and self.widget not in WIDGETS:
            raise ValueError(f"unknown widget {self.widget!r} (allowed: {sorted(WIDGETS)})")


def widget_for(section: str, field_name: str, value: object) -> str:
    """The widget to render for a field: explicit schema hint wins, else inferred
    from the field name (raw-id / sticker / timezone conventions) and finally the
    live value's type. Never returns None — the panel always has a UI.

    Name conventions keep every current *and future* infrastructure field on a
    guided capture without a per-field schema entry: ``*channel_id`` → channel
    picker, ``*sticker_id`` / ``start_sticker*`` → sticker capture, ``timezone``
    → the zone picker. A field can still override any of these with an explicit
    ``widget=`` in its :class:`FieldDoc`.
    """
    doc = FIELD_DOCS.get(f"{section}.{field_name}")
    if doc and doc.widget:
        return doc.widget
    if doc and doc.placeholders:
        return "template"
    if isinstance(value, bool):
        return "toggle"
    # Name-based guided widgets — only for scalar (non-list) fields.
    if not isinstance(value, list):
        if field_name.endswith("channel_id"):
            return "channel"
        if field_name.endswith("sticker_id") or field_name.startswith("start_sticker"):
            return "sticker"
        if field_name == "timezone":
            return "timezone"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "text"


# ── shared placeholder sets ───────────────────────────────────────────────────
# Each set lists EVERY variable the matching template renderer actually injects,
# so the panel never advertises a variable that silently disappears.
_RENAME = {
    "{title}": "full anime title",
    "{short_title}": "shortened title (synonym or acronym) for tidy filenames",
    "{season}": "season number, zero-padded (01)",
    "{season_part}": "part tag for split seasons (P02), else blank",
    "{episode}": "episode number, padded to the season's episode count",
    "{resolution}": "quality, e.g. 1080p",
    "{audio}": "audio tag — Sub / Dub / Dual / Multi",
    "{source}": "the source the file came from",
    "{group}": "your release/brand group tag",
}
_RENAME_TYPED = {
    **_RENAME,
    "{content_type}": "entry type — Movie / OVA / ONA / Special",
}
_PACK = {
    "{title}": "anime title",
    "{season}": "season number (— for entries without one)",
    "{resolution}": "quality, e.g. 1080p",
    "{language}": "audio label — Sub / Dub / Dual",
    "{episode_from}": "first episode number in the pack",
    "{episode_to}": "last episode number in the pack",
    "{content_type}": "entry type — Season / OVA / ONA / Movie / Special",
    "{group}": "your release/brand group tag",
}
_MAIN = {
    "{title}": "anime title",
    "{tag}": "hashtag-safe title",
    "{episodes}": "episode count",
    "{qualities}": "available resolutions",
    "{languages}": "available audio tracks",
    "{genres}": "genre list",
    "{overview}": "synopsis / overview",
}
_INDEX_LETTER = {
    "{letter}": "the first-letter bucket (A, B, C …)",
    "{entries}": "the titles filed under that letter",
}
# ── distribution-bot card templates (post_format.*) ───────────────────────────
_CARD_INFO = {
    "{title}": "anime title",
    "{romaji}": "romaji title",
    "{genres}": "genre list",
    "{format}": "format — TV / Movie / OVA …",
    "{rating}": "AniList score",
    "{status}": "airing status",
    "{first_aired}": "first air date",
    "{last_aired}": "last air date",
    "{runtime}": "per-episode runtime",
    "{episodes}": "episode count",
    "{synopsis}": "synopsis (trimmed)",
}
_CARD_SEASON = {
    "{title}": "anime title",
    "{season}": "season number",
    "{episodes}": "episode count",
    "{S}": "'S' when plural (EPISODE vs EPISODES), else blank",
    "{rating}": "AniList score",
    "{language}": "audio label — Sub / Dub / Dual",
    "{genres}": "genre list",
    "{synopsis}": "synopsis (trimmed)",
}
_CARD_MOVIE = {
    "{title}": "movie title",
    "{duration}": "runtime from AniList minutes (e.g. 1h 35m)",
    "{language}": "audio label — Sub / Dub / Dual",
    "{synopsis}": "synopsis (trimmed)",
}
_CARD_GUIDE = {"{seasons}": "the assembled per-season lines block"}
_CARD_GUIDE_SEASON = {
    "{season_label}": "season heading (e.g. Season 2)",
    "{episodes}": "episode count",
    "{qualities}": "available resolutions",
}
_CARD_GUIDE_EXTRA = {
    "{label}": "extra heading (e.g. Movie / OVA)",
    "{episodes}": "episode count",
    "{qualities}": "available resolutions",
}


FIELD_DOCS: dict[str, FieldDoc] = {
    # ── features (master switches) ────────────────────────────────────────────
    "features.request_system": FieldDoc(
        desc="Master switch for user anime requests. Off = nobody can submit new requests."),
    "features.download_queue": FieldDoc(
        desc="Enable the background queue that downloads and processes requested titles."),
    "features.distribution_bots": FieldDoc(
        desc="Allow auto-creating and running per-title bots/channels that deliver files."),
    "features.watermarking": FieldDoc(
        desc="Burn a text or image watermark into subtitles during processing."),
    "features.metadata_editing": FieldDoc(
        desc="Rewrite embedded file metadata (title, author, tags) during processing."),
    "features.thumbnail_generation": FieldDoc(
        desc="Generate custom poster thumbnails for processed files and channel cards."),
    "features.auto_delete": FieldDoc(
        desc="Auto-delete delivered files from user chats after a delay (see Distribution)."),
    "features.temporary_links": FieldDoc(
        desc="Gate downloads behind time-limited access links instead of permanent ones."),
    "features.analytics": FieldDoc(
        desc="Collect request/delivery analytics shown on the admin dashboard."),
    "features.audit_logs": FieldDoc(
        desc="Record an audit trail of admin actions and setting changes."),
    "features.catbox_image_cache": FieldDoc(
        desc="Cache post images on catbox.moe so bots don't refetch from TMDB/AniList each "
             "delivery. Turn off behind firewalls that block catbox."),

    # ── sources ───────────────────────────────────────────────────────────────
    "sources.enabled": FieldDoc(
        desc="Active download sources, comma-separated. Order sets priority (first = tried first).",
        options=("local", "telegram", "anizone", "anikoto", "kickassanime", "miruro", "nyaa"),
        example="local, telegram, anizone, anikoto, kickassanime, miruro, nyaa"),
    "sources.default": FieldDoc(
        desc="Fallback source used when a request can't be resolved to a specific one.",
        options=("local", "telegram", "anizone", "anikoto", "kickassanime", "miruro", "nyaa"),
        example="telegram"),
    "sources.miruro.api_base_url": FieldDoc(
        desc="Base URL for the self-hosted Miruro-API service used by the Miruro source.",
        example="http://localhost:8000"),
    "sources.miruro.stream_referer": FieldDoc(
        desc="Referer used for HLS playlist and segment requests returned by Miruro-API.",
        example="http://localhost:8000"),
    "sources.miruro.provider_order": FieldDoc(
        desc="Miruro server priority, comma-separated; the first available provider wins.",
        example="kiwi, arc, zoro, hop, pahe"),

    # ── downloads ─────────────────────────────────────────────────────────────
    "downloads.concurrent_downloads": FieldDoc(
        desc="How many downloads run at once. Higher = faster, but more bandwidth and CPU.",
        example="5"),
    "downloads.retry_attempts": FieldDoc(
        desc="How many times a failed download is retried before giving up.", example="3"),
    "downloads.retry_backoff_seconds": FieldDoc(
        desc="Base wait between download retries; grows with each attempt.", example="10"),
    "downloads.resume_interrupted": FieldDoc(
        desc="Resume partially-downloaded files after an interruption instead of restarting."),
    "downloads.chunk_size_kb": FieldDoc(
        desc="Download chunk size in KB. Larger = fewer requests but more memory per transfer.",
        example="1024"),
    "downloads.progress_update_interval_seconds": FieldDoc(
        desc="How often the download progress bar refreshes, in seconds.", example="3"),

    # ── acquisition (what to fetch when a request pins nothing) ────────────────
    "acquisition.resolutions": FieldDoc(
        desc="Resolutions to fetch when a request pins none, comma-separated.",
        example="360p, 540p, 720p, 1080p"),
    "acquisition.languages": FieldDoc(
        desc="Audio tracks to fetch when unspecified, comma-separated.",
        option_notes={
            "english": "English dub",
            "japanese": "Japanese audio with English subtitles",
            "hindi": "Hindi dub",
        },
        example="english, japanese"),
    "acquisition.require_english_subs": FieldDoc(
        desc="Only accept releases that include an English subtitle track."),
    "acquisition.target_resolutions": FieldDoc(
        desc="Qualities grabbed for every request, best-first; each taken when the source has it.",
        example="1080p, 720p, 480p"),
    "acquisition.resolution_fallbacks": FieldDoc(
        desc=(
            "When a target quality is missing, alternates to try in order "
            "(advanced; edit in config.yaml)."
        ),
        example="480p: 540p, 360p"),

    # ── processing stages ─────────────────────────────────────────────────────
    "processing.verify_files": FieldDoc(
        desc="Verify each downloaded file is valid and non-corrupt before processing."),
    "processing.rename": FieldDoc(
        desc="Run the rename stage (applies the filename template below)."),
    "processing.metadata": FieldDoc(
        desc="Run the metadata-editing stage during processing."),
    "processing.branding": FieldDoc(
        desc="Run the branding stage (group tag, watermark text) during processing."),
    "processing.thumbnail": FieldDoc(
        desc="Run the thumbnail-generation stage during processing."),
    "processing.require_approval_before_publish": FieldDoc(
        desc="On = an admin must approve each title before it publishes. Off = auto-publish."),

    # ── rename ────────────────────────────────────────────────────────────────
    "rename.enabled": FieldDoc(
        desc="Rename processed files using the template below. Off keeps original filenames."),
    "rename.template": FieldDoc(
        desc="Filename pattern for regular TV season episodes.",
        placeholders=_RENAME,
        example="{short_title} S{season}E{episode} [{resolution}] [{audio}] - {group}"),
    "rename.movie_template": FieldDoc(
        desc="Filename pattern for movies — no season/episode (empty = use the season template).",
        placeholders=_RENAME_TYPED,
        example="{short_title} - Movie [{resolution}] [{audio}] - {group}"),
    "rename.special_template": FieldDoc(
        desc="Filename pattern for OVAs/ONAs/specials — keeps {episode}, drops the season "
             "(empty = use the season template).",
        placeholders=_RENAME_TYPED,
        example="{short_title} - {content_type} E{episode} [{resolution}] [{audio}] - {group}"),

    # ── metadata ──────────────────────────────────────────────────────────────
    "metadata.enabled": FieldDoc(
        desc="Master switch for the metadata-editing stage."),
    "metadata.update_title": FieldDoc(desc="Rewrite the embedded title field in each file."),
    "metadata.update_author": FieldDoc(desc="Rewrite the embedded author/artist field."),
    "metadata.update_comment": FieldDoc(desc="Rewrite the embedded comment field."),
    "metadata.update_tags": FieldDoc(desc="Rewrite embedded tags/genre metadata."),
    "metadata.update_description": FieldDoc(
        desc="Rewrite the embedded description/synopsis field."),
    "metadata.supported_containers": FieldDoc(
        desc="File extensions metadata editing applies to, comma-separated.",
        example="mkv, mp4, avi, mov"),

    # ── thumbnail ─────────────────────────────────────────────────────────────
    "thumbnail.enabled": FieldDoc(desc="Generate thumbnails during processing."),
    "thumbnail.attach_to_video": FieldDoc(desc="Embed the generated thumbnail into video files."),
    "thumbnail.attach_to_document": FieldDoc(
        desc="Attach the thumbnail when a file is sent as a document."),
    "thumbnail.generate_previews": FieldDoc(
        desc="Generate the preview images used on channel cards."),

    # ── watermark ─────────────────────────────────────────────────────────────
    "watermark.enabled": FieldDoc(desc="Burn a watermark into releases during processing."),
    "watermark.type": FieldDoc(
        desc="What kind of watermark to apply.",
        option_notes={"text": "render the watermark text below",
                      "image": "overlay the image at image_path"}),
    "watermark.text": FieldDoc(
        desc="Watermark text, used when type = text.", example="@AniXWeebs"),
    "watermark.image_path": FieldDoc(
        desc="Path to a watermark image file, used when type = image.",
        example="/data/branding/watermark.png"),
    "watermark.font": FieldDoc(
        desc="Font for the text watermark (blank = ffmpeg default).",
        option_notes={
            "": "ffmpeg built-in default font",
            "galantic.regular.otf": "Galantic — clean display serif",
            "golden-varsity.regular.ttf": "Golden Varsity — collegiate block",
            "hit-me-punk.01.ttf": "Hit Me Punk — grungy brush",
            "sportzan.regular.ttf": "Sportzan — bold sporty sans",
        }),
    "watermark.corner": FieldDoc(
        desc="Which corner of the frame the watermark sits in.",
        option_notes={
            "bottom_right": "lower-right corner",
            "bottom_left": "lower-left corner",
            "top_right": "upper-right corner",
            "top_left": "upper-left corner",
        }),
    "watermark.margin": FieldDoc(
        desc="Distance in pixels from each frame edge (per corner).", example="16"),
    "watermark.opacity": FieldDoc(
        desc="Watermark opacity from 0.0 (invisible) to 1.0 (solid).", example="0.55"),
    "watermark.scale": FieldDoc(
        desc="Text watermark size as a fraction of frame HEIGHT (0.03 = 3%). "
             "For an image watermark it's a fraction of frame width.",
        example="0.03"),
    "watermark.fast": FieldDoc(
        desc="Fast watermark burn: use the quickest H.264 encoder available "
             "(GPU NVENC/QSV/VAAPI if present, else libx264) instead of "
             "re-encoding to the source's slower codec. Much faster; trades a "
             "little 1080p size. Turn off for a codec-matching quality burn."),

    # ── branding ──────────────────────────────────────────────────────────────
    "branding.enabled": FieldDoc(
        desc="Apply your brand name, footer, and watermark text across posts and files."),
    "branding.channel_name": FieldDoc(
        desc="Your brand/channel name shown on cards and posts.", example="Anime Weebs"),
    "branding.footer_text": FieldDoc(
        desc="Footer line appended to posts.", example="Anime Weebs"),
    "branding.website": FieldDoc(
        desc="Your website URL, shown where the brand links appear.",
        example="https://animeweebs.example"),
    "branding.telegram_channel": FieldDoc(
        desc="Your public Telegram channel handle.", example="@AnimeWeebs"),
    "branding.community_link": FieldDoc(
        desc="Invite link to your community/group.", example="https://t.me/AnimeWeebsChat"),
    "branding.watermark_text": FieldDoc(
        desc="Default subtitle watermark text inserted into releases.", example="@AniXWeebs"),
    "branding.metadata_author": FieldDoc(
        desc="Author tag written into processed file metadata.", example="Anime Weebs"),
    "branding.metadata_comment": FieldDoc(
        desc="Comment tag written into processed file metadata.",
        example="Provided by Anime Weebs"),

    # ── distribution / delivery ───────────────────────────────────────────────
    "distribution.mode": FieldDoc(
        desc="How published content is packaged for delivery.",
        option_notes={"season_package": "one pack per season/entry",
                      "single_file": "one delivery per episode"}),
    "distribution.protect_content": FieldDoc(
        desc="Block users from forwarding/saving delivered files (Telegram content protection)."),
    "distribution.temporary_links": FieldDoc(
        desc="Deliver via time-limited links instead of permanent file references."),
    "distribution.link_expiry_minutes": FieldDoc(
        desc="Minutes a generated access link stays valid.", example="60"),
    "distribution.auto_delete": FieldDoc(
        desc="Delete delivered files from the user's chat after a delay."),
    "distribution.auto_delete_after_minutes": FieldDoc(
        desc="Delay before delivered files are auto-deleted, in minutes (0 = never).",
        example="60"),

    # ── queue ─────────────────────────────────────────────────────────────────
    "queue.max_visible": FieldDoc(
        desc="How many queue rows show on the dashboard at once.", example="10"),
    "queue.position_recalc_seconds": FieldDoc(
        desc="How often queue positions and ETAs are recomputed, in seconds.", example="5"),

    # ── security ──────────────────────────────────────────────────────────────
    "security.rate_limit_per_minute": FieldDoc(
        desc="Maximum actions a single user may take per minute.", example="20"),
    "security.anti_spam_cooldown_seconds": FieldDoc(
        desc="Minimum seconds between a user's repeated actions.", example="2"),
    "security.force_subscribe": FieldDoc(
        desc="Require users to join your channels before they can use this bot."),
    "security.force_subscribe_channels": FieldDoc(
        desc="Channels users must join before using this bot, added one at a time (-100… ids).",
        example="-1001234567890, -1009876543210"),
    "security.dist_force_subscribe": FieldDoc(
        desc=(
            "Require users to join channels before using distribution bots "
            "(separate from admin)."
        )),
    "security.dist_force_subscribe_channels": FieldDoc(
        desc="Channel IDs users must join for distribution bots, comma-separated (-100… ids).",
        example="-1001234567890, -1009876543210"),
    "security.owner_id": FieldDoc(
        desc=(
            "Telegram user id of the bot owner (full access). Normally set in .env "
            "- change with care."
        ),
        example="123456789"),

    # ── access (trial + token) ────────────────────────────────────────────────
    "access.enabled": FieldDoc(
        desc="Require time-based access (trial, then token renewal) before users can download."),
    "access.free_trial": FieldDoc(
        desc="Grant new users a free trial window before a token is required."),
    "access.trial_days": FieldDoc(desc="Free-trial length in days.", example="3"),
    "access.token_days": FieldDoc(desc="How many days a renewed token grants access.", example="3"),
    "access.token_link_ttl_hours": FieldDoc(
        desc="How long a generated token link stays valid, in hours.", example="24"),
    "access.forward_to_saved_hint": FieldDoc(
        desc="Nudge users to forward delivered files to Saved Messages before auto-delete."),

    # ── shortlink ─────────────────────────────────────────────────────────────
    "shortlink.enabled": FieldDoc(
        desc="Gate token generation behind a URL shortener (earns ad revenue per unlock)."),
    "shortlink.provider": FieldDoc(
        desc="Which URL shortener issues the token links.",
        option_notes={"arolinks": "AroLinks provider", "vplinks": "VPLinks provider"}),
    "shortlink.base_url": FieldDoc(
        desc="Custom provider base URL, for generic/legacy shorteners only.",
        example="https://vplinks.in"),

    # ── storage channel (the database channel) ────────────────────────────────
    "storage_channel.enabled": FieldDoc(
        desc="Enable the private database channel that stores content packs."),
    "storage_channel.channel_id": FieldDoc(
        desc="Telegram id (-100…) of the private database channel.",
        example="-1001234567890"),
    "storage_channel.header_template": FieldDoc(
        desc="Header posted above each TV-season storage pack.",
        placeholders=_PACK, html=True,
        example="{title} — Season {season} [{resolution}] [{language}]"),
    "storage_channel.movie_header_template": FieldDoc(
        desc="Header for movie packs — no season number (empty = use the season header).",
        placeholders=_PACK, html=True,
        example="{title} — {content_type} [{resolution}] [{language}]"),
    "storage_channel.special_header_template": FieldDoc(
        desc="Header for OVA/ONA/special packs — no season number (empty = use the season header).",
        placeholders=_PACK, html=True,
        example="{title} — {content_type} [{resolution}] [{language}]"),
    "storage_channel.end_sticker_id": FieldDoc(
        desc="file_id of the sticker posted after each pack (empty = none).",
        example="CAACAgUAAxkBAAI0vGpAOaZ7gJ6Yk9MtJ63jm0sYmDysAAI..."),
    "storage_channel.copy_mode": FieldDoc(
        desc="How files are delivered to users from the storage channel.",
        option_notes={"copy": "clean re-send with no 'forwarded from' tag",
                      "forward": "faster, but keeps the 'forwarded from' source tag"}),
    "storage_channel.include_header_in_delivery": FieldDoc(
        desc="Include the pack header message when delivering to a user."),
    "storage_channel.include_sticker_in_delivery": FieldDoc(
        desc="Include the end-of-pack sticker when delivering to a user."),

    # ── log channel (control center) ──────────────────────────────────────────
    "log_channel.enabled": FieldDoc(
        desc="Enable the operational control-center channel (dashboard, queue, notices)."),
    "log_channel.channel_id": FieldDoc(
        desc="Telegram id (-100…) of the control-center channel.", example="-1001234567890"),
    "log_channel.pinned_dashboard": FieldDoc(
        desc="Keep a live stats dashboard message pinned and edited in place."),
    "log_channel.pinned_catalog": FieldDoc(
        desc="Keep a published-catalog index message pinned and edited in place."),
    "log_channel.sections": FieldDoc(
        desc="Use the full sectioned control center (dashboard/pending/active/completed/notices)."),
    "log_channel.reserved_slots": FieldDoc(
        desc="Spare pre-allocated messages per growth-prone section (for Telegram's edit window).",
        example="2"),
    "log_channel.notices_lines": FieldDoc(
        desc="How many recent events the rolling notices stream keeps.", example="12"),
    "log_channel.divider_sticker_id": FieldDoc(
        desc="Sticker used as a permanent divider between sections (empty = none).",
        example="CAACAgUAAxkBAAI0vGpAOaZ7gJ6Yk9MtJ63jm0sYmDysAAI..."),
    "log_channel.cover_image": FieldDoc(
        desc="Cover image at the top of the channel — URL or file_id (empty = none).",
        example="https://example.com/cover.png"),
    "log_channel.refresh_seconds": FieldDoc(
        desc="How often the whole control center is fully rebuilt, in seconds.", example="60"),
    "log_channel.active_refresh_seconds": FieldDoc(
        desc="Fast-lane refresh for the live active-tasks panel, in seconds.", example="5"),
    "log_channel.events": FieldDoc(
        desc="Which event categories to forward — 'all', or a comma-separated subset.",
        example="all"),

    # ── main channel (public) ─────────────────────────────────────────────────
    "main_channel.enabled": FieldDoc(
        desc="Enable posting each published anime to the public main channel."),
    "main_channel.channel_id": FieldDoc(
        desc="Telegram id (-100…) of the public main channel.", example="-1001234567890"),
    "main_channel.caption_template": FieldDoc(
        desc="Caption for each anime posted to the public main channel.",
        placeholders=_MAIN, html=True,
        example="{title}『 #{tag} 』\\n⌬ EPISODES : {episodes}"),
    "main_channel.index_button_text": FieldDoc(
        desc="Label of the Index button (small-caps Unicode supported).", example="ɪɴᴅᴇx"),
    "main_channel.download_button_text": FieldDoc(
        desc="Label of the Download button (small-caps Unicode supported).", example="ᴅᴏᴡɴʟᴏᴀᴅ"),
    "main_channel.silent_default": FieldDoc(
        desc="Publish to the main channel silently by default (no member notification). "
             "The review card's Silent button always forces silent regardless.",
        label="Silent by default"),

    # ── index channel ─────────────────────────────────────────────────────────
    "index_channel.enabled": FieldDoc(
        desc="Enable the stylized per-letter catalog index channel."),
    "index_channel.channel_id": FieldDoc(
        desc="Telegram id (-100…) of the index channel.", example="-1001234567890"),
    "index_channel.letter_header_template": FieldDoc(
        desc="Header rendered above each first-letter index post.",
        placeholders=_INDEX_LETTER, html=True,
        example="•──────• {letter} •──────•"),
    "index_channel.entry_template": FieldDoc(
        desc="One catalog line per title in the index.",
        placeholders={"{title}": "anime title"}, html=True, example="⦿ {title}"),
    "index_channel.username": FieldDoc(
        desc="Public @username of the index channel (no @). Drives every t.me/…/… "
             "deep link on the poster and letter buttons; a restore rewrites it.",
        label="Index channel username", example="AniXWeebs_Index"),
    "index_channel.poster_message_id": FieldDoc(
        desc="Message id of the pinned poster (the letter-grid navigation post). "
             "Updated automatically by a restore.",
        label="Poster message id", example="171"),
    "index_channel.main_channel_link": FieldDoc(
        desc="Link the index letter buttons' “Main Channel” button targets.",
        label="Main-channel link", example="https://t.me/AniXWeebs"),

    # ── UI / onboarding ───────────────────────────────────────────────────────
    "ui.start_sticker_id": FieldDoc(
        desc="Shared /start sticker for the admin + delivery bots, and the fallback "
             "for any pipeline bot without its own sticker below. Telegram file_id "
             "(empty = none).",
        example="CAACAgUAAyEFAASAgUwqAAJh_mck..."),
    "ui.start_sticker_lelouch": FieldDoc(
        desc="Lelouch's own /start sticker (empty = use start_sticker_id). Telegram file_id.",
        example="CAACAgUAAyEFAASAgUwqAAJh_mck..."),
    "ui.start_sticker_levi": FieldDoc(
        desc="Levi's own /start sticker (empty = use start_sticker_id). Telegram file_id.",
        example="CAACAgUAAyEFAASAgUwqAAJh_mck..."),
    "ui.start_sticker_senku": FieldDoc(
        desc="Senku's own /start sticker (empty = use start_sticker_id). Telegram file_id.",
        example="CAACAgUAAyEFAASAgUwqAAJh_mck..."),
    "ui.start_sticker_gojo": FieldDoc(
        desc="Gojo's own /start sticker (empty = use start_sticker_id). Telegram file_id.",
        example="CAACAgUAAyEFAASAgUwqAAJh_mck..."),
    "ui.start_image_url": FieldDoc(
        desc="Welcome image shown on /start — URL or file_id (empty = none).",
        example="https://envs.sh/odE.png"),
    "ui.start_image_has_spoiler": FieldDoc(
        desc="Send the welcome image with a spoiler (tap-to-reveal) blur."),
    "ui.sticker_delete_delay": FieldDoc(
        desc="Seconds the /start sticker stays before it's removed.", example="1.5"),
    "ui.loading_dot_delay": FieldDoc(
        desc="Seconds between animated loading-dot frames.", example="0.32"),
    "ui.loading_steps": FieldDoc(
        desc="How many stages the animated loading sequence shows.", example="3"),

    # ── distribution bots ─────────────────────────────────────────────────────
    "bot.auto_create_on_publish": FieldDoc(
        desc="Auto-create a distribution bot/channel when a title is published."),
    "bot.health_check_interval_minutes": FieldDoc(
        desc="Minutes between bot ban-detection health checks (0 = disabled).", example="60"),
    "bot.delivery_retention_days": FieldDoc(
        desc="Days before bot-delivered messages auto-delete per user (0 = never).", example="7"),
    "bot.avatar_source": FieldDoc(
        desc="Where a new bot's profile photo comes from.",
        option_notes={"tmdb": "TMDB poster art", "anilist": "AniList cover art"}),
    "bot.filestore_bots": FieldDoc(
        desc="Fstore file-delivery bot usernames, comma-separated. Alternated for load-sharing "
             "and ban resilience.",
        example="KiloxBot, MarkySayBot"),
    "bot.footer_image_url": FieldDoc(
        desc="Image on every distribution bot's footer post — URL or file_id (empty = none).",
        example="https://files.catbox.moe/example.png"),
    "bot.footer_text": FieldDoc(
        desc="Override the built-in footer text (empty = use the default template).",
        example="ANIME WEEBS — feel the story, live the art"),
    "bot.divider_sticker_id": FieldDoc(
        desc="Sticker sent between content sections (info → seasons → guide → footer).",
        example="CAACAgUAAxkBAAI5pmpE1uh9_sD-z2tYJ3wlado6vS29AAI..."),
    "bot.max_bots_per_account": FieldDoc(
        desc="Max distribution bots one userbot account may create before rolling over.",
        example="20"),
    "bot.max_channels_per_account": FieldDoc(
        desc="Max distribution channels one userbot account may create before rolling over.",
        example="10"),
    "bot.entity_full_check_days": FieldDoc(
        desc="Days between full health audits of all created bots/channels.", example="7"),
    "bot.update_check_interval_days": FieldDoc(
        label="Monthly Update-Check Interval (days)",
        desc="Days between the scheduled franchise update sweep. 0 disables the "
             "scheduled job (the manual /updates command still works).", example="30"),
    "bot.ban_check_interval_days": FieldDoc(
        label="Monthly Ban-Check Interval (days)",
        desc="Days between the scheduled ban check. 0 disables the scheduled job "
             "(the manual /bancheck command still works).", example="30"),
    "bot.monthly_update_check_enabled": FieldDoc(
        label="Scheduled Update Check",
        desc="Master switch for the scheduled monthly update sweep (detect-only — "
             "it DMs Gojo admins a reviewable list, nothing is auto-created)."),
    "bot.monthly_ban_check_enabled": FieldDoc(
        label="Scheduled Ban Check",
        desc="Master switch for the scheduled monthly ban check (auto-recovers down "
             "distribution channels from backup)."),
    "bot.restore_batch_size": FieldDoc(
        label="Restore Batch Size",
        desc="Posts per batch during a whole-channel restore (Change Main). 0 posts "
             "as fast as the API allows; a positive value paces large catalogs.",
        example="10"),
    "bot.restore_batch_delay_seconds": FieldDoc(
        label="Restore Batch Delay (seconds)",
        desc="Seconds to sleep between restore batches when Restore Batch Size is set.",
        example="5"),
    "bot.image_host_order": FieldDoc(
        label="Image Mirror Host Order",
        desc="Durable image-backup hosts tried in order; the first that sticks becomes "
             "the primary URL. Recognized: catbox, telegraph, imgbb.",
        example="catbox, telegraph, imgbb"),
    "bot.bot_username_suffix": FieldDoc(
        desc="Suffix appended when generating new distribution-bot usernames.", example="Bot"),
    "bot.channel_username_suffix": FieldDoc(
        desc="Suffix appended when generating new distribution-channel usernames.", example="TV"),

    # ── thumbnail channel ─────────────────────────────────────────────────────
    "thumbnail_channel.enabled": FieldDoc(
        desc="Enable the thumbnail control-center channel for asset selection & generation."),
    "thumbnail_channel.channel_id": FieldDoc(
        desc="Telegram id (-100…) of the private thumbnail workflow channel.",
        example="-1001234567890"),
    "thumbnail_channel.cover_image": FieldDoc(
        desc="Intro cover image for the channel — URL or file_id (empty = none).",
        example="https://example.com/cover.png"),
    "thumbnail_channel.divider_sticker_id": FieldDoc(
        desc="Sticker used as a visual divider between sections (empty = none).",
        example="CAACAgUAAxkBAAI0vGpAOaZ7gJ6..."),
    "thumbnail_channel.telegraph_access_token": FieldDoc(
        desc="Telegraph API access token used to build asset galleries.",
        example="b968da509bb76866c35425099bc0989a5ec3b32997d55286c657e6994bbb"),
    "thumbnail_channel.max_queue_size": FieldDoc(
        desc="Maximum entries allowed in the thumbnail queue at once.", example="20"),

    # ── post format (distribution-bot card look) ───────────────────────────────
    "post_format.info_card_template": FieldDoc(
        desc="Franchise info/overview card. Empty = built-in default.",
        placeholders=_CARD_INFO, html=True,
        example="<b>{title}</b>\\n⭐ {rating}  •  {episodes} eps"),
    "post_format.season_card_template": FieldDoc(
        desc="Per-season card (multi-episode entries). Empty = built-in default.",
        placeholders=_CARD_SEASON, html=True,
        example="<b>{title}</b> — Season {season}\\n{episodes} EPISODE{S}"),
    "post_format.movie_card_template": FieldDoc(
        desc="Movie / single-episode card — shows runtime, not episode count. Empty = default.",
        placeholders=_CARD_MOVIE, html=True,
        example="<b>{title}</b>\\n⏱ {duration}  •  {language}"),
    "post_format.extras_card_template": FieldDoc(
        desc="Reserved for a distinct extras card; extras currently reuse the season/movie "
             "card by episode-count rule. Empty = that rule.",
        placeholders=_CARD_MOVIE),
    "post_format.watch_guide_template": FieldDoc(
        desc="Wrapper around the assembled watch-guide lines. Empty = built-in default.",
        placeholders=_CARD_GUIDE, html=True,
        example="<b>WATCH ORDER</b>\\n{seasons}"),
    "post_format.watch_guide_season_line": FieldDoc(
        desc="One watch-guide line per season. Empty = built-in default.",
        placeholders=_CARD_GUIDE_SEASON, html=True,
        example="{season_label} — {episodes} eps [{qualities}]"),
    "post_format.watch_guide_extra_line": FieldDoc(
        desc="One watch-guide line per extra (movie/OVA). Empty = built-in default.",
        placeholders=_CARD_GUIDE_EXTRA, html=True,
        example="{label} [{qualities}]"),
    "post_format.footer_template": FieldDoc(
        desc="Footer card text. Empty = BotConfig.footer_text, then the built-in default.",
        html=True, example="ANIME WEEBS — feel the story, live the art"),
    "post_format.footer_image_url": FieldDoc(
        desc="Footer image — URL or file_id. Empty = BotConfig.footer_image_url.",
        example="https://files.catbox.moe/example.png"),
    "post_format.resolution_label": FieldDoc(
        desc="Quality-button label wrapper. Must contain {res} or it falls back to bare "
             "resolution.",
        placeholders={"{res}": "resolution, e.g. 1080p"}, example="「 {res} 」"),
    "post_format.buttons_per_row": FieldDoc(
        desc="Quality buttons per keyboard row. 2 = reference layout (2→[2], 3→[2,1], 4→[2,2]).",
        example="2"),
    "post_format.max_quality_buttons": FieldDoc(
        desc="Cap on how many distinct qualities become buttons (reference shows 3).",
        example="3"),
    "post_format.language_label_japanese": FieldDoc(
        desc="Header above Japanese/sub quality buttons (separate-audio layout). Empty = default.",
        example="🇯🇵 Japanese"),
    "post_format.language_label_english": FieldDoc(
        desc="Header above English/dub quality buttons (separate-audio layout). Empty = default.",
        example="🇬🇧 English"),
    "post_format.japanese_first": FieldDoc(
        desc="Show the Japanese (original audio) section first, matching the reference channels."),
    "post_format.pin_info_card": FieldDoc(
        desc="Pin the info/overview card in the distribution channel."),
    "post_format.pin_watch_guide": FieldDoc(
        desc="Pin the watch guide in the distribution channel."),
    "post_format.divider_sticker_id": FieldDoc(
        desc="Divider sticker between sections. Empty = fall back to bot.divider_sticker_id.",
        example="CAACAgUAAxkBAAI5pmpE1uh9..."),
    "post_format.duration_format_hm": FieldDoc(
        desc="Runtime format when an hour or more. {h}=hours {m}=minutes.",
        placeholders={"{h}": "hours", "{m}": "minutes"}, example="{h}h {m}m"),
    "post_format.duration_format_m": FieldDoc(
        desc="Runtime format when under an hour. {m}=minutes.",
        placeholders={"{m}": "minutes"}, example="{m}m"),
    "post_format.premium_emoji": FieldDoc(
        desc="Map :name: tokens (or raw glyphs) to Telegram custom-emoji ids; expanded in every "
             "card. Empty = plain unicode.",
        example="movie=5375464961822695008, sparkle=5471952986970267163"),

    # ── localization ──────────────────────────────────────────────────────────
    "localization.default_language": FieldDoc(
        desc="Default UI language code (must match a JSON file in the language directory).",
        example="en"),
    "localization.directory": FieldDoc(
        desc="Folder holding the language JSON catalogs.", example="resources/language"),
}


# Sensitive config — infrastructure ids, credentials, security, sources. Only the
# owner may view or change these; non-owner admins get the operational sections.
OWNER_ONLY_SECTIONS = frozenset({
    "security", "sources", "access", "shortlink",
    "storage_channel", "log_channel", "main_channel", "index_channel",
    "thumbnail_channel", "bot", "post_format",
})


def doc_for(section: str, field_name: str) -> FieldDoc | None:
    return FIELD_DOCS.get(f"{section}.{field_name}")


def label_for(section: str, field_name: str) -> str | None:
    """The schema-authored friendly label for a field, if one is set (else None
    so callers can fall back to their own slug-prettifier)."""
    doc = FIELD_DOCS.get(f"{section}.{field_name}")
    return doc.label if doc else None


def is_owner_only(section: str) -> bool:
    return section in OWNER_ONLY_SECTIONS
