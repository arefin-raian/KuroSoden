"""Senku's thumbnail loop — the admin thumbnail-channel UX, in a DM (Phase 3).

The plan's rule is *wrap, don't fork*: this reuses NekoFetch's thumbnail
machinery unchanged —

* ``fetch_logos`` / ``fetch_posters_ranked`` / ``fetch_backdrops_ranked`` (the
  EN-first, textless-backdrop asset fetchers),
* ``TelegraphClient.create_gallery`` (the numbered Telegraph gallery),
* ``gather_thumbnail_fields`` (the shared TMDB+AniList+pack-language enrichment,
  the same one ``ThumbnailChannelService.handle_generate`` now calls), and
* ``ThumbnailRenderService`` (the Playwright HTML→WebP render).

What changes is only the **surface** and the **store**: instead of the thumbnail
channel + its Redis workflow keys, selections land in :class:`DistributionCache`
keyed by request code + entry index, and the wizard renders the cards in the
admin's Senku DM. This class stays surface-agnostic — it fetches assets, builds
galleries, stores picks, and renders — while the wizard owns the card grammar
(art, voice, ``send_screen``) and routing.

The per-entry loop walks the cached, watch-ordered entries first→last:

    logo → poster → backdrop → generate → next entry → … → all done

Numbered buttons are laid out in even rows (three per row, matching the channel
service) so spacing stays uniform regardless of count. Selection callbacks live
under the wizard's own ``senku|wiz|`` namespace so its existing dispatcher routes
them — no separate registration. When the last entry renders, :meth:`is_complete`
reports done and the wizard advances to Phase 4.
"""

from __future__ import annotations

from pathlib import Path

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.providers.metadata.telegraph_client import ImageEntry, TelegraphClient
from nekofetch.providers.metadata.tmdb_assets import (
    fetch_backdrops_ranked, fetch_logos, fetch_posters_ranked,
)
from nekofetch.ui.components import cb

from kurosoden.shared.distribution_cache import DistributionCache, EntryData, Selection

log = get_logger(__name__)

BOT = "senku"

# Asset types walked per entry, in order. ``bg`` matches the cache field and the
# voice vocabulary; the TMDB fetcher calls the same thing a "backdrop".
ASSET_ORDER = ("logo", "poster", "bg")
_NUMS_PER_ROW = 3
_ASSET_FIELD = {"logo": "logo_url", "poster": "poster_url", "bg": "backdrop_url"}


def _entry_variant_key(entry: EntryData) -> str:
    """A per-entry disambiguator for the render output path.

    Cour splits (e.g. Vanitas S1P1 + S1P2) carry the SAME franchise title AND the
    same AniList id, so a title-only render path made the second entry overwrite
    the first's assets/webp — both cards then showed one thumbnail. Keying on
    ``season_part`` (plus the index as a final tiebreak) keeps them separate.
    """
    season = getattr(entry, "season_number", None)
    part = getattr(entry, "season_part", None)
    idx = getattr(entry, "index", None)
    bits = []
    if season is not None:
        bits.append(f"s{season}")
    if part is not None:
        bits.append(f"p{part}")
    bits.append(f"e{idx if idx is not None else 0}")
    return "_".join(bits)


