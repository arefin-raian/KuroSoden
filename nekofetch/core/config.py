"""Configuration system.

Three layers, in increasing precedence:

1. ``.env`` / environment  -> secrets & connection strings  (``EnvSettings``)
2. ``config.yaml``         -> feature toggles & behaviour    (``AppConfig``)
3. MongoDB ``settings``    -> runtime overrides from the admin panel
                              (applied by ``ConfigService``, see services layer)

``EnvSettings`` and ``AppConfig`` are immutable, typed snapshots loaded at startup.
Runtime overrides are layered on read so the admin can change behaviour without a
restart — see ``nekofetch.services.config_service.ConfigService``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _portable_path(value: Any, default: str) -> Path:
    """Coerce a configured filesystem path to something valid on the current OS.

    A Windows-style path (drive letter like ``C:\\`` or backslash separators) is
    meaningless on a POSIX host — pathlib treats it as one literal segment, so it
    leaks into ffmpeg as ``C:\\data\\storage/work/...`` and fails with "Protocol
    not found". When that mismatch is detected, fall back to a portable,
    project-relative default so a clone runs anywhere without editing paths.
    (main.py chdir's to the project root, so a relative default lands there.)
    """
    s = str(value).strip().strip('"').strip("'")
    if not s:
        return Path(default)
    looks_windows = ("\\" in s) or (len(s) >= 2 and s[1] == ":" and s[0].isalpha())
    if os.name != "nt" and looks_windows:
        return Path(default)
    return Path(s)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — secrets & connection strings (.env)
# ─────────────────────────────────────────────────────────────────────────────
class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    telegram_api_id: int = Field(..., alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(..., alias="TELEGRAM_API_HASH")
    admin_bot_token: str = Field(..., alias="ADMIN_BOT_TOKEN")
    admin_ids: list[int] = Field(default_factory=list, alias="ADMIN_IDS")
    request_bot_token: str = Field("", alias="REQUEST_BOT_TOKEN")
    downloader_bot_token: str = Field("", alias="DOWNLOADER_BOT_TOKEN")
    distribution_bot_token: str = Field("", alias="DISTRIBUTION_BOT_TOKEN")
    publisher_bot_token: str = Field("", alias="PUBLISHER_BOT_TOKEN")

    # Security
    secret_key: str = Field(..., alias="SECRET_KEY")

    # PostgreSQL
    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    postgres_user: str = Field("nekofetch", alias="POSTGRES_USER")
    postgres_password: str = Field("change-me", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("nekofetch", alias="POSTGRES_DB")

    # MongoDB
    mongo_uri: str = Field("mongodb://mongo:27017", alias="MONGO_URI")
    mongo_db: str = Field("nekofetch", alias="MONGO_DB")

    # Redis
    redis_url: str = Field("redis://redis:6379/0", alias="REDIS_URL")

    @field_validator("redis_url", mode="before")
    @classmethod
    def _coerce_redis_tls(cls, v: Any) -> Any:
        """Upgrade managed Redis providers that REQUIRE TLS to ``rediss://``.

        Upstash (and most managed Redis) reject plain ``redis://`` with
        "Connection closed by server" — the exact failure that surfaces on
        Railway as "middleware Redis unreachable". These hosts only speak TLS,
        so a plain-scheme URL can never connect. We auto-upgrade the scheme for
        known TLS-only hosts so a copy-pasted ``redis://...upstash.io`` URL works
        without the user having to remember the second ``s``.
        """
        if not isinstance(v, str) or not v:
            return v
        _TLS_HOSTS = ("upstash.io",)
        if v.startswith("redis://") and any(h in v for h in _TLS_HOSTS):
            return "rediss://" + v[len("redis://"):]
        return v

    # Storage. Portable, project-relative defaults so a fresh clone runs on any OS
    # without root or manual path edits; override in .env (Docker uses /data/...).
    storage_path: Path = Field(Path("data/storage"), alias="STORAGE_PATH")
    session_path: Path = Field(Path("data/sessions"), alias="SESSION_PATH")
    # Pyrogram parallel file-chunk streams for Levi's uploads (0 = use the
    # built-in default of 8). Raise for a fat uplink, lower if the host throws
    # FLOOD_WAIT / connection resets under many parallel streams.
    upload_concurrency: int = Field(0, alias="UPLOAD_CONCURRENCY")

    @field_validator("storage_path", mode="before")
    @classmethod
    def _coerce_storage_path(cls, v: Any) -> Path:
        return _portable_path(v, "data/storage")

    @field_validator("session_path", mode="before")
    @classmethod
    def _coerce_session_path(cls, v: Any) -> Path:
        return _portable_path(v, "data/sessions")

    # Userbot (Telegram USER session — not a bot token). Powers the @acutebot
    # metadata fallback and any userbot-only capability (channel history, etc.).
    # Generate a StringSession once interactively, then paste it here. The pool
    # (``UserbotPool.from_env``) also honours ``TELEGRAM_USERBOT_ACCOUNTS`` (inline
    # JSON array) and ``TELEGRAM_USERBOT_ACCOUNTS_FILE`` (path to a JSON array) for
    # multi-account rotation; a single session string is the simplest setup.
    telegram_userbot_session: str = Field("", alias="TELEGRAM_USERBOT_SESSION")

    # TMDB
    tmdb_read_access_token: str = Field("", alias="TMDB_API_READ_ACCESS_TOKEN")
    tmdb_api_key: str = Field("", alias="TMDB_API_KEY")

    # ImgBB — free public image host used as a durable backup mirror (fallback
    # after catbox). Get a key at https://api.imgbb.com/. Empty = the imgbb mirror
    # is skipped (catbox / telegraph still run).
    imgbb_api_key: str = Field("", alias="IMGBB_API_KEY")

    # Telegraph — access token for creating galleries and (legacy) file uploads.
    # Generate one with: curl "https://api.telegra.ph/createAccount?short_name=kuro&author_name=Kuro"
    # and copy the ``result.access_token`` from the JSON. When set, it overrides
    # the (optional) config.yaml ``thumbnail_channel.telegraph_access_token`` so
    # the secret lives in .env with the other credentials. Empty = fall back to
    # config.yaml, then skip the telegraph host.
    telegraph_access_token: str = Field("", alias="TELEGRAPH_ACCESS_TOKEN")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_json: bool = Field(False, alias="LOG_JSON")

    # Schema management: True auto-creates tables on startup (dev convenience).
    # Set False in production and manage the schema with Alembic migrations.
    auto_create_schema: bool = Field(True, alias="AUTO_CREATE_SCHEMA")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, v: Any) -> Any:
        if v is None or v == "":
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?ssl=require"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — feature toggles & behaviour (config.yaml)
# Each section mirrors a block in config.yaml. Defaults make the file optional.
# ─────────────────────────────────────────────────────────────────────────────
class Features(BaseModel):
    request_system: bool = True
    download_queue: bool = True
    distribution_bots: bool = True
    watermarking: bool = False
    metadata_editing: bool = True
    thumbnail_generation: bool = True
    auto_delete: bool = False
    temporary_links: bool = True
    analytics: bool = True
    audit_logs: bool = True
    # When True, ``BotContentService.generate_posts`` uploads every image-bearing
    # post's poster to catbox.moe and stores the public URL on
    # ``BotContentPost.image_cached_url``. The distribution bot serves from this
    # URL on /start so Telegram doesn't refetch from TMDB/AniList CDNs every
    # delivery. Disable for operators behind firewalls that block catbox, or
    # for transient DR runs where you don't want the small dependency on a
    # third-party file host.
    catbox_image_cache: bool = True


class DownloadsConfig(BaseModel):
    concurrent_downloads: int = 5
    retry_attempts: int = 3
    retry_backoff_seconds: int = 10
    resume_interrupted: bool = True
    chunk_size_kb: int = 1024
    progress_update_interval_seconds: int = 3


class ProcessingConfig(BaseModel):
    verify_files: bool = True
    rename: bool = True
    metadata: bool = True
    branding: bool = True
    thumbnail: bool = True
    # Torrent sources deliver a single (1080p) file per episode; derive the
    # lower quality tiers (720p / 480p) ourselves via ffmpeg so every release
    # ships the standard three resolutions. Streaming sources acquire their own
    # qualities natively and skip this stage.
    encode: bool = True
    # Target rendition heights produced from the source file. Uploaded smallest
    # first, so packs post as 480p → 720p → 1080p.
    encode_heights: list[int] = Field(default_factory=lambda: [720, 480])
    # x264 speed/efficiency preset for the derived tiers. "faster" is the chosen
    # speed/size balance — noticeably quicker than "fast"/"medium" while
    # -tune animation + psy-rd + per-tier CRF keep quality natural for the
    # resolution. Derived tiers always encode as libx264 regardless of source
    # codec (fast + small + universally playable). Valid: ultrafast…veryslow.
    encode_preset: str = "faster"
    # Constant Rate Factor per tier (higher = smaller). 480p 26 → <100 MB, 720p 24
    # → ~100–200 MB from a ~300 MB source. 1080p key is used only by the watermark
    # re-encode (derived tiers never include 1080p).
    encode_crf: dict[int, int] = Field(
        default_factory=lambda: {1080: 21, 720: 24, 480: 26}
    )
    # x264 threads per encode. 0 = auto: divide the box's cores by
    # concurrent_downloads (floor 2) so several admins' jobs can encode at once
    # without oversubscribing the CPU. Set a fixed number to pin it.
    encode_threads: int = 0
    # When False, the pipeline automatically publishes to the main channel +
    # index channel and brings up the distribution bot right after the storage
    # upload finishes — no admin click required. When True, the existing
    # approval card gates the publish step.
    require_approval_before_publish: bool = False


class RenameConfig(BaseModel):
    enabled: bool = True
    # Default (TV season) filename pattern.
    template: str = "{title} S{season}E{episode} [{resolution}] [{audio}] - {group}"
    # Per-type overrides. Movies and OVAs/specials have no meaningful S/E pair, so
    # forcing them into the season template ("S90E01") reads terribly. When a
    # per-type template is empty, that type falls back to ``template``.
    # ``movie_template`` is used for single-file movies; ``special_template`` for
    # OVAs/ONAs/specials (keeps an entry index via {episode}, drops the season).
    # Same variables as ``template`` plus {content_type} (Movie / OVA / Special).
    movie_template: str = "{short_title} - Movie [{resolution}] [{audio}] - {group}"
    special_template: str = (
        "{short_title} - {content_type} E{episode} [{resolution}] [{audio}] - {group}"
    )


class MetadataConfig(BaseModel):
    enabled: bool = True
    update_title: bool = True
    update_author: bool = True
    update_comment: bool = True
    update_tags: bool = True
    update_description: bool = True
    supported_containers: list[str] = Field(default_factory=lambda: ["mkv", "mp4", "avi", "mov"])


class ThumbnailConfig(BaseModel):
    enabled: bool = True
    attach_to_video: bool = True
    attach_to_document: bool = True
    generate_previews: bool = True


class WatermarkConfig(BaseModel):
    # Watermark policy:
    #   "off"    — never burn a watermark.
    #   "always" — always burn it on the highest-res source.
    #   "auto"   — burn ONLY when subtitle branding wasn't possible, i.e. the
    #              file has no brandable TEXT subtitle track (PGS/image subs, or
    #              no subs). When a text sub is present our channel cue is injected
    #              there instead, so the video is left un-watermarked. This is the
    #              default: watermarking becomes a smart fallback for PGS releases.
    mode: str = "auto"
    # Vestigial master switch kept for back-compat with older configs. ``mode``
    # now drives behaviour; the stage runs whenever ``mode != "off"``.
    enabled: bool = False
    type: str = "text"
    text: str = "Anime Weebs"
    image_path: str = ""
    # Font file name under resources/fonts/ for the text watermark. "" = ffmpeg's
    # built-in default font. Picked from the shipped fonts in the Levi settings UI.
    font: str = ""
    corner: str = "bottom_right"
    # Distance in pixels from each frame edge the watermark hugs (per the chosen
    # corner). Default 16px.
    margin: int = 16
    opacity: float = 0.55
    # Text watermark size as a fraction of frame HEIGHT (0.03 ≈ one step larger
    # than a typical player's small corner mark). For an image watermark this is
    # a fraction of frame WIDTH (legacy meaning).
    scale: float = 0.03
    # Fast burn: use a hardware H.264 encoder (NVENC/QSV/VAAPI) when the box has
    # one, else libx264 — dramatically faster than re-encoding back to the
    # source's HEVC/AV1 on CPU. A corner mark's quality is dominated by the
    # source, so this trades a little 1080p size for a large wall-clock win.
    # Set false to keep the slower codec-aware (source-matching) re-encode.
    fast: bool = True


class BrandingConfig(BaseModel):
    enabled: bool = True
    channel_name: str = "Anime Weebs"
    footer_text: str = "Anime Weebs"
    website: str = ""
    telegram_channel: str = ""
    community_link: str = ""
    watermark_text: str = "Anime Weebs"
    metadata_author: str = "Anime Weebs"
    metadata_comment: str = "Provided by Anime Weebs"


class DistributionConfig(BaseModel):
    mode: str = "season_package"
    protect_content: bool = True
    temporary_links: bool = True
    link_expiry_minutes: int = 60
    auto_delete: bool = False
    auto_delete_after_minutes: int = 60


class QueueConfig(BaseModel):
    max_visible: int = 10
    position_recalc_seconds: int = 5


class SecurityConfig(BaseModel):
    rate_limit_per_minute: int = 20
    anti_spam_cooldown_seconds: int = 2
    # NekoFetch (admin) bot force-subscription.
    force_subscribe: bool = False
    force_subscribe_channels: list[int] = Field(default_factory=list)
    # Distribution bots force-subscription (separate from admin bot).
    dist_force_subscribe: bool = False
    dist_force_subscribe_channels: list[int] = Field(default_factory=list)
    owner_id: int = 0


class StorageChannelConfig(BaseModel):
    """The database channel where content packs live (header -> files -> end sticker)."""

    enabled: bool = False
    channel_id: int = 0                       # -100... id of the database channel
    # Header text posted before each pack. Variables: {title} {season} {resolution}
    # {language} {episode_from} {episode_to} {content_type} {group}
    header_template: str = "{title} — Season {season} [{resolution}] [{language}]"
    # Per-type header overrides (empty = fall back to header_template). Seasons get
    # a season number; movies/OVAs/specials don't, so their headers read naturally
    # instead of "Season —". Same variables as header_template.
    movie_header_template: str = "{title} — {content_type} [{resolution}] [{language}]"
    special_header_template: str = "{title} — {content_type} [{resolution}] [{language}]"
    end_sticker_id: str = ""                  # file_id of the end-of-pack sticker
    copy_mode: str = "copy"                   # copy | forward
    include_header_in_delivery: bool = True
    include_sticker_in_delivery: bool = False


class LogChannelConfig(BaseModel):
    """The operational control center: one channel of persistent, edited-in-place
    section messages (dashboard, pending, active, completed, notices, catalog)
    plus a pool of preallocated reserved messages used when a section message can
    no longer be edited (Telegram's ~48h edit window)."""

    enabled: bool = False
    channel_id: int = 0
    pinned_dashboard: bool = True             # live stats summary (edited in place)
    pinned_catalog: bool = True               # published catalog index (edited in place)
    sections: bool = True                     # full sectioned control center
    reserved_slots: int = 2                   # reserved msgs per growth-prone section
    notices_lines: int = 12                   # rolling event-stream length
    # Sticker posted between sections as a permanent visual divider.
    divider_sticker_id: str = (
        "CAACAgUAAxkBAAI0vGpAOaZ7gJ6Yk9MtJ63jm0sYmDysAAIYAANDc8kSzixbXL29lfc8BA"
    )
    # Cover image at the very top of the channel (URL or file_id). Empty = skip.
    cover_image: str = ""
    refresh_seconds: int = 60                 # full rebuild of all sections
    # The active-tasks panel gets a fast lane: live downloads/processing update on
    # this short interval so the progress bar feels responsive, while the heavier
    # dashboard/catalog/completed panels stay on the slower full refresh above.
    active_refresh_seconds: int = 5
    # 'all' = everything; otherwise a subset of categories to forward.
    events: list[str] = Field(default_factory=lambda: ["all"])

    # When True, every rebuild also wipes any message newer than the cover/
    # intro — even when posted by a different admin or by another user.
    # Only messages more recent than the intro message are touched (older
    # history is preserved). Capped by ``wipe_max_history`` so a runaway
    # bug can't nuke weeks of context. Set False for deployments where
    # admins draft notes in the channel between rebuilds.
    wipe_all_on_rebuild: bool = True
    # Safety cap on the number of recent messages the wipe sweeps — keeps
    # a single Telegram history-window fetch well under any flood-wait
    # threshold while still clearing the relevant working set.
    wipe_max_history: int = 200


class ThumbnailChannelConfig(BaseModel):
    """Thumbnail control center channel — asset selection & generation workflow.

    A dedicated private channel where admins browse assets via Telegraph galleries
    and select logos, posters, and backdrops for custom thumbnail generation.
    """

    enabled: bool = False
    channel_id: int = 0
    cover_image: str = ""                  # URL or file_id for the channel intro
    # Sticker posted between sections as a visual divider.
    divider_sticker_id: str = (
        "CAACAgUAAxkBAAI0vGpAOaZ7gJ6Yk9MtJ63jm0sYmDysAAIYAANDc8kSzixbXL29lfc8BA"
    )
    # Telegraph API access token for creating galleries.
    telegraph_access_token: str = ""
    # Maximum entries in the thumbnail queue at once.
    max_queue_size: int = 20

    # When True, every rebuild also wipes any message newer than the intro
    # — even when posted by a different admin or another user. Older history
    # (before the intro) is preserved. Capped by ``wipe_max_history`` so a
    # runaway bug can't nuke weeks of context.
    wipe_all_on_rebuild: bool = True
    # Safety cap on the number of recent messages the wipe sweeps.
    wipe_max_history: int = 200


class AccessConfig(BaseModel):
    """Time-based access: a free trial, then renew via a shortlink token."""

    enabled: bool = False
    free_trial: bool = True
    trial_days: int = 3
    token_days: int = 3
    token_link_ttl_hours: int = 24       # how long a generated token link stays valid
    forward_to_saved_hint: bool = True   # nudge users to forward files to Saved Messages


class ShortlinkConfig(BaseModel):
    """URL shortener used to gate token generation (AroLinks / VPLinks)."""

    enabled: bool = False
    provider: str = "vplinks"            # arolinks | vplinks
    arolinks_api_key: str = ""           # AroLinks API token
    vplinks_api_key: str = ""            # VPLinks API token
    api_token: str = ""                  # generic api token (legacy)
    base_url: str = ""                   # generic provider base url (legacy)


class AcquisitionConfig(BaseModel):
    """What to fetch when a request doesn't pin a specific quality/language.

    A request with no resolution/audio fans out into the full matrix below. ``languages``
    map to audio tracks: english = Dub, japanese = Sub (always with English subtitles).
    """

    resolutions: list[str] = Field(default_factory=lambda: ["360p", "540p", "720p", "1080p"])
    languages: list[str] = Field(default_factory=lambda: ["english", "japanese"])
    require_english_subs: bool = True
    # Mandatory qualities to grab for every request (best-first). Each is fetched
    # when the source offers it; 480p is special-cased with a fallback ladder
    # below so we never ship nothing at the SD tier.
    target_resolutions: list[str] = Field(
        default_factory=lambda: ["1080p", "720p", "480p"]
    )
    # When a target resolution is missing, try these alternates in order. Only the
    # first available alternate is taken, so we don't double up the same tier.
    resolution_fallbacks: dict[str, list[str]] = Field(
        default_factory=lambda: {"480p": ["540p", "360p"]}
    )


class MainChannelConfig(BaseModel):
    """The public 'main' channel where each published anime is posted."""

    enabled: bool = False
    channel_id: int = 0
    # Variables: {title} {tag} {episodes} {qualities} {languages} {genres} {overview}
    # {title} arrives pre-rendered as "<b>English</b>〢Romaji" (same header the
    # entry posts use), so the template must NOT re-wrap it in <b>.
    caption_template: str = (
        "<blockquote>{title}<b>『 </b>#{tag} <b>』</b></blockquote>\n\n"
        "<b>⌬ EPISODES :</b> {episodes}\n"
        "<b>⌬ QUALITY :</b> {qualities}\n"
        "<b>⌬ LANGUAGE :</b> {languages}\n"
        "<b>⌬ GENRE :</b> {genres}\n\n"
        "<blockquote expandable><b>‣ OverView :</b> {overview}</blockquote>"
    )
    index_button_text: str = "ɪɴᴅᴇx"
    download_button_text: str = "ᴅᴏᴡɴʟᴏᴀᴅ"
    # Default notification mode for a main-channel publish. True = post silently
    # (no member notification) unless the operator overrides per-publish.
    silent_default: bool = False


class IndexChannelConfig(BaseModel):
    """A channel holding stylized, per-letter index posts the bot maintains."""

    enabled: bool = False
    channel_id: int = 0
    # Rendered per first-letter. Variables: {letter} {entries}
    letter_header_template: str = "•──────────•°• {letter} •°•──────────•"
    entry_template: str = "⦿ {title}"
    # Public username of the index channel (no @). Drives the t.me/<username>/<mid>
    # links on the poster and letter buttons. Made config-driven so a restore onto
    # a fresh channel (after a ban) rewrites every link to the new channel.
    username: str = "AniXWeebs_Index"
    # Message id of the pinned poster (the letter-grid navigation post). Updated by
    # a restore so "Go to Top" and the poster grid point at the rebuilt poster.
    poster_message_id: int = 171
    # Link the letter buttons' "Main Channel" button targets.
    main_channel_link: str = "https://t.me/AniXWeebs"


class MiruroConfig(BaseModel):
    api_base_url: str = "http://localhost:8000"
    stream_referer: str = "http://localhost:8000"
    provider_order: list[str] = Field(
        default_factory=lambda: ["kiwi", "arc", "zoro", "hop", "pahe"]
    )


class SourcesConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "local", "telegram", "anikoto", "anizone", "kickassanime", "miruro", "nyaa",
        ]
    )
    default: str = "telegram"
    miruro: MiruroConfig = Field(default_factory=MiruroConfig)


