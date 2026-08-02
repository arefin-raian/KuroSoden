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

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.parsing import clean_anilist_id
from nekofetch.domain.enums import AudioType
from nekofetch.infrastructure.database.postgres.models import (
    ChannelPost,
    DistributionBot,
    StoragePack,
)
from nekofetch.infrastructure.database.postgres.session import session_scope
from nekofetch.ui import templates

log = get_logger(__name__)

_RES_ORDER = {"360p": 360, "480p": 480, "540p": 540, "720p": 720, "1080p": 1080}


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
# Audio track language as the user thinks of it: Dub = English, Sub = Japanese (Eng subs).
_AUDIO_LANG = {
    AudioType.DUBBED: ["English"],
    AudioType.SUBBED: ["Japanese"],
    AudioType.DUAL_AUDIO: ["English", "Japanese"],
    AudioType.MULTI: ["English", "Japanese", "Hindi"],
}


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
    rating: str = "—"                   # franchise-average AniList score, e.g. "8.4"
    poster_url: str | None = None
    backdrop_url: str | None = None   # TMDB English 16:9 backdrop for the post photo
    bot_username: str | None = None
    is_channel: bool = False            # True when distribution target is a channel, not a bot
    invite_link: str | None = None      # private invite link we minted for the channel
    anime_doc_id_bot: int | None = None  # DistributionBot.id backing this title, if any
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

    def _active(self) -> bool:
        client = getattr(self._c, "admin_client", None)
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
                    )
                )
            ).scalars().first()

        facts = PublicationFacts(anime_doc_id=anime_doc_id, title=anime_doc_id)
        if packs:
            facts.title = packs[0].anime_title
            resolutions = sorted({p.resolution for p in packs},
                                  key=lambda r: _RES_ORDER.get(r, 9999))
            facts.qualities = ", ".join(resolutions) or "—"
            langs: list[str] = []
            for p in packs:
                for lang in _AUDIO_LANG.get(p.audio, []):
                    if lang not in langs:
                        langs.append(lang)
            facts.languages = " & ".join(langs) or "—"
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
                # Episodes = Σ TV/TV_SHORT-season episodes (matches franchise_totals).
                tv_eps = sum(
                    (e.get("episodes") or 0)
                    for e in vals
                    if isinstance(e, dict) and e.get("format") in ("TV", "TV_SHORT")
                )
                if tv_eps:
                    facts.episodes = str(tv_eps)
                scores = [e.get("score") for e in vals
                          if isinstance(e, dict) and e.get("score") is not None]
                if scores:
                    facts.rating = f"{sum(scores) / len(scores):.1f}"
                if tv_eps or scores:
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

        # Episodes = Σ TV-season episodes only (continuity chain).
        try:
            totals = await anilist.franchise_totals(root_id)
            if totals.episodes:
                facts.episodes = str(totals.episodes)
        except Exception as exc:  # noqa: BLE001
            log.debug("mainchannel.franchise_totals.failed",
                      anime=anime_doc_id, error=str(exc))

        # Rating = average of every franchise entry's AniList score.
        try:
            entries = await anilist.walk_franchise_full(root_id)
            scores = [e.score for e in entries.values() if e.score is not None]
            if scores:
                facts.rating = f"{sum(scores) / len(scores):.1f}"
        except Exception as exc:  # noqa: BLE001
            log.debug("mainchannel.franchise_scores.failed",
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
        index_url = await IndexChannelService(self._c).entry_link(f.title)
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
    ) -> int | None:
        """Post (or edit) the main-channel entry for a title. Returns the message id.

        ``caption_override`` replaces the templated caption verbatim (already
        finished HTML, e.g. an admin's hand-edited version). ``silent`` posts with
        notifications disabled — the "silent publish" option from Gojo's review card.
        """
        if not self._active():
            return None
        facts = await self.gather_facts(anime_doc_id)
        caption = caption_override if caption_override is not None else self._caption(facts)
        markup = await self._buttons(facts)
        client = self._c.admin_client

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
                await client.edit_message_caption(
                    self.cfg.channel_id, existing_id, caption=caption, reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
                message_id = existing_id
            elif photo_url:
                sent = await client.send_photo(
                    self.cfg.channel_id, photo_url, caption=caption, reply_markup=markup,
                    parse_mode=ParseMode.HTML, disable_notification=silent,
                )
                message_id = sent.id
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
