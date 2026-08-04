"""Bot content generation — watch guide, season cards, info cards, footer.

After a distribution bot is created, we generate a set of pre-formatted posts that
mirror the reference channel layouts. These are stored in BotContentPost and
delivered in order when a user starts the bot.

Data sources:
  * AniList — titles, relations, episode/season counts, synopsis, score, genres
  * TMDB   — poster images, backdrops
  * Storage packs — actual resolutions/audio available

All text templates live in en.json and are configurable via the Settings panel.

Architecture (v2 — franchise-aware):
  1. Gather franchise-level metadata (acutebot → AniList → TMDB)
  2. Walk the FULL franchise graph via ``AnilistClient.walk_franchise_full``
  3. Split into TV entries (sorted chronologically) and extras (OVA/ONA/MOVIE/SPECIAL)
  4. Map TV entries to storage packs by season index; extra entries get their own
     metadata from AniList
  5. Build cards: info → season cards → extra cards → unified watch guide → footer
"""

from __future__ import annotations

from sqlalchemy import select

import calendar
import re

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    DistributionBot,
    StoragePack,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.localization.messages import M, t
from nekofetch.providers.catbox import upload_from_url as catbox_upload_from_url
from nekofetch.providers.filestore import build_fstore_link, pick_fstore_bot_rr
from nekofetch.services.bot_render import format_duration, resolve_premium_emoji
from nekofetch.ui import templates

log = get_logger(__name__)