class UIConfig(BaseModel):
    # Shared /start sticker — the admin (NekoFetch) bot and delivery bots use it,
    # and it's the fallback for any pipeline bot without its own sticker below.
    start_sticker_id: str = (
        "CAACAgUAAyEFAASAgUwqAAJh_mckw2STkeY1WMOHJGY4Hs9_1-2fAAIPFAACYLShVon-N6AFLnIiHgQ"
    )
    # Per-bot /start stickers for the four pipeline personas. Empty = fall back to
    # ``start_sticker_id`` above. Set a Telegram sticker file_id to give a bot its
    # own opening sticker.
    start_sticker_lelouch: str = ""
    start_sticker_levi: str = ""
    start_sticker_senku: str = ""
    start_sticker_gojo: str = ""
    start_image_url: str = "https://envs.sh/odE.png"
    start_image_has_spoiler: bool = True
    sticker_delete_delay: float = 1.5
    loading_dot_delay: float = 0.32
    loading_steps: int = 3

    def sticker_for(self, bot_name: str | None) -> str:
        """Return the /start sticker for ``bot_name`` (lelouch/levi/senku/gojo),
        falling back to the shared ``start_sticker_id`` when the bot has none set."""
        if bot_name:
            specific = getattr(self, f"start_sticker_{bot_name.lower()}", "")
            if specific:
                return specific
        return self.start_sticker_id


