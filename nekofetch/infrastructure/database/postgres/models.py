"""PostgreSQL ORM models — structured, transactional data.

Flexible content (anime metadata, artwork, templates, runtime settings) lives in
MongoDB; this schema holds the relational backbone. Anime are referenced here by
their MongoDB id (``anime_doc_id``) so the two stores stay loosely coupled.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nekofetch.domain.enums import (
    AudioType,
    BotKind,
    JobStatus,
    RequestStatus,
    Role,
)
from nekofetch.infrastructure.database.postgres.base import (
    Base,
    EnumStr,
    PKMixin,
    TimestampMixin,
)


class User(Base, PKMixin, TimestampMixin):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[Role] = mapped_column(EnumStr(Role), default=Role.USER, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Time-based access (trial / token renewals). None = never granted yet.
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    requests: Mapped[list["Request"]] = relationship(back_populates="user")


class Request(Base, PKMixin, TimestampMixin):
    __tablename__ = "requests"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)  # REQ-1048
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)

    anime_doc_id: Mapped[str | None] = mapped_column(String(48), index=True)
    anime_title: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # which source plugin
    # Source-native identifier. For nyaa this is the full torrent metadata blob
    # json.dumps'd (title + torrent_url + view_url + seeders/leechers/downloads +
    # size + category + audio kind). Variable length; widened to ``Text`` so
    # richly-described releases don't trip asyncpg's VARCHAR length check.
    # Migration: 20260711_0004_widen_request_source_ref.
    source_ref: Mapped[str | None] = mapped_column(Text)             # source-native id (e.g. torrent blob)

    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    season: Mapped[int | None] = mapped_column(Integer)
    episodes: Mapped[list | None] = mapped_column(JSONB)             # selected episode numbers
    resolution: Mapped[str | None] = mapped_column(String(16))
    audio: Mapped[AudioType | None] = mapped_column(EnumStr(AudioType))

    status: Mapped[RequestStatus] = mapped_column(
        EnumStr(RequestStatus), default=RequestStatus.PENDING, index=True, nullable=False
    )
    position: Mapped[int | None] = mapped_column(Integer)

    # Phase 1 franchise data — JSON blob with the full AniList relation graph
    # so downstream sourcing knows the complete connected universe.
    franchise_data: Mapped[dict | None] = mapped_column(JSONB)

    user: Mapped["User"] = relationship(back_populates="requests")
    jobs: Mapped[list["DownloadJob"]] = relationship(back_populates="request")


class DownloadJob(Base, PKMixin, TimestampMixin):
    __tablename__ = "download_queue"

    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        EnumStr(JobStatus), default=JobStatus.QUEUED, index=True, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # Live progress (also mirrored to Redis for fast UI reads)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)   # 0..100
    speed_bps: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_episode: Mapped[int | None] = mapped_column(Integer)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)

    # Resume support
    resume_state: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    request: Mapped["Request"] = relationship(back_populates="jobs")
    files: Mapped[list["MediaFile"]] = relationship(back_populates="job")


class MediaFile(Base, PKMixin, TimestampMixin):
    __tablename__ = "files"

    job_id: Mapped[int | None] = mapped_column(ForeignKey("download_queue.id"), index=True)
    anime_doc_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)

    season: Mapped[int | None] = mapped_column(Integer)
    season_part: Mapped[int | None] = mapped_column(Integer, default=None)
    episode: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[str | None] = mapped_column(String(16))
    audio: Mapped[AudioType | None] = mapped_column(EnumStr(AudioType))

    original_name: Mapped[str | None] = mapped_column(String(512))
    final_name: Mapped[str | None] = mapped_column(String(512))
    local_path: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128))
    container: Mapped[str | None] = mapped_column(String(8))

    # Telegram delivery references (populated once uploaded to a storage chat)
    tg_file_id: Mapped[str | None] = mapped_column(String(256))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)

    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    job: Mapped["DownloadJob | None"] = relationship(back_populates="files")

    __table_args__ = (
        Index("ix_files_locator", "anime_doc_id", "season", "episode", "resolution", "audio"),
    )


class DistributionBot(Base, PKMixin, TimestampMixin):
    __tablename__ = "bots"

    kind: Mapped[BotKind] = mapped_column(EnumStr(BotKind), default=BotKind.DISTRIBUTION, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    bot_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)

    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted
    anime_doc_id: Mapped[str | None] = mapped_column(String(48), index=True)  # bound title, if any
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB)  # per-bot overrides

    # Channel vs bot: bots get a token and bot_user_id; channels get a chat_id.
    # ``is_channel`` is True when this row represents a public channel rather than
    # a bot. Channels don't run Pyrogram clients — they're posted to directly.
    is_channel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # -100… for channels

    # A bot-minted **private** invite link to this channel (t.me/+…), deliberately
    # NOT the public t.me/<username> link: the main-channel Download button and the
    # index hyperlink point here so traffic flows through a link we control and can
    # revoke/replace. Re-minted on recreate (the old channel — and its link — is
    # gone), then swapped into the main-channel post's button and the index entry.
    # NULL for bots (they use a ?start deep link) and for channels created before
    # this column existed (they fall back to the public username link).
    invite_link: Mapped[str | None] = mapped_column(Text)

    # Monotonic counter incremented every time ``BotContentService.generate_posts``
    # rebuilds this bot's content set. The distribution bot compares it against a
    # user's stored ``BotDelivery.delivered_revision`` on /start to decide whether
    # the user's view is stale and needs a delete-then-redeliver dance.
    content_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # How this channel was created: "own" (an admin made it and added the bots as
    # admins) or "userbot" (a pooled userbot session created it). NULL for bots and
    # for channels created before scope tracking. Drives quota accounting below.
    creation_scope: Mapped[str | None] = mapped_column(String(16))
    # Name of the userbot account (``Account.name``) that owns a "userbot"-scoped
    # channel — used to tally each session's channel count against its quota so a
    # session at capacity is skipped. NULL for "own"-scoped channels and bots.
    userbot_account: Mapped[str | None] = mapped_column(String(64), index=True)


class AccessLink(Base, PKMixin, TimestampMixin):
    """Temporary / protected access tokens for season packages."""

    __tablename__ = "access_links"

    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # what it grants
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class StoragePack(Base, PKMixin, TimestampMixin):
    """A season pack stored as a message range in the database channel.

    Layout in the channel (the file-sharing-bot pattern):

        header text  ->  file 1, 2, 3 ... N (in order)  ->  end sticker

    Delivery copies the recorded range to the user. A pack is unique per
    (anime, season, resolution, language).
    """

    __tablename__ = "storage_packs"

    anime_doc_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    anime_title: Mapped[str] = mapped_column(String(256), nullable=False)
    season: Mapped[int | None] = mapped_column(Integer)
    season_part: Mapped[int | None] = mapped_column(Integer, default=None)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    audio: Mapped[AudioType] = mapped_column(EnumStr(AudioType), nullable=False)

    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    header_message_id: Mapped[int | None] = mapped_column(BigInteger)
    start_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)   # first file
    end_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)     # end sticker / last
    file_message_ids: Mapped[list | None] = mapped_column(JSONB)                # ordered, explicit
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Exact HTML caption used for the header message.
    caption: Mapped[str | None] = mapped_column(Text)

    episode_from: Mapped[int | None] = mapped_column(Integer)
    episode_to: Mapped[int | None] = mapped_column(Integer)

    # AniList entry ID for per-extra tracking (MOVIE/OVA/ONA/SPECIAL).
    # Null for TV seasons or legacy entries; extras use this to distinguish
    # which specific entry a pack belongs to.
    entry_id: Mapped[int | None] = mapped_column(Integer, index=True)

    ingest_method: Mapped[str | None] = mapped_column(String(16))  # indexed | uploaded
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "anime_doc_id", "season", "season_part", "resolution", "audio",
            "entry_id", name="uq_storage_pack"
        ),
        Index("ix_storage_pack_lookup", "anime_doc_id", "season", "season_part", "resolution", "audio", "entry_id"),
    )


class ThumbnailSource(Base, PKMixin, TimestampMixin):
    """Durable inputs used to render one entry thumbnail.

    Keeping the selected artwork and gathered metadata means an owner can edit or
    regenerate a card after a restart without depending on the temporary Redis
    workflow or a provider returning the same result.
    """

    __tablename__ = "thumbnail_sources"

    anime_doc_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    # ``-1`` represents a mapping-only/root thumbnail with no AniList id. A
    # non-null key is intentional: PostgreSQL treats NULLs as distinct in a
    # UNIQUE constraint, which would allow duplicate root rows during retries.
    anilist_id: Mapped[int] = mapped_column(BigInteger, index=True, default=-1, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    html: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("anime_doc_id", "anilist_id", name="uq_thumbnail_source_entry"),
    )


class ChannelPost(Base, PKMixin, TimestampMixin):
    """Tracks where an anime has been posted (main channel post + index entry).

    Lets the bot edit/update those posts in place instead of reposting.
    """

    __tablename__ = "channel_posts"

    anime_doc_id: Mapped[str] = mapped_column(String(48), unique=True, index=True, nullable=False)
    main_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    main_message_id: Mapped[int | None] = mapped_column(BigInteger)
    index_letter: Mapped[str | None] = mapped_column(String(2))
    index_message_id: Mapped[int | None] = mapped_column(BigInteger)


class PublishedPostBackup(Base, PKMixin, TimestampMixin):
    """A byte-for-byte snapshot of a main-channel post, for disaster recovery.

    When the main channel is banned we rebuild every post on a fresh channel
    from these rows alone — no re-rendering, no re-fetching metadata. Each row
    stores everything needed to reproduce the exact message: the finished
    caption HTML, the photo (mirrored onto independent hosts so it outlives the
    original CDN), the structured button layout, and the divider sticker that
    preceded it. ``source_message_id`` ties the backup to the live post so a
    re-backup updates in place instead of duplicating.
    """

    __tablename__ = "published_post_backups"

    anime_doc_id: Mapped[str] = mapped_column(
        String(48), unique=True, index=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    # The exact rendered caption (finished HTML, styling preserved).
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Image mirrors: original CDN URL + the durable copies (one column per host).
    image_source_url: Mapped[str | None] = mapped_column(Text)
    image_catbox_url: Mapped[str | None] = mapped_column(Text)
    image_telegraph_url: Mapped[str | None] = mapped_column(Text)
    # ImgBB full-resolution mirror (data.url — never the thumb/medium size). NULL
    # for rows backed up before ImgBB replaced the dead envs.sh last-resort host.
    image_imgbb_url: Mapped[str | None] = mapped_column(Text)
    # Structured button rows: [[{"text","url"|"callback_data"}, …], …].
    button_data: Mapped[list | None] = mapped_column(JSONB)
    # Divider sticker file_id posted before the card (channel layout detail).
    divider_sticker_id: Mapped[str | None] = mapped_column(Text)
    # Where the original lived, so a re-backup updates in place.
    source_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)


class ChannelContentBackup(Base, PKMixin, TimestampMixin):
    """Wipe-proof snapshot of a distribution / index channel's content pack.

    :class:`PublishedPostBackup` covers the *main* channel. This is its sibling
    for the other two scopes:

    * **distribution** — a per-title channel's ordered card list (info card,
      season/movie cards, watch guide, footer) with each card's finished caption
      HTML, its image mirrored to a durable host, its structured Download
      buttons, and its pin flag. Distinct from the live ``BotContentPost`` rows
      (which :meth:`BotOrchestratorService.recreate_bot` *deletes* before it
      regenerates) and from ``ChannelLayout`` (message ids only, no content) —
      so a banned channel can be re-posted **verbatim** on a fresh chat with no
      re-render and no re-fetch.
    * **index** — the letter-section posts (caption HTML + poster image) so the
      index channel can be rebuilt the same way.

    One row per channel, keyed by ``(scope, channel_key)`` — ``channel_key`` is
    the bound ``anime_doc_id`` for a distribution channel and the fixed literal
    ``"index"`` for the single index channel — so a re-capture upserts in place.
    ``footer_message_id`` is the live footer's message id (Phase 4 universal
    footer edits target it); ``cards`` is the ordered send-list JSON.
    """

    __tablename__ = "channel_content_backups"

    # "distribution" | "index"
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    # anime_doc_id for a distribution channel; "index" for the index channel.
    channel_key: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    # The live chat this snapshot came from (informational / re-capture match).
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # Ordered card list — one dict per posted message, in send order:
    #   {"kind", "caption", "image_url", "button_data", "is_pinned",
    #    "anilist_id", "divider_before"}.
    cards: Mapped[list | None] = mapped_column(JSONB)
    # Live footer message id (target of Phase-4 universal footer edits).
    footer_message_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("scope", "channel_key", name="uq_channel_backup_scope_key"),
    )


class ChannelLayout(Base, PKMixin, TimestampMixin):
    """The ordered message layout of a distribution channel's content pack.

    When Senku (or the auto pipeline) publishes a channel, every message it
    posts — info card, season/extra cards, dividers, watch guide, footer — gets
    one row here in send order (``seq``). This lets a later *incremental* update
    (a new franchise entry finishing the pipeline) find the exact footer/divider
    message ids, delete just those, append the new card(s), and re-post a fresh
    divider + footer — without re-rendering the whole channel or touching the
    main channel. ``anilist_id`` ties a card to the entry it shows so a re-run
    never double-posts the same season.
    """

    __tablename__ = "channel_layout"

    channel_bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # info_card | season_card | movie_card | watch_guide | divider | footer
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    anilist_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChannelBroadcast(Base, PKMixin, TimestampMixin):
    """One broadcast message posted into a distribution channel.

    An operator can push a single announcement to *every* distribution channel
    at once (see :class:`BroadcastService`). Each delivered copy gets one row
    here so a scheduled auto-deletion survives a restart: ``delete_at`` is when
    the message should be removed (``None`` = permanent), and the scheduler's
    :meth:`BroadcastService.sweep_expired` job deletes any past-due, not-yet-
    deleted row. ``batch_id`` ties every copy of one broadcast together for
    reporting. The main channel is never a target — this is channels only.
    """

    __tablename__ = "channel_broadcasts"

    batch_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )  # None = permanent
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScheduledPost(Base, PKMixin, TimestampMixin):
    """A main-channel publish deferred to a future time.

    APScheduler jobs are in-memory and their callables aren't serializable, so a
    restart would silently forget every pending scheduled publish. This row is
    the durable source of truth: :meth:`ScheduleService.sweep_due` (a 60s
    scheduler job, same pattern as the broadcast/link sweeps) publishes every
    past-due ``pending`` row and marks it ``published``/``failed``, so a schedule
    survives restarts and is never double-fired.

    ``scheduled_at`` is stored in UTC (tz-aware) like everything else; each admin
    enters and reads it in their own timezone (``AdminAvailability.timezone``).
    """

    __tablename__ = "scheduled_posts"

    request_code: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    anime_title: Mapped[str | None] = mapped_column(Text)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    silent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    caption_override: Mapped[str | None] = mapped_column(Text)
    # "pending" | "published" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class IndexSection(Base, PKMixin, TimestampMixin):
    """Dynamic index-channel section mapping.

    Each row represents one photo post slot in the index channel, ordered by
    ``sort_order``. The ``label`` is the displayed letter (e.g. "A", "A(2)",
    "B") and ``message_id`` is its Telegram message ID. Reserved (unused)
    slots have ``label = None`` and sit at the end; the poster is tracked via
    a config value rather than this table.

    When a letter overflows Telegram's 1024-char caption limit, the next slot
    is rebranded, all subsequent slots shift down, and the last reserved slot
    is consumed. The poster buttons are rebuilt after every shift.
    """

    __tablename__ = "index_sections"

    sort_order: Mapped[int] = mapped_column(
        Integer, unique=True, index=True, nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(16))         # e.g. "A", "A(2)", "B", None
    base_letter: Mapped[str | None] = mapped_column(String(4))    # e.g. "A", "B" — the original letter
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    # An admin repurposed this reserved slot into a normal post (edited away its
    # "RESERVED FOR FUTURE" / "Slot N/N" marker). Auto-indexing must treat it as
    # off-limits: never rebrand, shift into, or consume it — it's no longer a slot.
    repurposed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AccessToken(Base, PKMixin, TimestampMixin):
    """A renewal token a user redeems (after completing a shortlink) for more access time."""

    __tablename__ = "access_tokens"

    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalyticsEvent(Base, PKMixin):
    __tablename__ = "analytics_events"

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    anime_doc_id: Mapped[str | None] = mapped_column(String(48), index=True)
    data: Mapped[dict | None] = mapped_column(JSONB)


class BotContentPost(Base, PKMixin, TimestampMixin):
    """Pre-generated content posts for a distribution bot.

    When a bot is created for an anime, we generate a set of posts (watch guide,
    season cards, info/overview, footer) that are stored here and delivered in
    order when a user starts the bot. The admin can edit these via settings.

    Images are uploaded once at generate time to a public file host (catbox.moe
    via ``providers/catbox.py``); ``image_cached_url`` is preferred at /start so
    Telegram doesn't refetch from TMDB/AniList CDNs on every delivery.
    """

    __tablename__ = "bot_content_posts"

    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    post_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "watch_guide" | "season_card" | "movie_card" | "info_card" | "footer"
    season: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[str | None] = mapped_column(String(16))
    audio: Mapped[str | None] = mapped_column(String(16))  # subbed/dubbed/dual_audio
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    caption: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    image_cached_url: Mapped[str | None] = mapped_column(Text)
    button_data: Mapped[dict | None] = mapped_column(JSONB)  # structured button layout
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)  # legacy single-broadcast slot
    # Real season identity for split-season cards; NULL for extras and non-entry cards.
    season_part: Mapped[int | None] = mapped_column(Integer)
    # AniList id of the entry this card shows (season/movie cards). Lets a
    # ban-restore rebuild the watch guide's {BOT_QUAL#id:…} quality deep-links —
    # each restored season card is remapped to its fresh message id by id. NULL
    # for the info card / watch guide / footer (no single entry).
    anilist_id: Mapped[int | None] = mapped_column(BigInteger, index=True)


class BotDelivery(Base, PKMixin, TimestampMixin):
    """Where a distribution bot delivered its posts to a specific user.

    Persisted across bot restarts so the distribution bot can find a returning
    user's previously-delivered messages and decide whether to keep, refresh, or
    replace them. ``delivered_revision`` is the bot's ``content_revision`` at the
    time of last delivery; on /start the bot deletes the old messages and
    re-delivers only if the bot's current revision is higher.

    Up to one row per ``(bot_id, user_id)`` (enforced by the unique constraint).
    """

    __tablename__ = "bot_deliveries"

    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The message IDs of the sent posts in delivery order. Used to delete them
    # when a re-delivery is needed (a fresh /start hits content_revision > this).
    message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, nullable=False)
    pinned_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # The bot's content_revision this delivery was last produced at.
    delivered_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", name="uq_bot_delivery_user"),
    )


class AuditLog(Base, PKMixin):
    __tablename__ = "audit_logs"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("ts", "actor_id", "action", "target", name="uq_audit_dedupe"),
    )