async def _jikan_search(title: str) -> dict | None:
    """Search MyAnimeList via Jikan (jikan.moe) and return the top anime record.

    Jikan is public/unauthenticated but rate-limited (~3 req/s) and sits behind
    Cloudflare, which serves Python's stock TLS fingerprint a synthetic 504. We
    mirror :meth:`MetadataPrefetchService._prefetch_jikan`: curl_cffi with Chrome
    impersonation first (falling back to plain httpx), one search call, take the
    first hit. Returns the raw MAL ``anime`` payload (``mal_id``, ``title``,
    ``title_english``, ``synopsis``, ``score``, ``genres``, ``episodes``,
    ``aired``, ``duration``, ``images.jpg.large_image_url``) or ``None``.
    """
    import asyncio

    url = "https://api.jikan.moe/v4/anime"
    params = {"q": title, "limit": 1}

    async def _fetch() -> dict | None:
        try:
            from curl_cffi import requests as cf_requests
        except ImportError:  # noqa: BLE001
            cf_requests = None
        if cf_requests is not None:
            sess = cf_requests.AsyncSession(
                impersonate="chrome", timeout=30.0, allow_redirects=True)
            try:
                for attempt in range(3):
                    r = await sess.get(url, params=params)
                    if r.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if r.status_code >= 400:
                        log.warning("bot.content.jikan.http_error",
                                    transport="curl_cffi", status=r.status_code)
                        return None
                    return r.json()
                log.warning("bot.content.jikan.gave_up",
                            transport="curl_cffi", status=getattr(r, "status_code", None))
                return None
            finally:
                try:
                    await sess.close()
                except Exception:  # noqa: BLE001
                    pass
        log.warning("bot.content.jikan.no_curl_cffi",
                    hint="curl_cffi not installed — httpx may 504 behind Cloudflare")
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cli:
            for attempt in range(3):
                r = await cli.get(url, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()

    try:
        body = await _fetch()
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("bot.content.jikan.failed", title=title, error=str(exc))
        return None
    data = (body or {}).get("data") or []
    if not data:
        log.debug("bot.content.jikan.empty", title=title)
        return None
    return data[0]

_RES_ORDER = {"360p": 360, "480p": 480, "540p": 540, "720p": 720, "1080p": 1080}
_BTN_QUALITIES = ("480p", "720p", "1080p")
_TV_FORMATS = {"TV", "TV_SHORT", "TV_SPECIAL"}

# AniList synopses (fetched asHtml:false) still embed literal HTML — <br>, <i>,
# <b>, and a trailing "<source>" fragment. Strip ALL tags before truncating so a
# mid-tag slice can't leave a dangling "<" that Pyrogram re-escapes to "&lt;".
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _clean_synopsis(text: str | None, *, limit: int = 350) -> str:
    """Flatten a synopsis for a card: strip HTML, collapse whitespace, trim.

    AniList ships the description with literal ``<br>``/``<i>`` tags; a naive
    ``[:N]`` slice can cut inside a tag and leave a stray ``<`` (renders as the
    literal ``&lt;``). We remove every tag first, collapse runs of whitespace to
    single spaces, then trim to ``limit`` on a WORD boundary and add an ellipsis.
    Keeps an inline ``(Source: …)`` credit when it fits.
    """
    if not text or text == "—":
        return "—"
    # Turn explicit breaks into spaces first, then drop any remaining tags.
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    text = _HTML_TAG_RE.sub("", text)
    text = " ".join(text.split())
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    # Trim on a word boundary so we never end mid-word or mid-entity.
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-—")
    return f"{clipped}…"


class BotContentService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def generate_posts(self, bot_id: int, anime_doc_id: str) -> list[BotContentPost]:
        """Generate ALL content posts for a distribution bot/entity and persist them.

        Produces, in order:
          1. Info/overview card  — poster + full franchise-level metadata
          2. Season cards        — one per TV season, sorted chronologically
          3. Extra cards         — OVA / ONA / Movie / Special (one per entry)
          4. Watch guide (pinned) — unified release-order listing
          5. Footer              — cross-promotion branding card

        Image priority per card (new): user-generated thumbnail →
          franchise banner → franchise poster → cover. Old behaviour was the
        inverse (AniList poster first), which produced visually bland cards
        that ignored the admin's asset-picking work in the thumbnail channel.
        Now the per-entry workflow output overrides whenever available.
        """
        # Remove any previously generated posts for this bot.
        async with session_scope(self._c.pg_sessionmaker) as session:
            old = await session.execute(
                select(BotContentPost).where(BotContentPost.bot_id == bot_id)
            )
            for p in old.scalars().all():
                await session.delete(p)

        # Gather the data we need.
        packs = await self._load_packs(anime_doc_id)
        meta = await self._gather_metadata(anime_doc_id)

        # Walk the full franchise graph (AniList BFS).
        franchise = await self._walk_franchise(anime_doc_id, meta)

        # Pull user-generated thumbnails from the thumbnail channel. Empty dict
        # when the admin skipped / before generation completed — callers fall
        # back to AniList poster via _pick_card_image.
        generated_thumbs = await self._lookup_generated_thumbnails(anime_doc_id)

        posts: list[BotContentPost] = []
        order = 0

        # ── 1. Info/overview card ──
        info_caption, info_default_image = await self._build_info_card(meta)
        if info_caption:
            # Info card image: prefer the FIRST franchise entry's generated
            # thumbnail (matches the "main channel uses first season" rule),
            # else fall back to the franchise banner / poster.
            info_image = self._pick_card_image(
                generated_thumbs.get(
                    (franchise.get("tv", [None, ])[0] or {}).get("anilist_id")
                    if franchise.get("tv") else None
                ),
                info_default_image, meta,
            )
            cached_info_url: str | None = None
            if info_image and self._c.config.features.catbox_image_cache:
                cached_info_url = await self._upload_card_image(
                    str(info_image), "info_card", bot_id=bot_id, order=order,
                )
            posts.append(BotContentPost(
                bot_id=bot_id, post_type="info_card", order=order,
                caption=info_caption,
                image_url=str(info_image) if info_image else None,
                image_cached_url=cached_info_url,
                is_pinned=self._c.config.post_format.pin_info_card,
            ))
            order += 1

        # ── 2. Season cards — TV entries mapped to storage packs by index ──
        tv_entries = franchise.get("tv", [])
        for i, entry in enumerate(tv_entries, start=1):
            season_packs = [p for p in packs if p.season == i]
            entry_meta = self._entry_meta(meta, entry)
            # Override the entry's poster with the user-generated thumbnail.
            generated = generated_thumbs.get(entry.anilist_id)
            if generated:
                entry_meta["poster_url"] = generated
            caption, image = self._build_season_card(entry_meta, i, season_packs)
            buttons = await self._build_season_buttons(season_packs)
            image_str = str(image) if image else None
            cached_url: str | None = None
            if image_str and self._c.config.features.catbox_image_cache:
                cached_url = await self._upload_card_image(
                    image_str, "season_card", bot_id=bot_id, order=order, season=i,
                )
            posts.append(BotContentPost(
                bot_id=bot_id, post_type="season_card", season=i,
                order=order, caption=caption,
                image_url=image_str,
                image_cached_url=cached_url,
                button_data=buttons,
                # Carry the entry id so a ban-restore can remap this card's
                # {BOT_QUAL#id:…} deep-links to its fresh message id.
                anilist_id=entry.anilist_id,
            ))
            order += 1

        # ── 3. Extra cards — OVA / ONA / Movie / Special ──
        for entry in franchise.get("extras", []):
            # Match packs by entry_id (AniList ID) when available, falling back
            # to season=None for legacy entries that predate entry_id tracking.
            extra_packs = [
                p for p in packs
                if (p.entry_id is not None and p.entry_id == entry.anilist_id)
                or (p.entry_id is None and p.season is None)
            ]
            entry_meta = self._entry_meta(meta, entry)
            # Override the entry's poster with the user-generated thumbnail.
            generated = generated_thumbs.get(entry.anilist_id)
            if generated:
                entry_meta["poster_url"] = generated
            is_movie = entry.format == "MOVIE" or (
                entry.format in ("OVA", "ONA", "SPECIAL")
                and (entry.episodes or 0) <= 1
            )
            if extra_packs:
                caption, image = self._build_season_card(entry_meta, 1, extra_packs)
            else:
                caption, image = self._build_season_card(entry_meta, 1, [])
            buttons = await self._build_season_buttons(extra_packs) if extra_packs else None
            image_str = str(image) if image else None
            cached_url_extra: str | None = None
            if image_str and self._c.config.features.catbox_image_cache:
                cached_url_extra = await self._upload_card_image(
                    image_str, "movie_card" if is_movie else "season_card",
                    bot_id=bot_id, order=order,
                )
            post_type = "movie_card" if is_movie else "season_card"
            posts.append(BotContentPost(
                bot_id=bot_id, post_type=post_type, order=order,
                caption=caption,
                image_url=image_str,
                image_cached_url=cached_url_extra,
                button_data=buttons,
                # Carry the entry id so a ban-restore can remap this card's
                # {BOT_QUAL#id:…} deep-links to its fresh message id.
                anilist_id=entry.anilist_id,
            ))
            order += 1

        # ── 4. Watch guide (pinned) — unified release order ──
        guide = self._build_franchise_watch_guide(meta, packs, franchise)
        if guide:
            posts.append(BotContentPost(
                bot_id=bot_id, post_type="watch_guide", order=order,
                caption=guide,
                is_pinned=self._c.config.post_format.pin_watch_guide,
            ))
            order += 1

        # ── 5. Footer ──
        # Priority: post_format template → BotConfig.footer_text → en.json.
        fmt = self._c.config.post_format
        footer_text = self._render(
            fmt.footer_template or self._c.config.bot.footer_text or "",
            M.BOT_FOOTER,
        )
        footer_image = (
            fmt.footer_image_url or self._c.config.bot.footer_image_url or None
        )
        footer_image_str = str(footer_image) if footer_image else None
        cached_footer_url: str | None = None
        if footer_image_str and self._c.config.features.catbox_image_cache:
            cached_footer_url = await self._upload_card_image(
                footer_image_str, "footer", bot_id=bot_id, order=order,
            )
        posts.append(BotContentPost(
            bot_id=bot_id, post_type="footer", order=order,
            caption=footer_text,
            image_url=footer_image_str,
            image_cached_url=cached_footer_url,
        ))

        # Persist all posts and bump the bot's content_revision in the same
        # transaction so /start can never observe the new posts at the old
        # revision (and vice-versa).
        new_revision = 0
        is_channel = False
        bot_chat_id: int | None = None
        async with session_scope(self._c.pg_sessionmaker) as session:
            for p in posts:
                session.add(p)
            bot_row = await session.get(DistributionBot, bot_id)
            if bot_row is not None:
                bot_row.content_revision = (bot_row.content_revision or 0) + 1
                new_revision = bot_row.content_revision
                is_channel = bool(bot_row.is_channel)
                bot_chat_id = bot_row.chat_id
            await session.flush()
            for p in posts:
                session.expunge(p)

        # ── Channel-direct broadcast ── Channels don't run a Pyrogram client
        # of their own (their storage is the channel itself, not a BotDelivery
        # table); when this row is a channel, post every card directly into the
        # channel chat_id so it's user-visible on Telegram without waiting for
        # a /start click. Bots skip this branch — they serve from the
        # BotContentPost table on /start.
        if is_channel and bot_chat_id:
            try:
                await self._broadcast_to_channel(bot_chat_id, posts)
            except Exception as exc:  # noqa: BLE001
                log.warning("bot.content.channel_broadcast.failed",
                            chat_id=bot_chat_id, error=str(exc))

        log.info("bot.content.generated",
                 bot_id=bot_id, posts=len(posts),
                 tv=len(tv_entries), extras=len(franchise.get("extras", [])),
                 revision=new_revision, is_channel=is_channel,
                 generated_thumbs=len(generated_thumbs))
        return posts

    # ── per-card image priority ─────────────────────────────────────────────────
    @staticmethod
    def _pick_card_image(generated_url: str | None,
                         default_url: str | None,
                         meta: dict) -> str | None:
        """Image priority: user-generated thumbnail > franchise banner > default.

        ``generated_url`` is the admin's picked logo/poster/bg render from
        :class:`ThumbnailChannelService`. ``default_url`` is whatever the
        ``_build_*_card`` method computed (banner or poster). ``meta`` walks
        the franchise for fallbacks if BOTH URLs are empty.
        """
        if generated_url:
            return generated_url
        if default_url:
            return default_url
        # Last-ditch fallback to AniList poster (banner/poster/cover).
        return meta.get("banner_url") or meta.get("poster_url")

    async def _lookup_generated_thumbnails(
        self, anime_doc_id: str,
    ) -> dict[int, str]:
        """Per-anime_id → generated-thumbnail-URL map.

        Proxies to :class:`ThumbnailOrchestratorService.get_generated_thumbnails`
        so this service doesn't depend on the channel client's redis directly.
        Empty dict means "admin hasn't generated anything yet" or "admin
        clicked Skip Custom Thumbnails" — either way, the per-entry cards
        fall back to AniList posters via :meth:`_pick_card_image`.
        """
        try:
            from nekofetch.services.thumbnail_orchestrator_service import (
                ThumbnailOrchestratorService,
            )
            return await ThumbnailOrchestratorService(self._c).get_generated_thumbnails(
                anime_doc_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("bot.content.generated_lookup.failed",
                      anime=anime_doc_id, error=str(exc))
            return {}

    async def _broadcast_to_channel(
        self, chat_id: int, posts: list[BotContentPost],
    ) -> None:
        """Post every BotContentPost directly into the channel's chat_id.

        Used when the entity is a public channel (not a bot). Mirrors what
        :class:`DistributionService.app._send_posts` does for a bot's /start
        delivery, but inlined so a channel doesn't need a Pyrogram client.

        Best-effort: a single failed post is logged and skipped, never aborts
        the loop \u2014 a partial channel still reaches users.

        Channels cannot host callback handlers, so persistent buttons are
        always omitted (``reply_markup=None``) regardless of whether
        ``post.button_data`` exists. The bot half delivers them via URL
        buttons on /start.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            log.warning("bot.content.broadcast.no_client", chat_id=chat_id)
            return
        from pyrogram.enums import ParseMode
        for p in posts:
            try:
                image = p.image_cached_url or p.image_url
                caption = p.caption or ""
                if image:
                    await client.send_photo(
                        chat_id, image, caption=caption,
                        reply_markup=None, parse_mode=ParseMode.HTML,
                    )
                else:
                    await client.send_message(
                        chat_id, caption, reply_markup=None,
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("bot.content.broadcast.post_failed",
                            post_type=p.post_type, error=str(exc))

    @staticmethod
    def build_thumbnail_entries(franchise: dict) -> list[dict]:
        """Build the per-entry dicts the thumbnail channel + publishing pipeline
        both consume.

        Centralises the ``label`` / ``format`` / ``episodes`` / ``summary`` /
        ``anilist_id`` shape so :meth:`BotContentService._queue_for_thumbnails`
        and :class:`PublishingService`._wait_for_thumbnails` stay in lock-step.
        If the two sites drifted in label conventions, an admin would have to
        re-pick assets when the second pipeline rebuilt the request.
        """
        from collections import defaultdict

        tv_entries = franchise.get("tv", []) or []
        extra_entries = franchise.get("extras", []) or []
        out: list[dict] = []

        for i, entry in enumerate(tv_entries, start=1):
            label = f"Season {i:02d}"
            if getattr(entry, "season_part", None):
                label += f" Part {entry.season_part}"
            out.append({
                "label": label,
                "format": "tv",
                "episodes": getattr(entry, "episodes", None),
                "summary": (getattr(entry, "synopsis", "") or "").strip(),
                "anilist_id": entry.anilist_id,
            })

        extra_counts: dict[str, int] = defaultdict(int)
        pretty_name = {"MOVIE": "Movie", "OVA": "OVA",
                       "ONA": "ONA", "SPECIAL": "Special"}
        for entry in extra_entries:
            fmt = (entry.format or "special").upper()
            extra_counts[fmt] += 1
            english_title = (getattr(entry, "english_title", "") or "").strip()
            base_label = f"{pretty_name.get(fmt, fmt.title())} {extra_counts[fmt]}"
            label = f"{base_label}: {english_title}" if english_title else base_label
            out.append({
                "label": label,
                "format": fmt.lower(),
                "episodes": getattr(entry, "episodes", None),
                "summary": (getattr(entry, "synopsis", "") or "").strip(),
                "anilist_id": entry.anilist_id,
            })

        return out


    # ── data loaders ──────────────────────────────────────────────────────────────

    async def _load_packs(self, anime_doc_id: str) -> list[StoragePack]:
        async with session_scope(self._c.pg_sessionmaker) as session:
            rows = (await session.execute(
                select(StoragePack).where(
                    StoragePack.anime_doc_id == anime_doc_id,
                    StoragePack.enabled.is_(True),
                )
            )).scalars().all()
            return list(rows)

    async def _load_bot(self, bot_id: int) -> DistributionBot | None:
        async with session_scope(self._c.pg_sessionmaker) as session:
            return await session.get(DistributionBot, bot_id)

    async def _gather_metadata(self, anime_doc_id: str,
                               title_hint: str | None = None) -> dict:
        """Collect metadata for the title, primarily via @acutebot with
        AniList/TMDB as fallback. Returns a flat dict.

        ``title_hint`` is a resolved series title the caller already knows (e.g.
        the franchise English title). It's used as the search query in place of
        ``anime_doc_id`` whenever the doc id isn't a usable title — most
        importantly when it's a request code like ``REQ-1061`` (which must NEVER
        be sent to AcuteBot: it returns nothing and the info card comes out
        blank)."""
        from nekofetch.providers.acute_bot import fetch_from_acutebot

        # Strip anilist: prefix so we search by title, not a numeric ID.
        from nekofetch.core.parsing import clean_anilist_id
        search_query = clean_anilist_id(anime_doc_id)

        # A request code (REQ-1061) or a bare numeric id is not a searchable
        # title — prefer the caller's resolved title when the doc id is one of
        # those. This is the fix for "info card sends REQ-#### to the metadata
        # API instead of the series name".
        import re as _re
        _looks_like_code = bool(
            not search_query
            or _re.fullmatch(r"REQ-?\d+", search_query, _re.IGNORECASE)
            or search_query.isdigit()
        )
        if title_hint and (_looks_like_code or not search_query.strip()):
            search_query = title_hint.strip()

        meta: dict = {
            "title": search_query,
            "romaji": None,
            "english": None,
            "format": None,
            "status": None,
            "score": None,
            "genres": [],
            "synopsis": None,
            "episode_count": None,
            "season_count": None,
            "first_aired": None,
            "last_aired": None,
            "runtime": None,
            "poster_url": None,
            "banner_url": None,
            "_source": None,
        }

        # ── Prefetch cache check (decides whether to probe AcuteBot at all) ──
        # If AniList data for this title was already fetched + saved at
        # acceptance, use it and SKIP the AcuteBot userbot probe entirely — that
        # probe is a live Telegram round-trip that fired on every publish/update
        # even though the answer was already on disk. Read the blob once here so
        # both the skip decision and the fallback parse below reuse it.
        cached_blob = None
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            cached_blob = await load_cached(self._c, anime_doc_id, "anilist",
                                            anime_doc_id=anime_doc_id)
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            log.debug("bot.content.cache.read_failed", error=str(exc))

        # ── Primary: @acutebot via the userbot pool ──
        # AcuteBot is ALWAYS the primary source for the info card — it returns a
        # richer card (backdrop + curated fields) than the raw API. Only skip it
        # when the query still looks like a request code (REQ-#### or bare id)
        # with no usable title, since AcuteBot returns nothing for those and
        # would blank the card. A populated prefetch cache is NOT a reason to
        # skip: we want AcuteBot's answer when available and only fall back to
        # the cached AniList/TMDB blob if the live probe fails.
        _still_code = bool(
            not search_query.strip()
            or _re.fullmatch(r"REQ-?\d+", search_query, _re.IGNORECASE)
        )
        if not _still_code:
            try:
                from nekofetch.sources.telegram.userbot import UserbotPool

                # Cache the pool on the container so we reuse the same Pyrogram
                # Client connections across calls instead of leaking new ones.
                pool: UserbotPool | None = getattr(self._c, "_userbot_pool", None)  # type: ignore[attr-defined]
                if pool is None:
                    pool = UserbotPool.from_env(
                        self._c.env.telegram_api_id,
                        self._c.env.telegram_api_hash,
                        str(self._c.env.session_path),
                    )
                    self._c._userbot_pool = pool  # type: ignore[attr-defined]
                # Persistent directory where AcuteBot photos are saved.
                photo_dir = str(self._c.env.storage_path / "acutebot_cards")
                acute = await fetch_from_acutebot(search_query, pool, photo_dir=photo_dir)
                if acute is not None:
                    meta.update(acute)
                    meta["_source"] = "acutebot"
                    log.info("bot.content.metadata.acutebot", anime=search_query, photo=acute.get("poster_url"))
                    return meta
            except Exception as exc:
                log.debug("bot.content.acutebot.failed", anime=search_query, error=str(exc))
        else:
            log.warning("bot.content.metadata.code_query_skipped_acute",
                        doc_id=anime_doc_id, query=search_query)

        # ── Fallback 1: AniList (or MAL when AniList is down) ──
        # Reuse the prefetch blob already read above (no second disk read). It
        # was written at acceptance, so this avoids a live AniList call (and the
        # shared-key rate limit) on every publish/update. Falls through to a live
        # search only when the cache is absent.
        cached_search = (cached_blob or {}).get("search")

        if cached_search:
            try:
                s = cached_search
                titles = s.get("titles") or []
                meta["title"] = (s.get("english") or s.get("romaji")
                                 or (titles[0] if titles else search_query))
                meta["romaji"] = s.get("romaji")
                meta["english"] = s.get("english")
                meta["format"] = s.get("format")
                meta["status"] = s.get("status")
                meta["score"] = str(s["score"]) if s.get("score") else None
                meta["genres"] = s.get("genres") or []
                meta["synopsis"] = s.get("synopsis")
                meta["episode_count"] = s.get("episodes")
                meta["season_count"] = s.get("franchise_seasons") or 1
                sd = s.get("start_date") or {}
                if sd.get("year") and sd.get("month") and sd.get("day"):
                    meta["first_aired"] = (
                        f"{calendar.month_abbr[sd['month']]} {sd['day']}, {sd['year']}"
                    )
                if s.get("duration"):
                    meta["runtime"] = f"{s['duration']} min/ep"
                if s.get("cover_url"):
                    meta["poster_url"] = s["cover_url"]
                meta["_source"] = "anilist_cache"
                log.info("bot.content.metadata.cache_hit", anime=search_query)
            except Exception as exc:  # noqa: BLE001 — bad cache → live fetch below
                log.debug("bot.content.cache.parse_failed", error=str(exc))
                cached_search = None

        if not cached_search:
            try:
                search = await self._c.anilist.search(search_query)
                if search is not None:
                    meta["title"] = search.english or search.romaji or search.titles[0] if search.titles else search_query
                    meta["romaji"] = search.romaji
                    meta["english"] = search.english
                    meta["format"] = search.format
                    meta["status"] = search.status
                    meta["score"] = str(search.score) if search.score else None
                    meta["genres"] = search.genres or []
                    meta["synopsis"] = search.synopsis
                    # Entry-level episode count (matches what acutebot shows on its card).
                    meta["episode_count"] = search.episodes
                    meta["season_count"] = search.franchise_seasons or 1
                    # Format first_aired from start_date dict (e.g. "Apr 7, 2013").
                    if search.start_date:
                        yr = search.start_date.get("year")
                        mo = search.start_date.get("month")
                        dy = search.start_date.get("day")
                        if yr and mo and dy:
                            meta["first_aired"] = f"{calendar.month_abbr[mo]} {dy}, {yr}"
                    # Duration per episode (acutebot shows "24 min/ep").
                    if search.duration:
                        meta["runtime"] = f"{search.duration} min/ep"
                    # Poster from the metadata provider's cover image.
                    if search.cover_url:
                        meta["poster_url"] = search.cover_url
                    meta["_source"] = "anilist"
                    log.info("bot.content.metadata.fallback",
                             anime=search_query, source=meta["_source"])
            except Exception as exc:
                log.warning("bot.content.anilist.failed", anime=search_query, error=str(exc))

        # ── Fallback 1.5: MyAnimeList (Jikan) for the fields AniList didn't fill ──
        # MAL is the tier BETWEEN AniList and TMDB: when AcuteBot is down and
        # AniList is thin (or unreachable), reconstruct the card from the
        # prefetched Jikan blob first, then a live Jikan search. Fills ONLY what
        # is still empty — never overwrites a value AniList already supplied.
        # TMDB remains the last resort.
        def _needs_mal() -> bool:
            return not any(
                meta.get(k) for k in
                ("synopsis", "score", "genres", "poster_url", "title")
            ) or not meta.get("synopsis")

        if _needs_mal():
            mal_top = None
            try:
                from nekofetch.services.metadata_prefetch import load_cached_jikan

                mal_top = await load_cached_jikan(
                    self._c, anime_doc_id, anime_doc_id=anime_doc_id)
            except Exception as exc:  # noqa: BLE001 — cache miss → live search
                log.debug("bot.content.jikan.cache_failed", error=str(exc))
            if mal_top is None:
                mal_top = await _jikan_search(search_query or meta.get("title", ""))

            if mal_top:
                img = (mal_top.get("images") or {}).get("jpg") or {}
                aired = mal_top.get("aired") or {}
                supplied_any = False
                if not meta.get("title"):
                    meta["title"] = (mal_top.get("title_english")
                                     or mal_top.get("title") or meta.get("title"))
                    supplied_any = True
                if not meta.get("english"):
                    meta["english"] = mal_top.get("title_english")
                    supplied_any = True
                if not meta.get("romaji") and mal_top.get("title"):
                    meta["romaji"] = mal_top.get("title")
                    supplied_any = True
                if not meta.get("synopsis"):
                    meta["synopsis"] = mal_top.get("synopsis")
                    supplied_any = True
                if not meta.get("score"):
                    meta["score"] = str(mal_top["score"]) if mal_top.get("score") else None
                    supplied_any = True
                if not meta.get("genres"):
                    meta["genres"] = [
                        g["name"] for g in (mal_top.get("genres") or [])
                        if isinstance(g, dict) and g.get("name")
                    ]
                    supplied_any = True
                if not meta.get("episode_count") and mal_top.get("episodes"):
                    meta["episode_count"] = mal_top["episodes"]
                    supplied_any = True
                if not meta.get("poster_url") and img.get("large_image_url"):
                    meta["poster_url"] = img["large_image_url"]
                    supplied_any = True
                if not meta.get("first_aired") and aired.get("string"):
                    meta["first_aired"] = aired["string"]
                    supplied_any = True
                if not meta.get("runtime") and mal_top.get("duration"):
                    # "24 min per ep" → "24 min/ep" to match the other tiers.
                    meta["runtime"] = re.sub(
                        r"\s*per\s*ep\s*$", "/ep", str(mal_top["duration"]).strip(),
                    )
                    supplied_any = True
                if supplied_any:
                    meta["_source"] = "myanimelist"
                    log.info("bot.content.metadata.mal_fallback",
                             anime=search_query, score=meta.get("score"))

        # ── Fallback 2: TMDB for poster + backdrop ──
        # Prefer the prefetched TMDB blob (poster/backdrop/overview cached at
        # acceptance) before any live TMDB call. Only search live on a miss.
        if not meta.get("poster_url") or not meta.get("banner_url"):
            try:
                from nekofetch.services.metadata_prefetch import load_cached, resolve_cached_cover

                if not meta.get("poster_url"):
                    cover = await resolve_cached_cover(
                        self._c, anime_doc_id, kind="poster",
                        anime_doc_id=anime_doc_id)
                    if cover:
                        meta["poster_url"] = cover
                tblob = await load_cached(self._c, anime_doc_id, "tmdb",
                                          anime_doc_id=anime_doc_id)
                if tblob:
                    res = tblob.get("result") or {}
                    if not meta.get("banner_url"):
                        bd = res.get("backdrop_url")
                        if not bd:
                            # First ranked backdrop from the cached asset list.
                            bds = tblob.get("backdrops") or []
                            if bds and isinstance(bds[0], dict):
                                bd = bds[0].get("url")
                        if bd:
                            meta["banner_url"] = bd
                    if not meta.get("synopsis") and res.get("overview"):
                        meta["synopsis"] = res["overview"]
            except Exception as exc:  # noqa: BLE001 — cache miss → live below
                log.debug("bot.content.tmdb.cache_failed", error=str(exc))

        if not meta.get("poster_url"):
            try:
                url = await self._c.tmdb.poster_for(meta["title"])
                if url:
                    meta["poster_url"] = url
                result = await self._c.tmdb.search(meta["title"])
                if result is not None:
                    if result.backdrop_url and not meta.get("banner_url"):
                        meta["banner_url"] = result.backdrop_url
                    # Use TMDB overview as synopsis if we don't have one yet
                    if not meta.get("synopsis") and result.overview:
                        meta["synopsis"] = result.overview
            except Exception as exc:
                log.warning("bot.content.tmdb.failed", anime=search_query, error=str(exc))

        return meta

    # ── franchise walking ───────────────────────────────────────────────────────

    @staticmethod
    def _franchise_from_cache(walk: dict) -> dict:
        """Rebuild ``{id: FranchiseEntry}`` from the cached franchise-walk dict.

        The prefetch serialises ``walk_franchise_full``'s output (a dict of
        ``FranchiseEntry``) to plain dicts in ``anilist.json``. This reconstructs
        the dataclasses so ``_walk_franchise`` can treat a cache hit and a live
        walk identically (same ``.format`` / ``.start_date`` / ``.cover_url``
        attribute access). Unknown/extra keys are ignored so a schema drift in
        the cache never crashes the read."""
        from dataclasses import fields as _dc_fields

        from nekofetch.sources.telegram.anilist import FranchiseEntry

        allowed = {f.name for f in _dc_fields(FranchiseEntry)}
        out: dict = {}
        values = walk.values() if isinstance(walk, dict) else (walk or [])
        for raw in values:
            if not isinstance(raw, dict):
                continue
            aid = raw.get("anilist_id")
            if aid is None:
                continue
            try:
                out[int(aid)] = FranchiseEntry(
                    **{k: v for k, v in raw.items() if k in allowed}
                )
            except Exception:  # noqa: BLE001 — skip a malformed entry, keep the rest
                continue
        return out

    async def _walk_franchise(self, anime_doc_id: str, meta: dict) -> dict:
        """Walk the full AniList franchise graph and return sorted entries.

        Returns ``{"tv": [...], "extras": [...], "all": [...]}`` where:
          * ``tv`` — TV/TV_SHORT entries sorted chronologically by start_date
          * ``extras`` — OVA/ONA/MOVIE/SPECIAL entries sorted chronologically
          * ``all`` — combined release-order list

        Falls back gracefully: if AniList can't resolve the title or the walk
        fails, returns empty lists so the caller still produces the info card
        and footer (just no season/extra cards or watch guide).
        """
        empty = {"tv": [], "extras": [], "all": []}
        try:
            entries = None
            # ── Prefer the prefetched franchise walk (anilist.json["franchise"]) ──
            # It was written at acceptance by walk_franchise_full, so re-walking
            # live here just burns the shared AniList key. Reconstruct
            # FranchiseEntry objects from the cached dicts.
            try:
                from nekofetch.services.metadata_prefetch import load_cached

                blob = await load_cached(self._c, anime_doc_id, "anilist",
                                         anime_doc_id=anime_doc_id)
                walk = (blob or {}).get("franchise")
                if walk:
                    entries = self._franchise_from_cache(walk)
                    if entries:
                        log.info("bot.content.franchise.cache_hit",
                                 anime=anime_doc_id, entries=len(entries))
            except Exception as exc:  # noqa: BLE001 — cache miss → live walk
                log.debug("bot.content.franchise.cache_read_failed", error=str(exc))

            # ── Live fallback (only on cache miss) ──
            if not entries:
                search_query = anime_doc_id
                if search_query.startswith("anilist:"):
                    search_query = search_query[len("anilist:"):]
                search = await self._c.anilist.search(search_query)
                if search is None:
                    log.debug("bot.content.franchise.no_match", anime=search_query)
                    return empty
                root_id = search.id
                entries = await self._c.anilist.walk_franchise_full(root_id)
            if not entries:
                log.debug("bot.content.franchise.empty", anime=anime_doc_id)
                return empty

            # Split and sort.
            tv_entries = [e for e in entries.values() if e.format in _TV_FORMATS]
            tv_entries.sort(key=lambda e: (
                (e.start_date or {}).get("year", 9999),
                (e.start_date or {}).get("month", 99),
                (e.start_date or {}).get("day", 99),
            ))
            extra_entries = [
                e for e in entries.values()
                if e.format in ("OVA", "ONA", "MOVIE", "SPECIAL")
            ]
            extra_entries.sort(key=lambda e: (
                (e.start_date or {}).get("year", 9999),
                (e.start_date or {}).get("month", 99),
                (e.start_date or {}).get("day", 99),
            ))
            all_entries = sorted(
                tv_entries + extra_entries,
                key=lambda e: (
                    (e.start_date or {}).get("year", 9999),
                    (e.start_date or {}).get("month", 99),
                    (e.start_date or {}).get("day", 99),
                ),
            )
            log.info("bot.content.franchise.walked", anime=anime_doc_id,
                     tv=len(tv_entries), extras=len(extra_entries))
            return {"tv": tv_entries, "extras": extra_entries, "all": all_entries}
        except Exception as exc:
            log.warning("bot.content.franchise.failed", anime=anime_doc_id, error=str(exc))
            return empty

    def _entry_meta(self, base_meta: dict, entry) -> dict:
        """Merge franchise-level metadata with per-entry AniList data.

        Returns a shallow copy of ``base_meta`` with the entry's title,
        synopsis, banner, and poster overlaid. The caller passes this dict
        to ``_build_season_card`` / ``_build_info_card``.
        """
        merged = dict(base_meta)
        merged["title"] = entry.english_title or base_meta.get("title", "—")
        # Romaji (transliterated original) for the "<b>English</b>〢Romaji" header.
        # FranchiseEntry.titles is English-first [english, romaji, native], so the
        # 2nd entry is the romaji when it exists and differs from the English name.
        titles = list(getattr(entry, "titles", None) or [])
        romaji = titles[1] if len(titles) > 1 else ""
        merged["romaji"] = romaji or ""
        if entry.synopsis:
            merged["synopsis"] = entry.synopsis
        if entry.banner_url:
            merged["banner_url"] = entry.banner_url
        if entry.cover_url:
            merged["poster_url"] = entry.cover_url
        # Per-episode runtime + episode count drive the movie-vs-season card
        # choice and the DURATION field (a single-episode entry shows runtime,
        # a multi-episode one shows episode count).
        if getattr(entry, "duration", None):
            merged["duration_min"] = entry.duration
        if getattr(entry, "episodes", None) is not None:
            merged["entry_episodes"] = entry.episodes
        return merged

    # ── content builders ─────────────────────────────────────────────────────────

    @staticmethod
    def _qual_placeholder(qual_str: str, anilist_id: int | None) -> str:
        """Wrap a quality string in a ``{BOT_QUAL…}`` placeholder.

        When ``anilist_id`` is known the placeholder is *anchored* to that entry
        (``{BOT_QUAL#<id>:…}``) so a channel sender can deep-link the qualities to
        that entry's own card message; without an id (mapping-built entry) it
        falls back to the plain ``{BOT_QUAL:…}`` form that resolves to the channel
        root. Both forms are recognised by every resolver.
        """
        if anilist_id is not None:
            return f"{{BOT_QUAL#{anilist_id}:{qual_str}}}"
        return f"{{BOT_QUAL:{qual_str}}}"

    def _build_franchise_watch_guide(
        self, meta: dict, packs: list[StoragePack], franchise: dict,
    ) -> str | None:
        """Build the unified watch guide in pure release order.

        TV seasons use the ``BOT_WATCH_GUIDE_SEASON`` template; extras
        (OVA / ONA / Movie / Special) use ``BOT_WATCH_GUIDE_EXTRA``.
        Extras are numbered by format (e.g. "OVA 1", "OVA 2", "Movie 1").
        """
        all_entries = franchise.get("all", [])
        if not all_entries:
            # Fallback: if franchise walking failed, build from packs alone.
            return self._build_watch_guide_fallback(meta, packs)

        tv_entries = franchise.get("tv", [])
        # Build season_num lookup for TV entries.
        season_map: dict[int, int] = {
            e.anilist_id: i for i, e in enumerate(tv_entries, start=1)
        }
        # Number extras by format.
        extra_counts: dict[str, int] = {}
        extra_labels: dict[int, str] = {}
        for e in all_entries:
            if e.format not in _TV_FORMATS:
                extra_counts[e.format] = extra_counts.get(e.format, 0) + 1
                extra_labels[e.anilist_id] = f"{e.format} {extra_counts[e.format]}"

        all_lines: list[str] = []
        for entry in all_entries:
            is_tv = entry.format in _TV_FORMATS
            if is_tv:
                s_num = season_map.get(entry.anilist_id)
                if s_num is None:
                    continue
                ep = [p for p in packs if p.season == s_num]
                label = f"Season {s_num:02d}"
                if entry.season_part:
                    label += f" Part {entry.season_part}"
                ep_count = max((p.episode_to or p.file_count or 0) for p in ep) if ep else (entry.episodes or 0)
                quals = sorted(
                    {p.resolution for p in ep if p.resolution},
                    key=lambda r: _RES_ORDER.get(r, 9999),
                ) if ep else []
                qual_str = "  ".join(quals) if quals else "480p  720p  1080p"
                # Wrap qualities in a placeholder the sender resolves at serving
                # time. In a CHANNEL we anchor to this entry's own card message
                # (``{BOT_QUAL#<anilist_id>:…}`` → t.me/<handle>/<msg_id>) so tapping
                # a quality jumps to the exact season card; in a private bot chat
                # (no message deep-linking) the anchor is stripped and it collapses
                # to the bot's t.me/<username> root.
                qual_str = self._qual_placeholder(qual_str, entry.anilist_id)
                season_lines = self._render(
                    self._c.config.post_format.watch_guide_season_line,
                    M.BOT_WATCH_GUIDE_SEASON,
                    season_label=label,
                    episodes=ep_count or "—",
                    qualities=qual_str,
                )
            else:
                # Match packs by entry_id (AniList ID) when available, falling
                # back to season=None for legacy entries that predate entry_id.
                ep = [
                    p for p in packs
                    if (p.entry_id is not None and p.entry_id == entry.anilist_id)
                    or (p.entry_id is None and p.season is None)
                ]
                ep_count = entry.episodes or 1
                quals = sorted(
                    {p.resolution for p in ep if p.resolution},
                    key=lambda r: _RES_ORDER.get(r, 9999),
                ) if ep else []
                qual_str = "  ".join(quals) if quals else "480p  720p  1080p"
                qual_str = self._qual_placeholder(qual_str, entry.anilist_id)
                label = extra_labels.get(entry.anilist_id, entry.format)
                season_lines = self._render(
                    self._c.config.post_format.watch_guide_extra_line,
                    M.BOT_WATCH_GUIDE_EXTRA,
                    label=label,
                    episodes=ep_count or "—",
                    qualities=qual_str,
                )
            all_lines.append(season_lines)

        return self._render(
            self._c.config.post_format.watch_guide_template,
            M.BOT_WATCH_GUIDE, seasons="\n\n".join(all_lines),
        )

    def _build_watch_guide_fallback(
        self, meta: dict, packs: list[StoragePack],
    ) -> str | None:
        """Fallback watch guide when franchise walking failed — uses packs only."""
        seasons = sorted({p.season for p in packs if p.season is not None})
        if not seasons:
            return None
        season_lines = []
        for s in seasons:
            season_packs = [p for p in packs if p.season == s]
            ep_max = max((p.episode_to or p.file_count or 0) for p in season_packs)
            quals = sorted(
                {r for r in {p.resolution for p in season_packs} if r},
                key=lambda r: _RES_ORDER.get(r, 9999),
            )
            qual_str = "  ".join(quals) if quals else "480p  720p  1080p"
            qual_str = f"{{BOT_QUAL:{qual_str}}}"
            season_label = self._season_label(s, meta)
            season_lines.append(self._render(
                self._c.config.post_format.watch_guide_season_line,
                M.BOT_WATCH_GUIDE_SEASON,
                season_label=season_label,
                episodes=ep_max or "—",
                qualities=qual_str,
            ))
        return self._render(
            self._c.config.post_format.watch_guide_template,
            M.BOT_WATCH_GUIDE, seasons="\n\n".join(season_lines),
        )

    def _build_watch_guide(self, meta: dict, packs: list[StoragePack]) -> str | None:
        """Legacy compat — delegates to the fallback pack-based builder."""
        return self._build_watch_guide_fallback(meta, packs)

    def _season_label(self, season: int, meta: dict) -> str:
        """Human-readable season label. """
        return f"Season {season:02d}"

    async def _build_info_card(self, meta: dict) -> tuple[str | None, str | None]:
        """Build the overview/info card from available metadata."""
        if not meta.get("title"):
            return None, None

        # Use the TMDB or AniList poster as the card image.
        image = meta.get("banner_url") or meta.get("poster_url")

        caption = self._render(
            self._c.config.post_format.info_card_template, M.BOT_INFO_CARD,
            title=meta.get("title", "—"),
            romaji=meta.get("romaji") or "",
            genres=", ".join(meta.get("genres", []) or []) or "—",
            format=meta.get("format") or "—",
            rating=meta.get("score") or "—",
            status=meta.get("status") or "—",
            # Use AcuteBot's parsed fields directly (release_date never existed here).
            first_aired=meta.get("first_aired") or "—",
            last_aired=meta.get("last_aired") or "—",
            runtime=meta.get("runtime") or "—",
            episodes=str(meta.get("episode_count") or "—"),
            synopsis=_clean_synopsis(meta.get("synopsis"), limit=400),
        )
        return caption, image

    @staticmethod
    def _language_tag(audios: set[AudioType], *, has_english_subs: bool = False,
                      extra_langs: set[str] | None = None) -> str:
        """Unified LANGUAGE field for season cards.

        Size matters here. ``bot_naming.format_bot_name`` renders the same
        words in the bot display name, so a user looking at "Spy x Family
        『 Dual Audio 』 « English & Japanese »" in the channel list and
        the season card's LANGUAGE field below will see the SAME terms
        for the audio type and language list — only the wrapping brackets
        differ (``『…』«…»`` for the bot name, ``[…]`` for the card).

        Words produced:
          * audio type: ``Multi Audio`` / ``Dual Audio`` / ``Sub & Dub`` /
            ``Dub`` / ``Sub`` (delegated to ``bot_naming.audio_tag``)
          * languages: ``English & Japanese`` / ``Japanese`` / ``English``
            (delegated to ``bot_naming.language_label``)

        Multi-audio (3+ languages after folding Japanese/English from audio
        types + any extras) is the only branch that diverges from audio_tag
        because audio_tag only recognises the ``AudioType.MULTI`` enum —
        assembled multi-language (e.g. Sub + Dub + Hindi extras) is detected
        here.
        """
        # 2-letter ISO codes ↔ canonical full names. Applied at the entry
        # point so the same key canonicalised once cannot appear twice in
        # the rendered list — otherwise extras={\"en\",\"ja\",\"hi\"} combined
        # with DUAL_AUDIO inference (which adds "english"/"japanese")
        # would produce \"English, English, Hindi, Japanese & Japanese\".
        _LANG_ALIAS = {
            "en": "english", "ja": "japanese", "hi": "hindi",
            "ko": "korean", "zh": "chinese", "es": "spanish",
        }

        audios_set = set(audios)
        langs: set[str] = {
            _LANG_ALIAS.get(l.strip().lower(), l.strip().lower())
            for l in (extra_langs or set())
            if l and l.strip()
        }
        # DUAL_AUDIO packs carry BOTH tracks in one file, so Japanese and
        # English languages are present in the file even though there are
        # no separate SUBBED/DUBBED packs. Imply them here so the language
        # list isn't empty for the canonical "Dual Audio" case.
        if AudioType.DUAL_AUDIO in audios_set:
            langs.update({"japanese", "english"})
        if AudioType.SUBBED in audios_set:
            langs.add("japanese")
        if AudioType.DUBBED in audios_set:
            langs.add("english")
        multi = len(langs) >= 3

        from nekofetch.services.bot_naming import audio_tag, language_label

        if multi:
            type_word = "Multi Audio"
        else:
            type_word = audio_tag(audios_set)
        # A dub-only pack with English subs available is genuinely "Dub +
        # Subs" — separate audio paths, just like Sub & Dub. Keep this hint
        # surfacing on the card since the bot name doesn't have an equivalent
        # signal.
        if has_english_subs and type_word == "Dub":
            type_word = "Dub + Subs"
        langs_label = language_label(langs) if langs else ""

        # Single-language cases read more naturally without the bracket —
        # mirrors :func:`bot_naming.format_bot_name`’s choice to drop the
        # «…» wrapper when there's only one language.
        if type_word and langs_label:
            return f"{type_word} [{langs_label}]"
        if type_word:
            # No languages resolved (e.g. an unknown audio type that
            # audio_tag mapped to ""). Fall back to a descriptive tag.
            return type_word
        if langs_label:
            return f"[{langs_label}]"
        return "—"

    @staticmethod
    def entry_language_tag(audios: set[AudioType], *, has_english_subs: bool = False) -> str:
        """Entry-card LANGUAGE label — the operator's 5 canonical variants.

        Distinct from the channel-title ``bot_naming.audio_tag`` (which reads
        "Dual Audio, Sub & Dub"): the ENTRY post uses a tighter label the
        operator specified exactly:

          * dual-audio file        → ``Dual Audio [English & Japanese]``
          * separate sub + dub     → ``Sub & Dub [English & Japanese]``
          * sub only               → ``Sub [Japanese + ESubs]``
          * dub only + eng subs    → ``Dub [English + Subs]``
          * dub only               → ``Dub [English]``
        """
        vals = set(audios)
        has_dual = AudioType.DUAL_AUDIO in vals or AudioType.MULTI in vals
        has_sub = AudioType.SUBBED in vals
        has_dub = AudioType.DUBBED in vals

        if has_dual:
            return "Dual Audio [English & Japanese]"
        if has_sub and has_dub:
            return "Sub & Dub [English & Japanese]"
        if has_dub:
            return "Dub [English + Subs]" if has_english_subs else "Dub [English]"
        if has_sub:
            return "Sub [Japanese + ESubs]"
        return "Sub [Japanese + ESubs]"

    def _render(self, override: str, key: str, **kwargs) -> str:
        """Render a card from a config override (if set) else the ``en.json`` key.

        A non-empty ``override`` (set in Settings → Post Format) wins over the
        shipped ``en.json`` template; an empty one falls back to the catalog so
        clearing a field restores the built-in look. Premium ``:name:`` emoji
        tokens are expanded last via the ``post_format`` map.
        """
        if override:
            try:
                text = override.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                # A malformed operator template must never crash a publish —
                # fall back to the shipped catalog string instead.
                text = t(key, **kwargs)
        else:
            text = t(key, **kwargs)
        return resolve_premium_emoji(text, self._c.config.post_format)

    def _build_season_card(self, meta: dict, season: int, packs: list[StoragePack]) -> tuple[str, str | None]:
        """Build a season entry card matching the reference format.

        Single-episode entries (movies, one-shot OVAs/specials) render the
        movie card with a real per-episode **runtime** pulled from AniList;
        multi-episode entries render the season card with an episode count.
        This replaces the old ``1h {episode_count}m`` bug where an episode
        count was fed into the duration slot as if it were minutes.
        """
        fmt = self._c.config.post_format
        ep_max = max((p.episode_to or p.file_count or 0) for p in packs) if packs else 0
        audios = {p.audio for p in packs}
        # Entry-card LANGUAGE label (tighter than the channel-title audio_tag):
        # "Dual Audio [English & Japanese]" / "Sub & Dub [...]" / etc. Empty packs
        # fall back to the sub-only shape.
        lang_str = self.entry_language_tag(audios) if audios else "Sub [Japanese + ESubs]"
        # Collect qualities.
        quals = sorted(
            {p.resolution for p in packs},
            key=lambda r: _RES_ORDER.get(r, 9999),
        )
        qual_str = ", ".join(quals) if quals else "Multi Quality"
        genres = ", ".join(meta.get("genres", []) or []) or "—"
        synopsis = _clean_synopsis(meta.get("synopsis"))
        score = meta.get("score") or "—"
        # Title header: "<b>English</b>〢Romaji" when a distinct romaji exists,
        # else just "<b>English</b>". Only the English name is bold; the romaji
        # tail sits outside the <b>. The template slot is the whole header HTML.
        english = meta.get("title", "—")
        romaji = (meta.get("romaji") or "").strip()
        if romaji and romaji != english:
            title = f"<b>{english}</b>〢{romaji}"
        else:
            title = f"<b>{english}</b>"

        # A single-episode entry (movie / one-shot OVA / special) shows a
        # runtime; a multi-episode entry shows an episode count. Prefer the
        # AniList episode count carried on ``meta`` for extras (packs may be a
        # single bundled file), falling back to the pack-derived ``ep_max``.
        entry_eps = meta.get("entry_episodes")
        effective_eps = entry_eps if entry_eps is not None else ep_max
        is_movie = any(
            p.season is None and (p.episode_from == p.episode_to and (p.episode_to or 0) <= 1)
            for p in packs
        ) or (packs == [] and (effective_eps or 0) <= 1)

        if is_movie:
            caption = self._render(
                fmt.movie_card_template, M.BOT_MOVIE_CARD,
                title=title,
                duration=format_duration(meta.get("duration_min"), fmt),
                language=lang_str,
                synopsis=synopsis,
            )
        else:
            caption = self._render(
                fmt.season_card_template, M.BOT_SEASON_CARD,
                title=title, season=season,
                episodes=ep_max or "—",
                S="S" if (ep_max or 0) != 1 else "",   # EPISODE vs EPISODES
                rating=score,
                language=lang_str,
                genres=genres,
                synopsis=synopsis,
            )

        # Use the same poster for all season cards.
        image = meta.get("poster_url")
        return caption, image

    async def _generate_fstore_links(self, packs: list[StoragePack]) -> dict[str, str]:
        """Pre-generate Fstore links for each quality in the given packs.

        Uses round-robin bot selection to distribute load across configured
        file-store bots. Returns a dict mapping quality (or ``lang_quality``
        for separate audio) to the full ``https://t.me/...`` link.

        Returns an empty dict when no file-store bots or storage channel
        is configured — the distribution bot will fall back gracefully.
        """
        bot_usernames = self._c.config.bot.filestore_bots
        if not bot_usernames or not self._c.config.storage_channel.enabled:
            return {}
        # Defensive: a config value may be a single comma-joined string (a bad
        # runtime override) or carry ``@``/whitespace. Flatten + clean so RR sees
        # real, separate usernames instead of one malformed entry.
        bot_usernames = [
            u.strip().lstrip("@")
            for entry in bot_usernames
            for u in str(entry).split(",")
            if u.strip()
        ]
        if not bot_usernames:
            return {}

        # Rotation mode (Senku Settings → bot.fstore_rotation):
        #   per_entry — pick ONE bot here so every quality of this entry is served
        #               by the SAME bot and the round-robin counter advances exactly
        #               once per entry. Strict per-entry rotation: entry 1 → bot A,
        #               entry 2 → bot B, entry 3 → bot C, entry 4 → bot A … so the
        #               bots end up with an even number of entries each.
        #   per_pack  — pick a fresh bot per pack (inside the loop) so each quality
        #               rotates to the next bot; one entry's links can span several.
        rotation = getattr(self._c.config.bot, "fstore_rotation", "per_entry")
        per_entry = rotation != "per_pack"
        entry_bot = None
        if per_entry:
            entry_bot = await pick_fstore_bot_rr(self._c.redis, bot_usernames)
            if entry_bot is None:
                return {}

        links: dict[str, str] = {}
        for pack in packs:
            # per_entry → the one bot picked above; per_pack → a fresh bot per pack.
            bot = entry_bot if per_entry else await pick_fstore_bot_rr(
                self._c.redis, bot_usernames)
            if bot is None:
                continue
            file_ids = pack.file_message_ids or []
            # A pack is the FULL message range in the database channel:
            #   header/caption → file 1 … file N → end sticker
            # (see StoragePack layout). Tapping a quality button must deliver the
            # whole pack — the caption, every file, and the ending sticker — so the
            # range starts at the header message (fallback: the first file, then
            # start_message_id) and ENDS at end_message_id (the sticker/last). The
            # previous code ended at file_ids[-1], which dropped the caption and
            # the end sticker from what a user actually received.
            start = (
                pack.header_message_id
                or (file_ids[0] if file_ids else None)
                or pack.start_message_id
            )
            # end_message_id is the end sticker / last message. Fall back to the
            # last recorded file, then to start itself, so a legacy pack with a
            # NULL end still delivers something sane rather than a single file.
            end = pack.end_message_id or (file_ids[-1] if file_ids else start)
            link = build_fstore_link(
                bot_username=bot,
                channel_id=pack.channel_id,
                start_msg_id=start,
                end_msg_id=end,
            )
            # Key includes audio type so separate sub/dub packs don't overwrite
            links[f"{pack.resolution}_{pack.audio.value}"] = link
        return links

    async def _build_season_buttons(self, packs: list[StoragePack]) -> dict | None:
        """Build the button layout for a season's quality options.

        For dual-audio packs: single row of quality buttons.
        For separate audio sources: language sections with quality buttons underneath.

        Pre-generates Fstore download links at build time and stores them in
        ``button_data.links`` so the distribution bot can serve them directly
        as URL buttons — no dynamic link generation per user request.
        """
        if not packs:
            return None
        quals = sorted(
            {p.resolution for p in packs},
            key=lambda r: _RES_ORDER.get(r, 9999),
        )

        # Take at most the configured number of reference qualities
        # (default 3: 480p, 720p, 1080p — matching the reference channels).
        cap = max(1, self._c.config.post_format.max_quality_buttons)
        available = [q for q in _BTN_QUALITIES if q in quals][:cap]
        if not available:
            available = quals[:cap]

        audios = {p.audio for p in packs}
        has_dual = AudioType.DUAL_AUDIO in audios
        has_separate = AudioType.SUBBED in audios and AudioType.DUBBED in audios

        # Pre-generate Fstore links for all packs
        raw_links = await self._generate_fstore_links(packs)

        if has_separate and not has_dual:
            # Separate audio: language → quality.
            sections = [
                {
                    "language": "english",
                    "label": t(M.BOT_LANG_ENGLISH),
                    "qualities": available,
                },
                {
                    "language": "japanese",
                    "label": t(M.BOT_LANG_JAPANESE),
                    "qualities": available,
                },
            ]
            # Map links per language+quality key, resolving to correct audio type
            lang_audio = {"english": AudioType.DUBBED, "japanese": AudioType.SUBBED}
            links: dict[str, str] = {}
            for sec in sections:
                lang = sec["language"]
                target_audio = lang_audio.get(lang, AudioType.DUAL_AUDIO)
                for q in sec["qualities"]:
                    key = f"{q}_{target_audio.value}"
                    if key in raw_links:
                        links[f"{lang}_{q}"] = raw_links[key]
            return {
                "type": "separate_audio",
                "sections": sections,
                "links": links,
            }

        # Dual-audio or single: flat quality row.
        # For flat layout there's only one pack per resolution, so any
        # audio variant works — pick the first match.
        flat_links: dict[str, str] = {}
        for q in available:
            key = next(
                (k for k in raw_links if k.startswith(f"{q}_")),
                None,
            )
            if key:
                flat_links[q] = raw_links[key]

        return {
            "type": "flat",
            "qualities": available,
            "links": flat_links,
        }

    async def _upload_card_image(
        self, url: str, post_type: str, *, bot_id: int, order: int,
        season: int | None = None,
    ) -> str | None:
        """Push a single card image to **catbox.moe** and return the public URL.

        Called once per image at ``generate_posts`` time; the returned URL is
        written to ``BotContentPost.image_cached_url`` and served directly by
        the distribution bot on every ``/start``. Returns ``None`` on any
        failure (network, parse, catbox rejection) — the caller stores
        ``None`` on the row and the distribution read path falls back to
        the original ``image_url`` so a single broken poster never blocks
        the entire regeneration.

        The ``bot_id`` / ``order`` / ``season`` args are reserved for future
        per-bot quota tracking; today they only appear in log lines.
        """
        if not url:
            return None
        log_ctx = {
            "bot_id": bot_id, "order": order, "post_type": post_type,
            "season": season, "url": url,
        }
        try:
            cached = await catbox_upload_from_url(url)
            if cached:
                log.info("bot.content.image_cached", **log_ctx, catbox_url=cached)
            else:
                log.warning("bot.content.image_cache_failed", **log_ctx)
            return cached
        except Exception as exc:  # noqa: BLE001 - any hiccup falls back to None
            log.warning("bot.content.image_cache_failed", **log_ctx, error=str(exc))
            return None

    async def _queue_for_thumbnails(self, anime_doc_id: str, meta: dict,
                                      franchise: dict) -> None:
        """If the thumbnail channel is enabled, queue this franchise's entries
        for custom thumbnail generation (logo/poster/background selection).

        Admins will be prompted in the thumbnail control center channel to
        select assets before the HTML→image renderer generates the final
        thumbnails. The existing TMDB/AniList poster is used until then.
        """
        if not self._c.config.features.thumbnail_generation:
            return
        if not self._c.config.thumbnail_channel.enabled:
            return
        if not self._c.config.thumbnail_channel.telegraph_access_token:
            return

        # Build the list of entries that need thumbnails. Each entry dict
        # carries the AniList synopsis (``summary``) AND ``anilist_id`` so the
        # thumbnail channel can post a per-entry summary card BEFORE the
        # workflow cards — without that context, admins click into the
        # workflow with zero understanding of what they're picking assets
        # for. The ``anilist_id`` is the join key downstream cards use to
        # look up the generated thumbnail URL when the admin finishes
        # generating (see :meth:`BotContentService._lookup_generated_thumbnails`).
        tv_entries = franchise.get("tv", [])
        extra_entries = franchise.get("extras", [])
        all_entries = franchise.get("all", [])

        if not all_entries and not tv_entries and not extra_entries:
            return  # No entries to generate thumbnails for

        # Build entries list using the shared helper so labels stay consistent
        # with the publish-time thumbnail request (see
        # :func:`BotContentService.build_thumbnail_entries`).
        entries = self.build_thumbnail_entries(franchise)
        if not entries:
            return

        try:
            from nekofetch.services.thumbnail_channel_service import ThumbnailChannelService

            thumb_svc = ThumbnailChannelService(self._c)
            title = meta.get("title") or meta.get("english") or anime_doc_id
            await thumb_svc.add_to_queue(anime_doc_id, str(title), entries)
        except Exception as exc:
            log.warning("bot.content.thumbnail_queue.failed",
                        anime=anime_doc_id, error=str(exc))