class LocalizationConfig(BaseModel):
    default_language: str = "en"
    directory: str = "resources/language"


class PostFormatConfig(BaseModel):
    """How every published channel card is rendered.

    This is the single home for the *look* of distribution-channel posts: the
    info / season / movie / extras cards, the watch guide, the footer, the
    resolution buttons, and the language labels. Each template mirrors the
    matching ``bot_*`` string in ``en.json`` — the defaults here reproduce the
    shipped layout exactly, so an operator who never touches Settings sees no
    change, while one who wants a different voice can override any single field
    without editing source or the language catalog.

    Empty string means "fall back to the ``en.json`` default" for the template
    fields, so clearing a field in the panel restores the built-in look rather
    than blanking the card.

    Premium (custom) emoji: every template is rendered with Telegram HTML, so a
    ``<tg-emoji emoji-id="123">🎬</tg-emoji>`` span passes straight through to
    the channel. ``premium_emoji`` maps a short ``:name:`` token to a custom
    emoji id; :func:`resolve_premium_emoji` expands ``:name:`` tokens in any
    rendered caption so operators can reuse the same premium glyph across every
    template without pasting the raw span each time. Left empty, nothing is
    substituted and the plain unicode emoji shows — so this is safe to ignore
    until a premium account is wired up.
    """

    # ── card templates (empty = use the en.json default) ──────────────────────
    info_card_template: str = ""       # bot_info_card
    season_card_template: str = ""     # bot_season_card
    movie_card_template: str = ""      # bot_movie_card
    extras_card_template: str = ""     # extras reuse the season/movie card by rule
    watch_guide_template: str = ""     # bot_watch_guide (wraps {seasons})
    watch_guide_season_line: str = ""  # bot_watch_guide_season
    watch_guide_extra_line: str = ""   # bot_watch_guide_extra

    # ── footer ────────────────────────────────────────────────────────────────
    # (footer_text / footer_image_url also live on BotConfig for backwards compat;
    #  these mirror them so the whole post look sits in one section. When empty,
    #  BotConfig.footer_* — then bot_footer in en.json — wins.)
    footer_template: str = ""
    footer_image_url: str = ""

    # ── resolution buttons ─────────────────────────────────────────────────────
    # Label wrapper for a quality button. {res} is the resolution (e.g. 1080p).
    # Use to add symbols front/back, e.g. "「 {res} 」" or "⬢ {res}".
    resolution_label: str = "{res}"
    # Buttons per keyboard row. 2 gives the reference layout: 2->[2], 3->[2,1],
    # 4->[2,2]. Set 1 for a single column, 3 for three-wide.
    buttons_per_row: int = 2
    # Cap on how many distinct qualities become buttons (reference shows 3).
    max_quality_buttons: int = 3

    # ── language section labels (separate-audio / sub-only layout) ─────────────
    # Header rows shown above each language's quality buttons when a title has
    # separate sub & dub packs (no dual-audio file). {lang} is the language name.
    language_label_japanese: str = ""  # bot_lang_japanese
    language_label_english: str = ""   # bot_lang_english
    # Japanese first mirrors the reference channels (original audio leads).
    japanese_first: bool = True

    # ── pinning / dividers ──────────────────────────────────────────────────────
    pin_info_card: bool = True
    pin_watch_guide: bool = True
    divider_sticker_id: str = ""       # empty = fall back to BotConfig.divider_sticker_id

    # ── duration formatting for movies / single-episode extras ─────────────────
    # A single-episode entry shows DURATION (from AniList minutes) instead of an
    # episode count; a multi-episode extra shows EPISODES. {h}=hours {m}=minutes.
    duration_format_hm: str = "{h}h {m}m"   # used when hours >= 1
    duration_format_m: str = "{m}m"          # used when under an hour

    # ── premium emoji (forward-compat; empty = plain unicode) ──────────────────
    premium_emoji: dict[str, str] = Field(default_factory=dict)  # :name: -> custom_emoji_id