class SenkuThumbnailAdapter:
    """Per-request thumbnail loop over the cached entries, rendered in Senku's DM.

    Stateless beyond the container; all working state is the shared
    :class:`DistributionCache` blob, so the adapter is safe to construct per call.
    """

    def __init__(self, container: Container) -> None:
        self._c = container
        self.cache = DistributionCache(container)
        self._telegraph: TelegraphClient | None = None
        self._render = None  # lazy ThumbnailRenderService
        # Set by render_entry on failure: "browser" (playwright not installed /
        # launch failed) vs "render" (everything else) — the wizard picks the
        # right operator-facing message from this.
        self.last_render_error: str | None = None

    # ── shared machinery (lazy) ─────────────────────────────────────────────

    def _telegraph_client(self) -> TelegraphClient | None:
        token = getattr(
            getattr(self._c.config, "thumbnail_channel", None),
            "telegraph_access_token", "",
        )
        if not token:
            return None
        if self._telegraph is None:
            self._telegraph = TelegraphClient(token)
        return self._telegraph

    def _renderer(self):
        if self._render is None:
            try:
                from nekofetch.services.thumbnail_service import ThumbnailRenderService
                self._render = ThumbnailRenderService()
            except Exception as exc:  # noqa: BLE001
                log.warning("senku.thumb.render_init_failed", error=str(exc))
        return self._render

    # ── TMDB resolution ─────────────────────────────────────────────────────

    async def _resolve_tmdb(self, code: str, entry: EntryData) -> tuple[int | None, str]:
        """Resolve this entry to a TMDB (id, media_type), caching the id back.

        TMDB lists assets at the FRANCHISE level, not per season/cour — a search
        for "… Season 2 Part 2" finds nothing or the wrong show. ``tmdb.search``
        strips season/part tokens down to the base franchise title, so every
        entry of a franchise resolves to the SAME TMDB id and therefore the same
        logo/poster/backdrop galleries (which is what we want: a cour split shows
        the franchise's assets). Per-season distinction is carried by AniList
        fields + the render ``variant_key``, not by a different TMDB lookup. The
        resolved id is persisted on the entry so repeat steps don't re-hit TMDB.
        """
        if entry.tmdb_id:
            return entry.tmdb_id, entry.media_type
        # The label is a bare "Season 01" / "Season 3 Part 2" for aggregated
        # franchises — it must NEVER become the TMDB query on its own (it resolves
        # to a random popular show: the "K-drama backdrops" bug). Only fall back to
        # the label when it carries real context beyond a season/part number.
        import re
        query = (entry.title or "").strip()
        if not query:
            label = (entry.label or "").strip()
            if label and not re.fullmatch(r"Season \d+( Part \d+)?", label):
                query = label
        if not query:
            return None, entry.media_type
        try:
            # Bias to anime (JP + animation) and hint the known media type so a
            # generically-named title doesn't resolve to a popular live-action
            # show (the wrong-images / "K-drama backdrops" bug).
            result = await self._c.tmdb.search(
                query, prefer_media=entry.media_type, anime=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.thumb.tmdb_search_failed", query=query, error=str(exc))
            return None, entry.media_type
        if result is None:
            return None, entry.media_type
        entry.tmdb_id = result.id
        entry.media_type = result.media_type or entry.media_type
        try:
            entries = await self.cache.get_entries(code)
            for e in entries:
                if e.index == entry.index:
                    e.tmdb_id = entry.tmdb_id
                    e.media_type = entry.media_type
            await self.cache.set_entries(code, entries)
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            log.debug("senku.thumb.tmdb_cache_failed", error=str(exc))
        return entry.tmdb_id, entry.media_type

    async def _root_doc_id(self, code: str) -> str | None:
        """Root ``anime_doc_id`` (the prefetch folder key) for a wizard ``code``.

        The prefetch cache is stored under the root title's anilist id, not the
        Senku request code, so the asset cache lookup needs this. Best-effort —
        ``None`` just means the cache is skipped and assets fetch live."""
        try:
            franchise = await self.cache.get_franchise(code) or {}
            doc = franchise.get("anime_doc_id") or franchise.get("anilist_id")
            return str(doc) if doc else None
        except Exception:  # noqa: BLE001
            return None

    async def fetch_assets(self, asset_type: str, tmdb_id: int,
                           media_type: str, anime_doc_id: str | None = None) -> list[dict]:
        """Ranked assets for one type — prefetch cache first, live on a miss.

        The prefetched ``tmdb.json`` (keyed by ``anime_doc_id`` = the root's
        anilist id) holds these same ranked lists from acceptance. It's guarded
        by ``tmdb_id`` via ``load_cached_tmdb_assets`` so a franchise entry whose
        own TMDB id differs from the cached root falls through to a live fetch of
        ITS assets — never the wrong installment's. ``anime_doc_id`` must be the
        prefetch folder key (the root anilist id); without it we skip the cache
        (the Senku wizard keys its own state by request code, which is NOT the
        folder key)."""
        cache_type = "backdrop" if asset_type in ("bg", "backdrop") else asset_type
        # Request-scoped cache is deliberately keyed by the franchise root, not
        # the season label. Every entry therefore receives the same TMDB gallery
        # while AniList-specific fields remain per-entry.
        if anime_doc_id:
            try:
                cached = await self.cache.get_tmdb_assets(str(anime_doc_id), cache_type)
                if cached is not None:
                    log.info("senku.thumb.cache_hit", type=asset_type, count=len(cached))
                    return cached
            except Exception as exc:  # noqa: BLE001 — cache miss → live below
                log.debug("senku.thumb.request_asset_cache_failed", error=str(exc))
        if anime_doc_id:
            try:
                from nekofetch.services.metadata_prefetch import load_cached_tmdb_assets

                cached = await load_cached_tmdb_assets(
                    self._c, anime_doc_id, cache_type,
                    anime_doc_id=anime_doc_id, tmdb_id=tmdb_id)
                if cached is not None:
                    await self.cache.set_tmdb_assets(str(anime_doc_id), cache_type, cached)
                    log.info("senku.thumb.cache_hit", type=asset_type,
                             count=len(cached))
                    return cached
            except Exception as exc:  # noqa: BLE001 — cache miss → live below
                log.debug("senku.thumb.cache_failed", error=str(exc))
        try:
            if asset_type == "logo":
                assets = await fetch_logos(self._c.tmdb, tmdb_id, media_type)
            elif asset_type == "poster":
                assets = await fetch_posters_ranked(self._c.tmdb, tmdb_id, media_type)
            elif asset_type in ("bg", "backdrop"):
                assets = await fetch_backdrops_ranked(self._c.tmdb, tmdb_id, media_type)
            else:
                assets = []
            if assets and anime_doc_id:
                await self.cache.set_tmdb_assets(str(anime_doc_id), cache_type, assets)
            return assets
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.thumb.fetch_failed", type=asset_type, error=str(exc))
        return []

    async def gallery_url(self, asset_type: str, title: str,
                          assets: list[dict]) -> str | None:
        """Build the numbered Telegraph gallery for ``assets`` (reused builder)."""
        telegraph = self._telegraph_client()
        if not telegraph or not assets:
            return None
        type_label = {"logo": "Logo", "poster": "Poster",
                      "bg": "Background"}.get(asset_type, asset_type)
        images: list[ImageEntry] = []
        for i, asset in enumerate(assets, start=1):
            parts = [str(i)]
            if asset.get("language") == "en":
                parts.append("English")
            elif not asset.get("language"):
                parts.append("Neutral")
            if asset_type == "logo":
                parts.append(f"({asset.get('width', 0)}x{asset.get('height', 0)})")
            images.append(ImageEntry(url=asset["url"], caption=" — ".join(parts)))
        try:
            page = await telegraph.create_gallery(
                title=f"{title} — {type_label}s", images=images, author_name="Senku",
            )
            return page.url
        except Exception as exc:  # noqa: BLE001
            log.warning("senku.thumb.gallery_failed", type=asset_type, error=str(exc))
            return None

    # ── numbered button rows (even rows, wizard-namespaced) ──────────────────

    @staticmethod
    def numbered_button_rows(code: str, index: int, asset_type: str,
                             count: int) -> list[list[tuple[str, str]]]:
        """``1 2 3 …`` selection buttons as ``(label, callback)`` rows, ≤3 per row.

        Even rows regardless of count — the same layout the channel service uses,
        so spacing never goes ragged. Callbacks are ``senku|wiz|pick|…`` so the
        wizard's existing dispatcher routes them.
        """
        rows: list[list[tuple[str, str]]] = []
        row: list[tuple[str, str]] = []
        for i in range(1, count + 1):
            row.append((str(i), cb(BOT, "wiz", "pick", code, str(index), asset_type, str(i))))
            if len(row) == _NUMS_PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    # ── step data for the wizard to render ───────────────────────────────────

    async def asset_step(self, code: str, entry: EntryData, asset_type: str):
        """Everything the wizard needs to render one asset-pick card.

        Returns ``(assets, gallery_url, button_rows)``. ``assets`` empty means
        TMDB had nothing for this type — the wizard shows the "skip/none" path.
        """
        tmdb_id, media_type = await self._resolve_tmdb(code, entry)
        if not tmdb_id:
            return [], None, []
        doc_id = await self._root_doc_id(code)
        assets = await self.fetch_assets(asset_type, tmdb_id, media_type,
                                         anime_doc_id=doc_id)
        if not assets:
            return [], None, []
        title = entry.title or entry.label
        gallery = await self.gallery_url(asset_type, title, assets)
        rows = self.numbered_button_rows(code, entry.index, asset_type, len(assets))
        return assets, gallery, rows

    async def store_pick(self, code: str, index: int, asset_type: str,
                         number: int) -> tuple[Selection, str | None]:
        """Persist a numbered pick; return the updated selection and next asset.

        Re-fetches the ranked assets to map ``number`` → URL (the same list the
        gallery was built from, ranking is deterministic). Returns
        ``(selection, next_asset_or_None)`` — None means the entry is ready to
        render.
        """
        entry = await self.cache.get_entry(code, index)
        if entry is None:
            return Selection(), None
        tmdb_id, media_type = await self._resolve_tmdb(code, entry)
        doc_id = await self._root_doc_id(code)
        assets = (await self.fetch_assets(asset_type, tmdb_id, media_type,
                                          anime_doc_id=doc_id)
                  if tmdb_id else [])
        if not assets or number < 1 or number > len(assets):
            sel = await self.cache.get_selection(code, index)
            return sel, self.next_asset(sel)
        url = assets[number - 1]["url"]
        sel = await self.cache.set_selection(code, index, asset=asset_type, value=url)
        return sel, self.next_asset(sel)

    async def store_text_logo(self, code: str, index: int, path) -> tuple[Selection, str | None]:
        """Mirror a generated transparent text logo and save it as ``logo_url``.

        This deliberately uses the same ``logo_url`` selection field as TMDB picks
        and manual uploads. No new Redis field or database table is introduced.
        """
        from kurosoden.shared.image_backup import backup_bytes

        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"text-logo preview no longer exists: {path}")
        backup = await backup_bytes(
            self._c, path.read_bytes(), mime="image/png", source_url=f"file://{path}",
        )
        url = backup.primary or f"file://{path}"
        sel = await self.cache.set_selection(code, index, asset="logo", value=url)
        return sel, self.next_asset(sel)

    async def store_upload(self, code: str, index: int, asset_type: str,
                           file_bytes: bytes,
                           ) -> tuple[Selection, str | None]:
        """Persist an admin-uploaded asset image; return (selection, next asset).

        The bytes are mirrored through :func:`image_backup.backup_bytes` (catbox
        primary, telegraph fallback) so the render step (and a later channel
        rebuild) sees a stable public URL that outlives a single host — identical
        downstream to a numbered pick. Raises on total upload failure so the
        caller can voice a retry; a success stores the URL ``store_pick`` uses.
        """
        from kurosoden.shared.image_backup import backup_bytes

        backup = await backup_bytes(self._c, file_bytes, mime="image/jpeg")
        url = backup.primary
        if not url:
            raise RuntimeError(f"every image host rejected the {asset_type} upload")
        sel = await self.cache.set_selection(code, index, asset=asset_type, value=url)
        return sel, self.next_asset(sel)

    async def render_entry(self, code: str, entry: EntryData) -> "object | None":
        """Render this entry's thumbnail from its picks; mark done on success.

        Uses the shared ``gather_thumbnail_fields`` enrichment + NekoFetch's
        ``ThumbnailRenderService`` — identical output to the channel path. Returns
        the rendered ``Path`` or None on failure (the entry stays not-done so the
        admin can retry).
        """
        sel = await self.cache.get_selection(code, entry.index)
        if not (sel.logo_url and sel.poster_url and sel.backdrop_url):
            return None
        self.last_render_error = None
        renderer = self._renderer()
        if renderer is None:
            # Renderer couldn't even initialise → almost always a missing browser.
            self.last_render_error = "browser"
            return None
        franchise = await self.cache.get_franchise(code) or {}
        anime_doc_id = franchise.get("anime_doc_id")
        title = entry.title or entry.label
        try:
            from nekofetch.services.thumbnail_service import (
                gather_thumbnail_fields,
                persist_thumbnail_source,
            )
            # Distribution entry cards describe THAT season → AniList per-entry
            # synopsis (TMDB fallback). Without this flag the render baked the
            # franchise-level TMDB overview onto every season card. Passing the
            # entry's own anilist_id also routes romaji/native/score/year/runtime
            # to THIS installment's node (season 2 ≠ season 1) instead of the
            # franchise root ``search`` blob.
            fields = await gather_thumbnail_fields(
                self._c, title, anime_doc_id, prefer_anilist_synopsis=True,
                anilist_id=entry.anilist_id)
            source_fields = {
                **fields,
                "title": title,
                "logo_url": sel.logo_url,
                "poster_url": sel.poster_url,
                "bg_url": sel.backdrop_url,
                "entry_label": entry.label,
                "entry_index": entry.index,
                "anilist_id": entry.anilist_id,
            }
            path = await renderer.render_thumbnail(
                title=title,
                logo_url=sel.logo_url,
                poster_url=sel.poster_url,
                bg_url=sel.backdrop_url,
                variant_key=_entry_variant_key(entry),
                **fields,
            )
            await persist_thumbnail_source(
                self._c, anime_doc_id, entry.anilist_id, source_fields,
                image_path=path,
            )
        except Exception as exc:  # noqa: BLE001
            # Flag a missing/broken headless browser distinctly so the wizard can
            # tell the operator to run `playwright install` instead of blaming the
            # network. Playwright surfaces this as an Error mentioning the missing
            # executable or as an ImportError when the package isn't installed.
            msg = str(exc).lower()
            self.last_render_error = (
                "browser"
                if ("playwright" in msg or "executable doesn't exist" in msg
                    or "browsertype.launch" in msg or "install" in msg)
                else "render"
            )
            log.warning("senku.thumb.render_failed", code=code, entry=entry.index,
                        error=str(exc))
            return None
        if not path:
            return None
        # Mirror the render across the image hosts RIGHT NOW. The render is only
        # preview material until the operator approves, but if the session dies
        # (or the DM preview can't be sent) the card must already live somewhere
        # public — today the ONLY uploader was the publish-time bridge, so a
        # render that never reached publish was lost entirely (the "rendered but
        # never uploaded" bug). The DM preview still uses the local ``path``;
        # the STORED url is the primary mirror, falling back to ``file://`` when
        # every host rejects the upload (the publisher bridge still understands
        # that). The wizard's explicit Approve action remains the sole transition
        # that marks an entry done.
        stored: str = f"file://{path}"
        try:
            from kurosoden.shared.image_backup import backup_bytes

            suffix = str(path).lower()
            mime = ("image/webp" if suffix.endswith(".webp")
                    else "image/png" if suffix.endswith(".png")
                    else "image/jpeg")
            backup = await backup_bytes(self._c, path.read_bytes(), mime=mime)
            if backup.primary:
                stored = backup.primary
                log.info("senku.thumb.hosted", code=code, entry=entry.index,
                         url=backup.primary)
        except Exception as exc:  # noqa: BLE001 — file:// fallback still works
            log.warning("senku.thumb.host_failed", code=code, entry=entry.index,
                        error=str(exc))
        await self.cache.set_selection(code, entry.index, asset="thumbnail",
                                       value=stored)
        log.info("senku.thumb.rendered", code=code, entry=entry.index, path=str(path))
        return path

    async def _franchise_avg_score(self, anime_doc_id: str | None) -> int | None:
        """Franchise-average AniList score on the 0-100 ring scale, or None.

        Same source + averaging as the main-channel post CAPTION rating
        (``main_channel_service._apply_franchise_facts`` / ``_avg_score_pct``):
        the cached AniList walk's per-entry ``score`` (0-10), averaged and scaled
        to 0-100. Cache-only (no live call) — best-effort, so a miss just leaves
        the ring on whatever ``gather_thumbnail_fields`` produced."""
        if not anime_doc_id:
            return None
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            blob = await load_cached(self._c, anime_doc_id, "anilist",
                                     anime_doc_id=anime_doc_id)
            walk = (blob or {}).get("franchise")
            if not walk:
                return None
            vals = list(walk.values()) if isinstance(walk, dict) else list(walk)
            scores = [float(e["score"]) for e in vals
                      if isinstance(e, dict) and e.get("score") is not None]
            if not scores:
                return None
            avg = sum(scores) / len(scores)
            pct = avg if avg > 10 else avg * 10  # 0-10 → 0-100; leave 0-100 alone
            return int(round(pct))
        except Exception as exc:  # noqa: BLE001 — averaging is best-effort
            log.debug("senku.thumb.avg_score_failed", anime=anime_doc_id,
                      error=str(exc))
            return None

    async def render_main(self, code: str) -> "object | None":
        """Render the MAIN-CHANNEL post thumbnail for the franchise (no preview).

        Distinct from the per-entry distribution cards: the franchise-level TMDB
        overview (AniList fallback) for the synopsis, and the franchise-AVERAGE
        AniList score on the ring (the TMDB badge stays series-level). Reuses the
        BASE (first watch-order) entry's picked logo/poster/backdrop — the main
        card mirrors S1's art, only the info differs. Persists a ``ThumbnailSource``
        row keyed ``anilist_id=-1`` (the established main sentinel) with the
        mirrored public URL in ``fields['hosted_url']`` so ``main_channel_service``
        can consume it. Returns the rendered ``Path`` or None (best-effort — the
        distribution publish has already succeeded when this runs)."""
        renderer = self._renderer()
        if renderer is None:
            self.last_render_error = "browser"
            return None
        franchise = await self.cache.get_franchise(code) or {}
        anime_doc_id = franchise.get("anime_doc_id")
        # Base entry = the first in watch order; it supplies the art + title.
        entries = sorted(await self.cache.get_entries(code),
                         key=lambda e: getattr(e, "index", 0))
        base = entries[0] if entries else None
        if base is None:
            return None
        sel = await self.cache.get_selection(code, base.index)
        if not (sel and sel.logo_url and sel.poster_url and sel.backdrop_url):
            log.info("senku.thumb.main_no_assets", code=code)
            return None
        title = (franchise.get("english") or franchise.get("title")
                 or base.title or base.label)
        try:
            from nekofetch.services.thumbnail_service import (
                gather_thumbnail_fields,
                persist_thumbnail_source,
            )
            # Main post summarizes the whole franchise → TMDB overview (AniList
            # fallback); prefer_anilist_synopsis=False is the surface switch.
            fields = await gather_thumbnail_fields(
                self._c, title, anime_doc_id, prefer_anilist_synopsis=False)
            # Ring = franchise-average AniList score (matches the caption rating);
            # the TMDB badge stays whatever gather resolved (series-level).
            avg = await self._franchise_avg_score(anime_doc_id)
            if avg is not None:
                fields["anilist_score"] = avg
            source_fields = {
                **fields,
                "title": title,
                "logo_url": sel.logo_url,
                "poster_url": sel.poster_url,
                "bg_url": sel.backdrop_url,
                "entry_label": title,
                "anilist_id": -1,          # main-channel sentinel
            }
            path = await renderer.render_thumbnail(
                title=title,
                logo_url=sel.logo_url,
                poster_url=sel.poster_url,
                bg_url=sel.backdrop_url,
                variant_key="main",
                **fields,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            self.last_render_error = (
                "browser"
                if ("playwright" in msg or "executable doesn't exist" in msg
                    or "browsertype.launch" in msg or "install" in msg)
                else "render"
            )
            log.warning("senku.thumb.main_render_failed", code=code, error=str(exc))
            return None
        if not path:
            return None
        # Mirror to a public host so the main-channel post (published downstream by
        # Gojo) can consume a durable URL; stash it in the persisted fields.
        stored: str = f"file://{path}"
        try:
            from kurosoden.shared.image_backup import backup_bytes

            suffix = str(path).lower()
            mime = ("image/webp" if suffix.endswith(".webp")
                    else "image/png" if suffix.endswith(".png")
                    else "image/jpeg")
            backup = await backup_bytes(self._c, path.read_bytes(), mime=mime)
            if backup.primary:
                stored = backup.primary
        except Exception as exc:  # noqa: BLE001 — file:// fallback still works
            log.warning("senku.thumb.main_host_failed", code=code, error=str(exc))
        source_fields["hosted_url"] = stored
        try:
            # anilist_id=None → persisted under the -1 main sentinel.
            await persist_thumbnail_source(
                self._c, anime_doc_id, None, source_fields, image_path=path)
        except Exception as exc:  # noqa: BLE001 — the URL is still returned
            log.warning("senku.thumb.main_persist_failed", code=code, error=str(exc))
        log.info("senku.thumb.main_rendered", code=code, url=stored)
        return path

    # ── loop-state query helpers ─────────────────────────────────────────────

    async def next_pending(self, code: str) -> EntryData | None:
        """The first not-yet-rendered entry (watch order), or None when all done."""
        entries = await self.cache.get_entries(code)
        selections = await self.cache.get_selections(code)
        for e in entries:
            sel = selections.get(e.index)
            if sel is None or not sel.done:
                return e
        return None

    async def is_complete(self, code: str) -> bool:
        return await self.cache.all_done(code)

    @staticmethod
    def next_asset(sel: Selection) -> str | None:
        """The next asset an entry still needs (logo→poster→bg), or None if ready."""
        have = {
            "logo": bool(sel.logo_url),
            "poster": bool(sel.poster_url),
            "bg": bool(sel.backdrop_url),
        }
        for a in ASSET_ORDER:
            if not have[a]:
                return a
        return None
