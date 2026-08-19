"""Main channel service.

Posts each published anime to the public main channel: poster + a templated caption
(episodes / quality / language / genre / overview) with two buttons — **Index** (links to
the index-channel letter post) and **Download** (deep-links to the title's distribution
bot). Posts are tracked in ``ChannelPost`` so they can be edited in place.

Facts are assembled from the stored packs (qualities, languages, episode count) and, when
available, the metadata enrichment layer (genres, overview, poster, studio tag).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.parsing import clean_anilist_id
from nekofetch.infrastructure.database.postgres.models import (
    BotContentPost,
    ChannelPost,
    DistributionBot,
    StoragePack,
    ThumbnailSource,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.services.thumbnail_service import webp_to_jpeg_async
from nekofetch.ui import templates

log = get_logger(__name__)

_RES_ORDER = {"360p": 360, "480p": 480, "540p": 540, "720p": 720, "1080p": 1080}


def is_stale_message_error(exc: Exception) -> bool:
    """Return whether Telegram rejected a stored id that should be re-posted.

    Matches both the canonical Pyrogram snake-case names (``MESSAGE_ID_INVALID``)
    and space-normalized forms (``message not found``) — an error that gets
    wrapped or reformatted on its way up must still be recognised as stale.
    """
    error = str(exc).upper()
    tokens = (
        "MESSAGE_ID_INVALID", "MESSAGE_NOT_FOUND",
        "MESSAGE_ID_NOT_FOUND", "MESSAGE_EMPTY",
    )
    if any(token in error for token in tokens):
        return True
    normalized = error.replace("_", " ")
    return any(token.replace("_", " ") in normalized for token in tokens)


def _avg_score_pct(scores: list[float]) -> str:
    """Average AniList scores → a plain 2-digit percent like ``"82%"``.

    AniList entry scores are on a 0-10 scale here (e.g. 8.7). The main-channel
    RATING is the average of EVERY franchise entry's score, shown as a rounded
    whole-number percent (no decimals) per spec. A value already on a 0-100
    scale (defensive) is used as-is."""
    vals = [float(s) for s in scores if s is not None]
    if not vals:
        return "—"
    avg = sum(vals) / len(vals)
    pct = avg if avg > 10 else avg * 10  # 0-10 → 0-100; leave a 0-100 value alone
    return f"{int(round(pct))}%"


def format_episode_summary(entries) -> str:
    """Format franchise episode content for the main-channel caption.

    Seasonal episodes are the base number. OVA/ONA/special entries contribute
    their episode totals as ``extras``; movies are counted as movie entries,
    because a movie is one title rather than an episode count.
    """
    seasonal = extras = movies = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or entry.get("format") or "").lower()
        episodes = int(entry.get("episodes") or entry.get("episode_count") or 0)
        # A multi-episode ONA is a real season (Netflix-style online series),
        # not an extra. Keep the same classifier used by the watch-order and
        # distribution-card builders so Takopi cannot diverge between surfaces.
        if kind in {"season", "tv", "tv_short"} \
                or (kind == "ona" and episodes > 1):
            seasonal += episodes
        elif kind in {"movie", "movies"} or kind == "movie".lower():
            movies += 1
        elif kind in {"special", "tv_special", "ova", "ona", "extra"}:
            extras += episodes

    parts: list[str] = []
    if seasonal:
        parts.append(str(seasonal))
    elif extras:
        parts.append("0")
    elif not movies:
        return "—"
    if extras:
        parts.append(f"+ {extras} {'extra' if extras == 1 else 'extras'}")
    if movies:
        parts.append(f"+ {movies} {'movie' if movies == 1 else 'movies'}")
    return " ".join(parts)


def _collapse(text: str | None) -> str:
    """Flatten a synopsis to one clean paragraph.

    TMDB/AniList overviews arrive with ragged hard line breaks (and AniList
    ships HTML ``<br>`` tags) that render as broken lines inside the caption's
    ``<blockquote>``. Collapse every run of whitespace/newlines to a single
    space so the text flows naturally.
    """
    if not text or text == "—":
        return "—"
    # AniList synopses embed literal HTML breaks; treat them as spaces too.
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    return " ".join(text.split())


def _language_summary(packs) -> str:
    """Union audio languages across every stored entry/pack.

    Delegates to the shared resolver (:mod:`nekofetch.services.audio_langs`),
    which prefers each pack's REAL probed ``audio_langs`` and falls back to the
    ``AudioType`` enum map only when a pack has none stored. Joined in reading
    order (English, Japanese, then the rest) with an Oxford separator: two read
    "English & Japanese"; three or more read "English, Japanese & Hindi" — never
    "English & Japanese & Hindi". Returns ``"—"`` when there's nothing to show.
    """
    from nekofetch.services.audio_langs import language_summary
    return language_summary(packs)


@dataclass(slots=True)
class PublicationFacts:
    anime_doc_id: str
    title: str
    tag: str = "Anime"
    episodes: str = "—"
    qualities: str = "—"
    languages: str = "—"
    genres: str = "—"
    overview: str = "—"
    rating: str = "—"                   # franchise-average AniList score, e.g. "82%"
    poster_url: str | None = None
    backdrop_url: str | None = None   # TMDB English 16:9 backdrop for the post photo
    bot_username: str | None = None
    is_channel: bool = False            # True when distribution target is a channel, not a bot
    # Private, bot-minted invite link to the distribution channel. Preferred over
    # the public t.me/<username> link for the Download button so traffic flows
    # through a link we control (and can revoke/replace on a recreate).
    invite_link: str | None = None
    anime_doc_id_bot: int | None = None  # DistributionBot.id (for lazy link minting)
    _audios: set = field(default_factory=set)
    # Title parts for the "<b>English</b>〢Romaji" caption header. ``title`` stays
    # PLAIN (drives the TMDB search); ``title_html`` is the rendered header the
    # caption template uses, built at the end of gather_facts.
    _english: str = ""
    _romaji: str = ""
    title_html: str = ""


class MainChannelService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self.cfg = container.config.main_channel

    def _active(self, client=None) -> bool:
        client = client or getattr(self._c, "admin_client", None)
        return bool(self.cfg.enabled and self.cfg.channel_id != 0 and client is not None)

    async def gather_facts(self, anime_doc_id: str) -> PublicationFacts:
        async with session_scope(self._c.pg_sessionmaker) as session:
            packs = (
                await session.execute(
                    select(StoragePack).where(StoragePack.anime_doc_id == anime_doc_id)
                )
            ).scalars().all()
            bot = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.anime_doc_id == anime_doc_id,
                        DistributionBot.enabled.is_(True),
                    ).order_by(DistributionBot.id.desc())
                )
            ).scalars().first()

        facts = PublicationFacts(anime_doc_id=anime_doc_id, title=anime_doc_id)
        if packs:
            facts.title = packs[0].anime_title
            resolutions = sorted({p.resolution for p in packs},
                                  key=lambda r: _RES_ORDER.get(r, 9999))
            facts.qualities = ", ".join(resolutions) or "—"
            # This is deliberately a union over all packs, not the first
            # season: one dubbed/dual-audio entry makes the franchise label
            # English & Japanese even when other entries are sub-only.
            facts.languages = _language_summary(packs)
            ep_max = max((p.episode_to or p.file_count or 0) for p in packs)
            facts.episodes = str(ep_max) if ep_max else "—"
        if bot and bot.username:
            facts.bot_username = bot.username
            facts.is_channel = bot.is_channel
        if bot:
            facts.anime_doc_id_bot = bot.id
            facts.invite_link = bot.invite_link
            # A channel target should route through a private invite link we own.
            # Mint one lazily the first time we publish if it's missing, so older
            # channels (created before invite links existed) get one on next post.
            if bot.is_channel and not bot.invite_link and bot.chat_id:
                from nekofetch.services.invite_link_service import InviteLinkService

                minted = await InviteLinkService(self._c).ensure_for_bot(bot.id)
                if minted:
                    facts.invite_link = minted

        # Genres / studio-tag / title from the PREFETCHED AniList search blob.
        # The main channel NEVER hits a source scraper (kaa.lt) at publish — that
        # was the 404 — so enrichment is gone. Genres + studio come from the
        # cached AniList media; the overview is overridden by TMDB below (TMDB's
        # franchise-level synopsis is preferred for the main post per spec).
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            ablob = await load_cached(self._c, anime_doc_id, "anilist",
                                      anime_doc_id=anime_doc_id)
            search = (ablob or {}).get("search") or {}
            if search:
                genres = search.get("genres") or []
                if genres:
                    facts.genres = ", ".join(genres)
                studio = search.get("studio")
                if studio:
                    facts.tag = studio.replace(" ", "")
                if not facts.overview or facts.overview == "—":
                    facts.overview = search.get("synopsis") or facts.overview
                # Capture english/romaji for the title header built at the end
                # (kept out of facts.title so the plain title still drives the
                # TMDB search below — the HTML header would break that lookup).
                english = (search.get("english") or "").strip()
                romaji = (search.get("romaji") or "").strip()
                if english:
                    facts.title = english  # plain, TMDB-search-safe
                facts._english = english or facts.title
                facts._romaji = romaji
        except Exception as exc:  # noqa: BLE001 — cache miss → pack/TMDB facts stand
            log.debug("mainchannel.anilist_cache.failed",
                      anime=anime_doc_id, error=str(exc))

        # ── Franchise-level corrections (per Gojo spec) ──
        #   • EPISODES = Σ episodes of the TV-season continuity chain ONLY
        #     (movies / OVAs / specials / spin-offs excluded). ``franchise_totals``
        #     already computes exactly this via the SEQUEL/PREQUEL walk.
        #   • RATING   = AVERAGE of every franchise entry's AniList score.
        await self._apply_franchise_facts(anime_doc_id, facts)

        # 1. Prefer the FIRST franchise entry's USER-GENERATED thumbnail
        #    (the admin picked logo/poster/bg and rendered it via Playwright
        #    in the thumbnail channel). The main channel post mirrors the
        #    first season per the operators' spec: "the main channel thumbnail,
        #    which is essentially the first season thumbnail, just the info's
        #    changed a bit." Falls back to AniList/TMDB below if missing.
        try:
            from nekofetch.services.thumbnail_orchestrator_service import (
                ThumbnailOrchestratorService,
            )
            orch = ThumbnailOrchestratorService(self._c)
            first_thumb = await orch.get_first_season_thumbnail(anime_doc_id)
            if first_thumb:
                facts.backdrop_url = first_thumb
        except Exception as exc:  # noqa: BLE001
            log.debug("mainchannel.thumbnail_lookup.failed",
                      anime=anime_doc_id, error=str(exc))

        # 1a. Senku wizard renders a DEDICATED main-channel card at publish time
        #     (TMDB franchise synopsis + franchise-average AniList ring) and
        #     persists it as the ThumbnailSource main row (anilist_id=-1) with the
        #     mirrored public URL in fields['hosted_url']. Prefer THAT over the
        #     first-season distribution card — the whole point is the two surfaces
        #     differ. The orchestrator (step 1) is empty on a Senku job, so this is
        #     the effective primary there; the auto pipeline keeps step 1.
        if not facts.backdrop_url:
            try:
                async with session_scope(self._c.pg_sessionmaker) as session:
                    row = (await session.execute(
                        select(ThumbnailSource).where(
                            ThumbnailSource.anime_doc_id == anime_doc_id,
                            ThumbnailSource.anilist_id == -1,
                        )
                    )).scalars().first()
                if row is not None:
                    hosted = (row.fields or {}).get("hosted_url") if row.fields else None
                    url = hosted or row.image_path
                    # A file:// path can't be sent by the downstream post; only use
                    # a real http(s) URL here (the mirror), else fall through.
                    if url and str(url).startswith(("http://", "https://")):
                        facts.backdrop_url = url
                        log.info("mainchannel.thumbnail.from_main_render",
                                 anime=anime_doc_id, url=url)
            except Exception as exc:  # noqa: BLE001
                log.debug("mainchannel.main_render_lookup.failed",
                          anime=anime_doc_id, error=str(exc))

        # 1b. Manual (Senku wizard) publish path never populates the thumbnail-
        #     channel workflow map the orchestrator reads — it stores the admin's
        #     rendered thumbnail as the season/movie card image in BotContentPost.
        #     So when the orchestrator has nothing, use the FIRST season/movie
        #     card's image (order-ascending; the info card at order 0 is skipped)
        #     — that is the exact same generated render the entry card shows, which
        #     is what the main post must mirror instead of the AniList poster.
        if not facts.backdrop_url and facts.anime_doc_id_bot:
            try:
                async with session_scope(self._c.pg_sessionmaker) as session:
                    row = (
                        await session.execute(
                            select(BotContentPost)
                            .where(
                                BotContentPost.bot_id == facts.anime_doc_id_bot,
                                BotContentPost.post_type.in_(
                                    ("season_card", "movie_card")),
                                BotContentPost.image_url.is_not(None),
                            )
                            .order_by(BotContentPost.order.asc())
                        )
                    ).scalars().first()
                if row and row.image_url:
                    facts.backdrop_url = row.image_cached_url or row.image_url
                    log.info("mainchannel.thumbnail.from_content_post",
                             anime=anime_doc_id, url=facts.backdrop_url)
            except Exception as exc:  # noqa: BLE001
                log.debug("mainchannel.content_post_thumb.failed",
                          anime=anime_doc_id, error=str(exc))

        # 2. TMDB metadata for the post photo + overview (best-effort).
        # TMDB descriptions cover the entire franchise, not a single season.
        # Prefer the prefetched tmdb.json (backdrop/overview cached at
        # acceptance) before a live TMDB search. Used as the SECOND fallback
        # when no generated thumbnail is available.
        try:
            from nekofetch.services.metadata_prefetch import (
                load_cached,
                resolve_cached_cover,
            )

            tblob = await load_cached(self._c, anime_doc_id, "tmdb",
                                      anime_doc_id=anime_doc_id)
            if tblob:
                res = tblob.get("result") or {}
                if not facts.backdrop_url:
                    bd = res.get("backdrop_url")
                    if not bd:
                        bds = tblob.get("backdrops") or []
                        if bds and isinstance(bds[0], dict):
                            bd = bds[0].get("url")
                    # Prefer a locally-mirrored / host-backed backdrop when present.
                    cached_bd = await resolve_cached_cover(
                        self._c, anime_doc_id, kind="backdrop",
                        anime_doc_id=anime_doc_id)
                    facts.backdrop_url = cached_bd or bd or facts.backdrop_url
                ov = res.get("overview")
                if ov and ov != "—":
                    facts.overview = ov
        except Exception as exc:  # noqa: BLE001 — cache miss → live below
            log.debug("mainchannel.tmdb_cache.failed",
                      anime=anime_doc_id, error=str(exc))

        if not facts.backdrop_url or not facts.overview or facts.overview == "—":
            try:
                tmdb = getattr(self._c, "tmdb", None)
                if tmdb is not None:
                    result = await tmdb.search(facts.title)
                    if result is not None:
                        if not facts.backdrop_url and result.backdrop_url:
                            facts.backdrop_url = result.backdrop_url
                        # TMDB overview covers the whole franchise — better for main channel
                        if result.overview and result.overview != "—" and (
                            not facts.overview or facts.overview == "—"
                        ):
                            facts.overview = result.overview
            except Exception as exc:  # noqa: BLE001
                log.debug("mainchannel.tmdb.failed", title=facts.title, error=str(exc))

        # Collapse hard line breaks so the overview reads as one clean paragraph
        # (TMDB/AniList synopses arrive with ragged newlines that look broken in
        # the <blockquote>).
        facts.overview = _collapse(facts.overview)

        # Build the "<b>English</b>〢Romaji" caption header now that the plain
        # title has served the TMDB search. Romaji only when it differs.
        english = (facts._english or facts.title or "").strip()
        romaji = (facts._romaji or "").strip()
        if romaji and romaji != english:
            facts.title_html = f"<b>{english}</b>〢{romaji}"
        elif english:
            facts.title_html = f"<b>{english}</b>"
        else:
            facts.title_html = facts.title

        return facts

    async def _apply_franchise_facts(
        self, anime_doc_id: str, facts: PublicationFacts,
    ) -> None:
        """Fill ``facts.episodes`` (TV-season sum) and ``facts.rating`` (franchise
        average AniList score).

        Prefers the prefetched franchise walk (``anilist.json["franchise"]``,
        written at acceptance) so no live AniList call fires on publish; only a
        cache miss falls back to a live ``franchise_totals`` + ``walk_franchise_
        full``. Best-effort: any failure leaves the pack-derived episode count
        and a "—" rating in place rather than aborting the whole post."""
        # ── Cache first: derive both facts from the cached walk ──
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            walk = (blob or {}).get("franchise")
            if walk:
                vals = list(walk.values()) if isinstance(walk, dict) else list(walk)
                # Keep the main seasonal count distinct from extras/movies so a
                # franchise reads "25 + 3 extras + 2 movies" instead of one opaque
                # total that makes movies look like episodes.
                summary = format_episode_summary(vals)
                if summary != "—":
                    facts.episodes = summary
                # Rating = average of every entry's AniList score, as a plain
                # 2-digit percent (scores are 0-10 here → ×10, rounded to int).
                scores = [e.get("score") for e in vals
                          if isinstance(e, dict) and e.get("score") is not None]
                if scores:
                    facts.rating = _avg_score_pct(scores)
                if any(isinstance(e, dict) and (e.get("episodes") or e.get("episode_count")) for e in vals) or scores:
                    return
        except Exception as exc:  # noqa: BLE001 — cache miss → live below
            log.debug("mainchannel.franchise_cache.failed",
                      anime=anime_doc_id, error=str(exc))

        # ── Live fallback (only on cache miss) ──
        anilist = getattr(self._c, "anilist", None)
        if anilist is None:
            return
        from nekofetch.core.parsing import clean_anilist_id

        raw_id = clean_anilist_id(anime_doc_id)
        if not raw_id.isdigit():
            return
        root_id = int(raw_id)

        # Episodes and rating come from the same franchise walk.
        try:
            entries = await anilist.walk_franchise_full(root_id)
            vals = list(entries.values())
            summary = format_episode_summary([
                {
                    "format": getattr(e, "format", ""),
                    "episodes": getattr(e, "episodes", 0),
                }
                for e in vals
            ])
            if summary != "—":
                facts.episodes = summary
            scores = [e.score for e in vals if e.score is not None]
            if scores:
                facts.rating = _avg_score_pct(scores)
        except Exception as exc:  # noqa: BLE001
            log.debug("mainchannel.franchise_walk.failed",
                      anime=anime_doc_id, error=str(exc))

    def _caption(self, f: PublicationFacts) -> str:
        # {title} is the pre-rendered "<b>English</b>〢Romaji" header; fall back
        # to the plain title if the header wasn't built (cache miss).
        return templates.render(
            self.cfg.caption_template,
            title=f.title_html or f.title, tag=f.tag, episodes=f.episodes,
            qualities=f.qualities, languages=f.languages, genres=f.genres,
            overview=f.overview, rating=f.rating,
        )

    async def _buttons(self, f: PublicationFacts) -> InlineKeyboardMarkup | None:
        from nekofetch.services.index_channel_service import IndexChannelService

        row: list[InlineKeyboardButton] = []
        idx_svc = IndexChannelService(self._c)
        # Index button → the INDEX CHANNEL's own private invite link (per spec).
        # Fall back to a deep-link to the exact letter section if minting fails, so
        # the button is never dead.
        index_url = await idx_svc.channel_invite() or await idx_svc.entry_link(f.title)
        if index_url:
            row.append(InlineKeyboardButton(self.cfg.index_button_text, url=index_url))
        # Download target preference (per the operator's explicit ask): a private
        # invite link minted by the channel admin — NOT the public t.me/<username>
        # link — so joins funnel through the bot-controlled link that we can revoke
        # and re-mint on a ban. Falls back to the public username link (channels)
        # or the bot deep-link (bots) when no invite link was minted.
        dl: str | None = None
        if f.is_channel and f.invite_link:
            dl = f.invite_link
        elif f.bot_username:
            if f.is_channel:
                dl = f"https://t.me/{f.bot_username}"
            else:
                dl = f"https://t.me/{f.bot_username}?start=anime_{f.anime_doc_id}"
        if dl:
            row.append(InlineKeyboardButton(self.cfg.download_button_text, url=dl))
        return InlineKeyboardMarkup([row]) if row else None

    async def publish(
        self,
        anime_doc_id: str,
        *,
        caption_override: str | None = None,
        silent: bool = False,
        client=None,
    ) -> int | None:
        """Post (or edit) the main-channel entry for a title. Returns the message id.

        ``caption_override`` replaces the templated caption verbatim (already
        finished HTML, e.g. an admin's hand-edited version). ``silent`` posts with
        notifications disabled — the "silent publish" option from Gojo's review card.
        """
        client = client or self._c.admin_client
        if not self._active(client):
            return None
        facts = await self.gather_facts(anime_doc_id)
        caption = caption_override if caption_override is not None else self._caption(facts)
        markup = await self._buttons(facts)

        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
            existing_id = post.main_message_id if post else None

        # Use the TMDB English backdrop as the post photo; fall back to poster.
        photo_url = facts.backdrop_url or facts.poster_url

        try:
            if existing_id:
                try:
                    # Main posts are normally photos, but a title without usable
                    # artwork is posted as text. Calling edit_message_caption on
                    # that text message produces a non-stale Telegram error and
                    # incorrectly turns a valid retry into a failed publish.
                    is_media = True
                    if hasattr(client, "get_messages"):
                        live = await client.get_messages(self.cfg.channel_id, existing_id)
                        is_media = any(
                            getattr(live, kind, None)
                            for kind in ("photo", "video", "animation", "document")
                        )
                    if is_media:
                        await client.edit_message_caption(
                            self.cfg.channel_id, existing_id, caption=caption,
                            reply_markup=markup, parse_mode=ParseMode.HTML,
                        )
                    else:
                        await client.edit_message_text(
                            self.cfg.channel_id, existing_id, caption,
                            reply_markup=markup, parse_mode=ParseMode.HTML,
                        )
                    message_id = existing_id
                except Exception as exc:
                    # A stale ChannelPost row must not turn a first publish into
                    # a silent no-op. This is the exact failure seen for Vanitas:
                    # Telegram rejected the old id, then the old code returned
                    # None without ever sending the new main-channel post.
                    if not is_stale_message_error(exc):
                        raise
                    log.warning("mainchannel.publish.stale_message",
                                anime=anime_doc_id, message_id=existing_id,
                                error=str(exc))
                    async with session_scope(self._c.pg_sessionmaker) as session:
                        row = (await session.execute(
                            select(ChannelPost).where(
                                ChannelPost.anime_doc_id == anime_doc_id
                            )
                        )).scalar_one_or_none()
                        if row is not None:
                            row.main_message_id = None
                    existing_id = None

            if not existing_id:
                if photo_url:
                    sent = await client.send_photo(
                        self.cfg.channel_id, photo_url, caption=caption,
                        reply_markup=markup, parse_mode=ParseMode.HTML,
                        disable_notification=silent,
                    )
                else:
                    sent = await client.send_message(
                        self.cfg.channel_id, caption, reply_markup=markup,
                        parse_mode=ParseMode.HTML, disable_notification=silent,
                    )
                message_id = sent.id
        except Exception as exc:  # noqa: BLE001
            log.warning("mainchannel.publish.failed", anime=anime_doc_id, error=str(exc))
            return None

        await self._record(anime_doc_id, message_id, facts.title)
        log.info("mainchannel.published", anime=anime_doc_id, message_id=message_id)
        return message_id

    async def refresh_thumbnail(self, anime_doc_id: str, image_path: str) -> bool:
        """Replace the live main-channel image without changing its caption.

        Thumbnail editing is deliberately media-only: the main post's caption and
        buttons are already the reviewed/published copy subscribers know. The
        durable backup is refreshed after Telegram accepts the new image so a later
        channel restore uses the corrected artwork too.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            return False
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (await session.execute(
                select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
            )).scalar_one_or_none()
            if post is None or not post.main_message_id:
                return False
            chat_id = post.main_channel_id or self.cfg.channel_id
            backup_caption = None
            from nekofetch.infrastructure.database.postgres.models import PublishedPostBackup
            backup = (await session.execute(
                select(PublishedPostBackup).where(
                    PublishedPostBackup.anime_doc_id == anime_doc_id
                )
            )).scalar_one_or_none()
            if backup is not None:
                backup_caption = backup.caption
            message_id = post.main_message_id

        caption = backup_caption
        markup = None
        try:
            if hasattr(client, "get_messages"):
                live = await client.get_messages(chat_id, message_id)
                # Use the HTML rendering (entities → tags) so re-applying the
                # caption doesn't strip the styling; keep the live keyboard so the
                # media swap doesn't drop the Index/Download buttons.
                src = getattr(live, "caption", None)
                if src is None:
                    src = getattr(live, "text", None)
                caption = getattr(src, "html", None) or (str(src) if src else None) or caption
                markup = getattr(live, "reply_markup", None)
            if not caption:
                log.warning("mainchannel.thumbnail_refresh.no_caption",
                            anime=anime_doc_id)
                return False
            # If the live message had no keyboard to read (id-only client), rebuild
            # from facts so we never post the main card button-less.
            if markup is None:
                try:
                    markup = await self._buttons(await self.gather_facts(anime_doc_id))
                except Exception:  # noqa: BLE001 — buttons are best-effort
                    markup = None
            # The webp card is the sticker format to Telegram's media endpoint,
            # so the live edit sends a JPEG conversion of the same render.
            photo = (await webp_to_jpeg_async(image_path)) or Path(image_path)
            await client.edit_message_media(
                chat_id,
                message_id,
                InputMediaPhoto(str(photo), caption=caption,
                                parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001 - editor reports a safe failure
            log.warning("mainchannel.thumbnail_refresh.failed",
                        anime=anime_doc_id, error=str(exc))
            return False

        try:
            from nekofetch.services.backup_service import BackupService
            await BackupService(self._c).update_main_thumbnail(
                anime_doc_id, image_path,
            )
        except Exception as exc:  # noqa: BLE001 - live edit already succeeded
            log.warning("mainchannel.thumbnail_backup.failed",
                        anime=anime_doc_id, error=str(exc))
        return True

    async def refresh_caption(self, anime_doc_id: str) -> bool:
        """Regenerate + edit the live main-post caption from the current facts.

        Used by the redo metadata refresh (Task O): after fresh packs land with
        a changed episode-count/language line, the caption must reflect it. The
        media (poster/thumbnail) and buttons are deliberately untouched — only
        the caption text is re-rendered and edited in place, and the durable
        backup caption is refreshed so a later restore uses the corrected copy.
        """
        client = getattr(self._c, "admin_client", None)
        if client is None:
            return False
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (await session.execute(
                select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
            )).scalar_one_or_none()
            if post is None or not post.main_message_id:
                return False
            chat_id = post.main_channel_id or self.cfg.channel_id
            message_id = post.main_message_id
        try:
            facts = await self.gather_facts(anime_doc_id)
            caption = self._caption(facts)
            # CRITICAL: editMessageCaption DROPS the inline keyboard unless it is
            # re-supplied. The main post carries Index + Download buttons — rebuild
            # them from the same facts publish() uses and hand them back, or a
            # caption refresh silently strips them.
            markup = await self._buttons(facts)
            await client.edit_message_caption(
                chat_id, message_id, caption=caption, parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001 - redo must survive a caption hiccup
            log.warning("mainchannel.caption_refresh.failed",
                        anime=anime_doc_id, error=str(exc))
            return False
        try:
            from nekofetch.services.backup_service import BackupService
            await BackupService(self._c).update_main_caption(anime_doc_id, caption)
        except Exception as exc:  # noqa: BLE001 - live edit already succeeded
            log.warning("mainchannel.caption_backup.failed",
                        anime=anime_doc_id, error=str(exc))
        return True

    async def distribution_link(self, anime_doc_id: str) -> str | None:
        """Return the current user-facing link for a title's distribution target."""
        async with session_scope(self._c.pg_sessionmaker) as session:
            bot = (
                await session.execute(
                    select(DistributionBot).where(
                        DistributionBot.anime_doc_id == anime_doc_id,
                        DistributionBot.enabled.is_(True),
                    ).order_by(DistributionBot.id.desc())
                )
            ).scalars().first()
        if bot is None or not bot.username and not bot.invite_link:
            return None
        if bot.is_channel:
            return bot.invite_link or f"https://t.me/{bot.username}"
        return f"https://t.me/{bot.username}?start=anime_{anime_doc_id}" if bot.username else None

    async def reply_update(
        self,
        anime_doc_id: str,
        entry_label: str,
        episodes: int | str,
        quality: str,
        channel_link: str,
    ) -> bool:
        """Reply to the existing main-channel post after a new entry is published.

        The reply deliberately has no keyboard: the only action is the localized
        hyperlink in the catalog template. This keeps update announcements readable
        and lets the owner reword them without a code change.
        """
        client = getattr(self._c, "admin_client", None)
        if not self._active(client) or client is None:
            return False
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
        if post is None or not post.main_message_id:
            return False
        from nekofetch.localization.messages import M, t
        async with session_scope(self._c.pg_sessionmaker) as session:
            pack = (
                await session.execute(
                    select(StoragePack)
                    .where(StoragePack.anime_doc_id == anime_doc_id)
                    .limit(1)
                )
            ).scalars().first()
        title = pack.anime_title if pack else anime_doc_id
        try:
            await client.send_message(
                self.cfg.channel_id,
                t(
                    M.SEASON_UPDATE_REPLY,
                    title=title,
                    entry_label=entry_label,
                    episodes=episodes,
                    quality=quality,
                    channel_link=channel_link,
                ),
                reply_to_message_id=post.main_message_id,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — announcement must not fail publish
            log.warning("mainchannel.update_reply.failed",
                        anime=anime_doc_id, error=str(exc))
            return False

    async def reply_recovery(
        self, anime_doc_id: str, title: str, channel_link: str, *, client=None,
    ) -> bool:
        """Reply to a main post after its distribution channel is restored."""
        client = client or getattr(self._c, "admin_client", None)
        if not self._active(client) or client is None:
            return False
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
        if post is None or not post.main_message_id:
            return False
        from nekofetch.localization.messages import M, t
        try:
            await client.send_message(
                self.cfg.channel_id,
                t(M.BAN_RECOVERY_REPLY, title=title, channel_link=channel_link),
                reply_to_message_id=post.main_message_id,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("mainchannel.recovery_reply.failed",
                        anime=anime_doc_id, error=str(exc))
            return False

    async def _record(self, anime_doc_id: str, message_id: int, title: str) -> None:
        from nekofetch.services.index_channel_service import IndexChannelService

        letter = IndexChannelService.letter_of(title)
        async with session_scope(self._c.pg_sessionmaker) as session:
            post = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.anime_doc_id == anime_doc_id)
                )
            ).scalar_one_or_none()
            if post is None:
                post = ChannelPost(anime_doc_id=anime_doc_id)
                session.add(post)
            post.main_channel_id = self.cfg.channel_id
            post.main_message_id = message_id
            post.index_letter = letter