class BotConfig(BaseModel):
    """Distribution-bot creation and content configuration."""

    auto_create_on_publish: bool = True
    health_check_interval_minutes: int = 60
    delivery_retention_days: int = 7
    avatar_source: str = "tmdb"  # "tmdb" | "anilist"
    # External file-store bot usernames for delivery links.
    # These are separate Telegram bot instances that serve files to users.
    # Bot tokens/API credentials are configured on the Fstore deployment side.
    filestore_bots: list[str] = Field(
        default_factory=list,
        description=(
            "Comma-separated usernames of Fstore file-delivery bots "
            "(e.g. KiloxBot, MarkySayBot)"
        ),
    )
    # Footer shown on the last post of every distribution bot.
    footer_image_url: str = ""   # URL or Telegram file_id; empty = no image
    footer_text: str = ""        # override the built-in bot_footer template (empty = use en.json)
    # Operator overrides for the bot's Telegram profile text. The defaults
    # bake in the AniXWeebs branding block (see BotFactory._BRANDING_*),
    # but operators can drop in their own copy without forking the bot.
    description_text: str = ""   # full /setdescription body (Telegram: 512-char cap)
    about_text: str = ""         # short /setabouttext    (Telegram: 120-char cap)
    # Divider sticker sent between content sections (info → seasons → guide → footer).
    divider_sticker_id: str = (
        "CAACAgUAAxkBAAI5pmpE1uh9_sD-z2tYJ3wlado6vS29AAIYAANDc8kSzixbXL29lfc8BA"
    )
    # Per-account limits for distribution entities. When the bot limit is exhausted
    # for a userbot session, the orchestrator falls back to creating public channels.
    max_bots_per_account: int = 20
    max_channels_per_account: int = 10
    # Days between comprehensive entity health checks (bots + channels).
    # Set to 0 to disable the scheduled full sweep (periodic bot checks still run).
    entity_full_check_days: int = 30
    # Days between the scheduled monthly maintenance jobs Gojo runs. The update
    # sweep is detect-only: it DMs Gojo admins a reviewable list (nothing is
    # auto-created). The ban check probes every channel and auto-recovers down
    # distribution channels. Set either to 0 to disable that scheduled job (the
    # manual /updates and /bancheck commands always remain available).
    update_check_interval_days: int = 30
    ban_check_interval_days: int = 30
    # Master on/off for each scheduled monthly job, independent of the interval
    # above. Off = the job never fires on the scheduler (manual command still works).
    monthly_update_check_enabled: bool = True
    monthly_ban_check_enabled: bool = True
    # Periodic wipe-proof backup refresh (main-channel posts + index sections).
    # Publish-time capture only refreshes the titles being published; a footer
    # edit, index reshuffle, or manual channel tweak between publishes would
    # otherwise not reach the ban-restore snapshot until the next publish. This
    # scheduled job re-runs backup_all + record_index so the snapshot stays
    # current. Set to 0 to disable (manual /backup always remains available).
    periodic_backup_hours: int = 24
    # Restore pacing for a whole-channel main-channel restore (Change Main). 0 posts
    # as fast as the API allows; a positive ``restore_batch_size`` posts that many
    # then sleeps ``restore_batch_delay_seconds`` before the next batch — gentler on
    # Telegram's flood limits for a large catalog.
    restore_batch_size: int = 0
    restore_batch_delay_seconds: float = 0.0
    # Image-host mirror order for durable backups (image_backup). A comma-free list
    # of host keys tried in order; the first that sticks becomes the primary URL.
    # Recognized: "catbox", "telegraph", "imgbb". Unknown keys are ignored. ImgBB
    # needs IMGBB_API_KEY set in .env, else that host is skipped.
    # Upload/mirror order for card images. ImgBB leads — it's the host that
    # actually sticks (catbox intermittently 500s/disconnects, telegraph killed
    # its upload endpoint). primary() picks imgbb → catbox → telegraph → source.
    image_host_order: list[str] = Field(
        default_factory=lambda: ["imgbb", "catbox", "telegraph"]
    )
    # Username suffix formatting. The base suffix is shared; ``format_bot_username``
    # appends "_bot" for bot entities (Telegram requirement) and leaves it off for
    # channels. Both entities use the same base, but only bots get the "_bot" tail.
    bot_username_suffix: str = "axw"
    channel_username_suffix: str = "axw"


class AppConfig(BaseModel):
    """Typed view of config.yaml. Every section is optional with sane defaults."""

    features: Features = Field(default_factory=Features)
    downloads: DownloadsConfig = Field(default_factory=DownloadsConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    rename: RenameConfig = Field(default_factory=RenameConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    thumbnail: ThumbnailConfig = Field(default_factory=ThumbnailConfig)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    distribution: DistributionConfig = Field(default_factory=DistributionConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    storage_channel: StorageChannelConfig = Field(default_factory=StorageChannelConfig)
    log_channel: LogChannelConfig = Field(default_factory=LogChannelConfig)
    thumbnail_channel: ThumbnailChannelConfig = Field(default_factory=ThumbnailChannelConfig)
    main_channel: MainChannelConfig = Field(default_factory=MainChannelConfig)
    index_channel: IndexChannelConfig = Field(default_factory=IndexChannelConfig)
    acquisition: AcquisitionConfig = Field(default_factory=AcquisitionConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    shortlink: ShortlinkConfig = Field(default_factory=ShortlinkConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    localization: LocalizationConfig = Field(default_factory=LocalizationConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    post_format: PostFormatConfig = Field(default_factory=PostFormatConfig)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> AppConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    """Cached environment settings (loaded once per process)."""
    return EnvSettings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    """Cached static config.yaml snapshot. Runtime overrides are applied by ConfigService."""
    return AppConfig.load()
