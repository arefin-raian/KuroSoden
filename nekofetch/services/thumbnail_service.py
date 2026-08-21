"""Thumbnail renderer — uses Playwright to render the HTML template to an image.

Takes ``thumbnail/index.html`` as the base template and substitutes real
per-anime data into ``{{TOKENS}}`` at render time. Uses headless Chromium for
faithful rendering of CSS, fonts, SVGs, gradients, and custom typography.

The template is entirely viewport-relative (``vw``/``vh``/``%``), tuned for a
wide ~2.13:1 canvas — so we render at that aspect (1366×641, ×2 for crispness)
to match the reference design exactly instead of squishing it into 16:9.

Data sources (wired by the caller): TMDB supplies the backdrop (textless),
logo, meta line, rating, studio, and origin-country flag; AniList supplies the
romaji/native titles, the seasonal poster, and the score ring.

The output is a high-quality ``.webp`` image.
"""

from __future__ import annotations

import asyncio
import html as html_module
import math
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from nekofetch.core.logging import get_logger
from nekofetch.infrastructure.database.postgres.models import ThumbnailSource
from nekofetch.infrastructure.database.postgres.session import session_scope

log = get_logger(__name__)

# repo layout: <repo>/nekofetch/services/thumbnail_service.py — so parents[2]
# is the repo root, where the sibling `thumbnail/` and `data/` dirs live.
# (Was parents[3], which climbed one level ABOVE the repo → on prod that
# resolved to /root/thumbnail/index.html and the template was never found.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _REPO_ROOT / "thumbnail"
_DEFAULT_TEMPLATE = _TEMPLATE_DIR / "index.html"
_OUTPUT_DIR = _REPO_ROOT / "data" / "thumbnails"

# The template's native design ratio (matches the reference browser render at
# 1366×641). device_scale_factor=3 triples the output resolution for high-quality
# export (→ 4098×1923 physical pixels). WEBP quality=95 for near-lossless output.
_THUMBNAIL_WIDTH = 1366
_THUMBNAIL_HEIGHT = 641
_THUMBNAIL_SCALE = 3   # device_scale_factor — increase for higher export resolution
_SYNOPSIS_MAX_CHARS = 300

# The SVG score ring: r=42 → circumference = 2·π·42 ≈ 263.89. dashoffset is the
# UNfilled remainder, so offset = C · (1 - pct/100).
_RING_CIRCUMFERENCE = 2 * math.pi * 42

# How many genre pills fit before the row visually overflows the design width.
# We budget by rendered character count rather than a fixed count, since "Slice
# of Life" eats far more room than "Action".
_GENRE_CHAR_BUDGET = 42
_GENRE_MAX_PILLS = 5


# ── Owner-tunable card styling ────────────────────────────────────────────────
# The renderer is container-less (constructed as ``ThumbnailRenderService()`` at
# many call sites), so it can't read ``container.config`` directly. The container
# installs a provider at startup that returns the LIVE ``ThumbnailStyleConfig``
# (which SettingsService keeps in sync with DB overrides), so a Senku settings
# edit is reflected on the very next render with no per-call-site plumbing.
_STYLE_PROVIDER: "Any | None" = None


def set_thumbnail_style_provider(provider) -> None:
    """Install a ``() -> ThumbnailStyleConfig`` provider (called once by the
    container at startup). Passing ``None`` clears it (tests / standalone)."""
    global _STYLE_PROVIDER
    _STYLE_PROVIDER = provider


def _resolve_style():
    """The current thumbnail style: the provider's live config, else defaults."""
    from nekofetch.core.config import ThumbnailStyleConfig
    if _STYLE_PROVIDER is not None:
        try:
            style = _STYLE_PROVIDER()
            if style is not None:
                return style
        except Exception as exc:  # noqa: BLE001 — never fail a render on style lookup
            log.debug("thumbnail.style_provider_failed", error=str(exc))
    return ThumbnailStyleConfig()


def _style_tokens() -> dict[str, str]:
    """Numeric ``{{STYLE_*}}`` substitutions for ``thumbnail/index.html``.

    All values are plain numbers embedded into existing CSS / Tailwind arbitrary
    values, so the template stays valid after substitution. The three overlay
    stops are derived from one ``overlay_darkness`` knob, preserving the template's
    original 0.80/0.30/0.10 ratio."""
    st = _resolve_style()
    op = float(st.shadow_opacity)
    d = float(st.overlay_darkness)
    return {
        "{{STYLE_SHADOW_BLUR}}": str(int(st.shadow_blur_px)),
        "{{STYLE_SHADOW_OPACITY}}": f"{round(op, 3)}",
        "{{STYLE_SHADOW_OPACITY2}}": f"{round(op * 0.7, 3)}",
        "{{STYLE_OVERLAY_FROM}}": str(int(round(d * 100))),
        "{{STYLE_OVERLAY_VIA}}": str(int(round(d * 100 * 0.375))),
        "{{STYLE_OVERLAY_TO}}": str(int(round(d * 100 * 0.125))),
        "{{STYLE_LOGO_ALPHA}}": f"{round(float(st.logo_shadow_opacity), 3)}",
        "{{STYLE_POSTER_ALPHA}}": f"{round(float(st.poster_shadow_opacity), 3)}",
        "{{STYLE_RING_ALPHA}}": f"{round(float(st.ring_shadow_opacity), 3)}",
        "{{STYLE_SYNOPSIS_PX}}": str(int(st.synopsis_px)),
        "{{STYLE_LOGO_HEIGHT}}": f"{round(float(st.logo_height_rem), 3)}",
    }


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text and append ``…`` if too long."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1].rstrip() + "…"


def _fit_genres(genres: list[str]) -> list[str]:
    """Pick as many genres as fit the row budget — never fewer than one, never
    so many they wrap and break the layout. Small genre sets show in full; long
    ones are trimmed by a rendered-width budget, not a hard count."""
    out: list[str] = []
    used = 0
    for g in genres:
        g = (g or "").strip()
        if not g:
            continue
        # +3 approximates the pill padding/gap in character-width terms.
        cost = len(g) + 3
        if out and (used + cost > _GENRE_CHAR_BUDGET or len(out) >= _GENRE_MAX_PILLS):
            break
        out.append(g)
        used += cost
    return out


def _genre_pill(label: str) -> str:
    """One genre pill — markup copied verbatim from the template so the dynamic
    pills are pixel-identical to the original hardcoded ones."""
    return (
        '<span class="border border-white/30 bg-black/10 px-[1.1rem] py-1.5 '
        'rounded-full text-md font-medium tracking-wider text-zinc-100 '
        f'backdrop-blur-xs">{html_module.escape(label)}</span>'
    )


def _encode_png_to_webp(png_bytes: bytes, output_path: Path) -> None:
    """Encode PNG bytes → WEBP (CPU-heavy; always run via ``asyncio.to_thread``)."""
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png_bytes)) as im:
        im.save(output_path, format="WEBP", quality=95, method=6)


async def webp_to_jpeg_async(path: str | Path, *, quality: int = 92) -> Path | None:
    """Async wrapper for :func:`webp_to_jpeg` — offloads the PIL decode/encode to a
    worker thread so a photo-send path never blocks the shared event loop."""
    return await asyncio.to_thread(webp_to_jpeg, path, quality=quality)


def webp_to_jpeg(path: str | Path, *, quality: int = 92) -> Path | None:
    """Convert a rendered ``.webp`` card to a JPEG Telegram can show as a photo.

    The renderer outputs WebP (small, lossy-95) but Telegram's photo endpoint is
    unreliable with WebP — it is the sticker format, so ``send_photo`` of a
    ``.webp`` can hang or be rejected (the "Gallery didn't load" preview bug:
    the render succeeded, the image hosts accepted it, and only the DM preview
    failed). Every photo ``send_photo``/``edit_message_media`` path converts
    through here first; the stored/published artifact stays WebP.

    Writes ``<stem>.jpg`` next to the source and returns its ``Path``; ``None``
    when the source is missing or PIL can't decode it (callers fall back to the
    original path so a conversion failure never regresses the send).
    """
    src = Path(path)
    try:
        from PIL import Image

        with Image.open(src) as im:
            # Flatten any alpha (RGBA/P) onto white so JPEG has nothing to drop.
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            dest = src.with_suffix(".jpg")
            # Already a JPEG (or the suffix collides): there is nothing to
            # convert, and saving over the source is fragile on Windows.
            if dest.resolve() == src.resolve():
                return src
            im.save(dest, format="JPEG", quality=quality, optimize=True)
            return dest
    except Exception as exc:
        log.warning("thumbnail.webp_to_jpeg.failed", path=str(src), error=str(exc))
        return None


async def _download_image(url: str, dest: Path) -> Path | None:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            return dest
    except Exception as exc:
        log.warning("thumbnail.download.failed", url=(url or "")[:80], error=str(exc))
        return None


_HTML_TEMPLATE: str | None = None


def _load_template() -> str:
    global _HTML_TEMPLATE
    if _HTML_TEMPLATE is None:
        if _DEFAULT_TEMPLATE.exists():
            _HTML_TEMPLATE = _DEFAULT_TEMPLATE.read_text(encoding="utf-8")
        else:
            log.warning("thumbnail.template.not_found", path=str(_DEFAULT_TEMPLATE))
            _HTML_TEMPLATE = "<html><body><h1>No template</h1></body></html>"
    return _HTML_TEMPLATE


# Country (ISO-3166 alpha-2, as TMDB returns in ``origin_country``) → the flag
# emoji-free approach: we use a flag image CDN so the card can show any country,
# not just Japan. flagcdn serves clean PNG flags by lowercase code.
def _flag_url(country: str | None) -> str:
    code = (country or "JP").strip().lower()[:2] or "jp"
    return f"https://flagcdn.com/w80/{code}.png"


async def gather_thumbnail_fields(container: Any, title: str,
                                  anime_doc_id: str | None = None,
                                  *, prefer_anilist_synopsis: bool = False,
                                  anilist_id: int | None = None) -> dict:
    """Enrich a title into the display fields ``render_thumbnail`` consumes.

    Pulls facts from TMDB (overview, native title, studio, rating, country,
    genres, year/cert/runtime meta line) and AniList (romaji/native title, genres,
    studio, 0–100 score) and derives the audio-language label from the title's
    enabled ``StoragePack`` rows — the same lookup ``bot_factory`` uses. Every
    provider call is best-effort: a miss degrades one field, never the whole set.

    ``prefer_anilist_synopsis`` routes the baked synopsis by SURFACE: distribution
    entry-card renders pass ``True`` (each card describes THAT title via AniList's
    synopsis), while the main-channel-post render leaves it ``False`` (the
    franchise-level TMDB overview). Either way the other source is the fallback.
    Rating stays TMDB on every surface (series-level).

    ``anilist_id`` targets a SPECIFIC franchise installment (distribution entry
    cards for season 2+): its romaji/native/score/synopsis/episodes/year/runtime
    are read from that entry's node in the cached franchise walk
    (``anilist.json["franchise"][anilist_id]``) — the same per-entry source the
    main-channel post averages — instead of the franchise ROOT ``search`` blob
    (which is season 1). On a cache miss it falls back to a live *resilient* fetch
    by id (``container.anilist._fetch_full`` → AniList/Kaggle/Jikan/Kitsu), so a
    title stored without a franchise walk still gets per-entry data. Only the
    content rating (TV-14) stays series-level from TMDB; year + runtime come from
    the entry's AniList node.

    The user-picked logo/poster/backdrop are NOT set here (the caller owns those);
    this fills everything else. Returns a kwargs dict ready to splat into
    ``render_thumbnail`` alongside the chosen art.
    """
    synopsis = ""
    tmdb_synopsis = anilist_synopsis = ""
    native_title = romaji_title = studio = language = ""
    meta_bits: list[str] = []
    genres: list[str] = []
    tmdb_rating = anilist_score = None
    country = None

    # ── TMDB: prefetch cache first, live search only on a miss ──
    # The tmdb.json ``result`` saved at acceptance carries the same fields; read
    # it (keyed by the root ``anime_doc_id``) before ever hitting TMDB live.
    tmdb_result = None
    tmdb_cached: dict | None = None
    if anime_doc_id:
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            tblob = await load_cached(container, anime_doc_id, "tmdb",
                                      anime_doc_id=anime_doc_id)
            if tblob:
                tmdb_cached = tblob.get("result") or None
        except Exception as exc:  # noqa: BLE001 — cache miss → live below
            log.debug("thumbfields.tmdb_cache.failed", error=str(exc))
    if tmdb_cached is None:
        try:
            tmdb_result = await container.tmdb.search(title)
        except Exception as exc:  # noqa: BLE001
            log.debug("thumbfields.tmdb.failed", error=str(exc))

    def _t(key: str, default: Any = None) -> Any:
        if tmdb_cached is not None:
            return tmdb_cached.get(key, default)
        return getattr(tmdb_result, key, default) if tmdb_result else default

    if tmdb_cached is not None or tmdb_result:
        tmdb_synopsis = _t("overview") or ""
        native_title = _t("native_title") or ""
        studio = _t("studio") or ""
        tmdb_rating = _t("rating")
        country = _t("origin_country")
        genres = list(_t("genres") or [])
        meta_bits = [b for b in (
            _t("year"), _t("certification"), _t("runtime")
        ) if b]

    # ── AniList: prefetch cache first, live search only on a miss ──
    anilist_media = None
    anilist_cached: dict | None = None
    anilist_franchise: dict | None = None
    if anime_doc_id:
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            ablob = await load_cached(container, anime_doc_id, "anilist",
                                      anime_doc_id=anime_doc_id)
            if ablob:
                anilist_cached = ablob.get("search") or None
                # Per-entry data (season 2+ cards) lives in the franchise walk.
                fr = ablob.get("franchise")
                if isinstance(fr, dict):
                    anilist_franchise = fr
        except Exception as exc:  # noqa: BLE001 — cache miss → live below
            log.debug("thumbfields.anilist_cache.failed", error=str(exc))
    if anilist_cached is None:
        try:
            anilist_media = await container.anilist.search(title)
        except Exception as exc:  # noqa: BLE001
            log.debug("thumbfields.anilist.failed", error=str(exc))

    def _a(key: str, default: Any = None) -> Any:
        if anilist_cached is not None:
            return anilist_cached.get(key, default)
        return getattr(anilist_media, key, default) if anilist_media else default

    if anilist_cached is not None or anilist_media:
        romaji_title = _a("romaji") or ""
        _titles = _a("titles") or []
        if not native_title and len(_titles) >= 3:
            native_title = _titles[2] or ""
        if _a("genres"):
            genres = list(_a("genres"))
        if _a("studio"):
            studio = _a("studio")
        _score = _a("score")
        if _score is not None:
            # AnilistMedia.score is 0-10; the ring wants 0-100.
            anilist_score = round(_score * 10)
        anilist_synopsis = _a("synopsis") or ""

    # ── Per-entry override (distribution entry cards for season 2+) ──
    # Season 2's romaji/native/score/synopsis/year/runtime must come from ITS OWN
    # AniList node, not the franchise root ``search`` blob (season 1). Read the
    # cached franchise walk node first (offline); on a miss fall back to a live
    # *resilient* fetch by id so a title stored without a walk still resolves from
    # whatever tier (AniList → Kaggle → Jikan → Kitsu) carries it.
    entry_year: str | None = None
    entry_runtime: str | None = None
    if anilist_id is not None:
        node: dict | None = None
        if anilist_franchise:
            node = (anilist_franchise.get(str(anilist_id))
                    or anilist_franchise.get(anilist_id))
        if node is None:
            try:
                ani = getattr(container, "anilist", None)
                media = await ani._fetch_full(int(anilist_id)) if ani else None
            except Exception as exc:  # noqa: BLE001 — live per-entry is best-effort
                media = None
                log.debug("thumbfields.entry.live_failed",
                          anilist_id=anilist_id, error=str(exc))
            if media is not None:
                node = {
                    "titles": list(getattr(media, "titles", None) or []),
                    "score": getattr(media, "score", None),
                    "synopsis": getattr(media, "synopsis", None),
                    "duration": getattr(media, "duration", None),
                    "start_date": (getattr(media, "start_date", None)
                                   or ({"year": media.year}
                                       if getattr(media, "year", None) else None)),
                }
        if node:
            _et = node.get("titles") or []
            if len(_et) >= 2 and _et[1]:
                romaji_title = _et[1]
            if len(_et) >= 3 and _et[2]:
                native_title = _et[2]
            _es = node.get("score")
            if _es is not None:
                # Node/media score is 0-10; the ring wants 0-100.
                anilist_score = round(float(_es) * 10)
            if node.get("synopsis"):
                anilist_synopsis = node["synopsis"]
            _sd = node.get("start_date") or {}
            if _sd.get("year"):
                entry_year = str(_sd["year"])
            if node.get("duration"):
                # AniList duration is minutes/episode; match TMDB's "24m" label.
                entry_runtime = f"{int(node['duration'])}m"

    # Route the baked synopsis by surface: distribution entry cards prefer the
    # per-title AniList synopsis; the main-channel post prefers the franchise-
    # level TMDB overview. The other source is always the fallback, and Jikan
    # (below) is the last-resort fill when both are empty.
    if prefer_anilist_synopsis:
        synopsis = anilist_synopsis or tmdb_synopsis
    else:
        synopsis = tmdb_synopsis or anilist_synopsis

    # ── Jikan (MyAnimeList) cache: last-resort fill for synopsis / score /
    # genres when neither TMDB nor AniList supplied them. This is the read-side
    # consumer of the prefetched jikan.json — without it the prefetch is dead
    # weight. Cache-only (no live call); a hit logs jikan.cache.hit.
    if anime_doc_id and (not synopsis or anilist_score is None or not genres):
        try:
            from nekofetch.services.metadata_prefetch import load_cached_jikan

            jk = await load_cached_jikan(container, anime_doc_id,
                                         anime_doc_id=anime_doc_id)
            if jk:
                if not synopsis and jk.get("synopsis"):
                    synopsis = jk["synopsis"]
                if anilist_score is None and jk.get("score") is not None:
                    # MAL score is 0-10; the ring wants 0-100.
                    anilist_score = round(float(jk["score"]) * 10)
                if not genres and jk.get("genres"):
                    genres = [g.get("name") for g in jk["genres"]
                              if isinstance(g, dict) and g.get("name")]
        except Exception as exc:  # noqa: BLE001 — cache miss / shape drift
            log.debug("thumbfields.jikan_cache.failed", error=str(exc))

    # Language label from what the title actually carries (see bot_factory):
    #   sub→Japanese, dub→English, dual→Japanese & English,
    #   multi→Japanese, English & Hindi. Union per-pack audio into one label.
    if anime_doc_id:
        try:
            from nekofetch.services.audio_langs import pack_languages
            from nekofetch.services.bot_naming import language_label
            from nekofetch.infrastructure.database.postgres.models import StoragePack
            from nekofetch.infrastructure.database.postgres.session import session_scope
            from sqlalchemy import select

            async with session_scope(container.pg_sessionmaker) as session:
                packs = (await session.execute(
                    select(StoragePack).where(
                        StoragePack.anime_doc_id == anime_doc_id,
                        StoragePack.enabled.is_(True),
                    )
                )).scalars().all()
                # Real probed languages per pack, else the enum fallback.
                langs = pack_languages(packs)
            if langs:
                language = language_label(langs)
        except Exception as exc:  # noqa: BLE001
            log.debug("thumbfields.language.failed", error=str(exc))

    # For a per-entry card, YEAR + RUNTIME come from the entry's AniList node;
    # only the content rating (TV-14) stays series-level from TMDB. Falls back to
    # the TMDB value per-field when the node didn't carry it.
    if anilist_id is not None:
        meta_bits = [b for b in (entry_year or _t("year"),
                                 _t("certification"),
                                 entry_runtime or _t("runtime")) if b]

    return {
        "native_title": native_title,
        "romaji_title": romaji_title,
        "synopsis": synopsis,
        "meta_label": " | ".join(meta_bits),
        "language": language,
        "genres": genres,
        "studio": studio,
        "tmdb_rating": tmdb_rating,
        "anilist_score": anilist_score,
        "country": country,
    }


def render_fields(fields: dict) -> dict:
    """Keep a persisted metadata dict compatible with ``render_thumbnail``.

    ``ThumbnailSource.fields`` also carries editor/identity keys (``entry_label``,
    ``anilist_id``, ``thumbnail_chat_id`` …) that ``render_thumbnail`` doesn't
    accept; the persisted dict must be filtered to the renderer's signature
    before it can be splatted. Single source of truth for both the admin editor
    and the redo metadata refresh so a re-render can never raise ``TypeError``.
    """
    import inspect

    allowed = set(inspect.signature(ThumbnailRenderService.render_thumbnail).parameters)
    allowed.discard("self")
    return {key: value for key, value in fields.items() if key in allowed}


async def persist_thumbnail_source(
    container: Any,
    anime_doc_id: str | None,
    anilist_id: int | None,
    fields: dict,
    *,
    image_path: str | Path | None = None,
) -> None:
    """Save the inputs behind a rendered thumbnail for later edits/recovery."""
    if not anime_doc_id or getattr(container, "pg_sessionmaker", None) is None:
        return
    source_fields = dict(fields)
    for key in ("genres",):
        if isinstance(source_fields.get(key), tuple):
            source_fields[key] = list(source_fields[key])
    # Use a non-null sentinel for mapping-only/root thumbnails. This makes the
    # database key truly unique on PostgreSQL and lets retries safely converge.
    entry_id = int(anilist_id) if anilist_id is not None else -1
    values = {
        "anime_doc_id": anime_doc_id,
        "anilist_id": entry_id,
        "fields": source_fields,
    }
    if image_path:
        values["image_path"] = str(image_path)
    async with session_scope(container.pg_sessionmaker) as session:
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            insert = None

        if insert is not None:
            stmt = insert(ThumbnailSource).values(**values)
            update_values = {
                "fields": stmt.excluded.fields,
                "image_path": (
                    stmt.excluded.image_path
                    if image_path else ThumbnailSource.image_path
                ),
            }
            if dialect == "postgresql":
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_thumbnail_source_entry",
                    set_=update_values,
                )
            else:
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        ThumbnailSource.anime_doc_id,
                        ThumbnailSource.anilist_id,
                    ],
                    set_=update_values,
                )
            await session.execute(stmt)
        else:
            # Keep non-Postgres/non-SQLite development backends usable. The
            # production paths above are atomic; this fallback is only for a
            # backend without an ON CONFLICT dialect extension.
            row = (await session.execute(
                select(ThumbnailSource).where(
                    ThumbnailSource.anime_doc_id == anime_doc_id,
                    ThumbnailSource.anilist_id == entry_id,
                )
            )).scalars().first()
            if row is None:
                session.add(ThumbnailSource(**values))
            else:
                row.fields = source_fields
                if image_path:
                    row.image_path = str(image_path)


class ThumbnailRenderService:
    """Renders the HTML template to a thumbnail image using Playwright."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._init_done = False

    async def _ensure_browser(self) -> Any:
        if self._init_done and self._browser:
            return self._browser
        try:
            import playwright.async_api as pw
            self._playwright = await pw.async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-dev-shm-usage", "--disable-gpu"],
            )
            self._init_done = True
            log.info("thumbnail.playwright.started")
            return self._browser
        except ImportError:
            log.warning("thumbnail.playwright.not_installed")
            raise
        except Exception as exc:
            log.warning("thumbnail.playwright.launch_failed", error=str(exc))
            raise

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._init_done = False
        log.info("thumbnail.playwright.stopped")

    async def render_thumbnail(
        self,
        *,
        title: str,
        native_title: str = "",
        romaji_title: str = "",
        synopsis: str = "",
        logo_url: str | None = None,
        poster_url: str | None = None,
        bg_url: str | None = None,
        meta_label: str = "",
        language: str = "",
        genres: list[str] | None = None,
        studio: str = "",
        tmdb_rating: float | str | None = None,
        anilist_score: int | float | None = None,
        country: str | None = None,
        flag_url: str | None = None,
        output_dir: str | Path | None = None,
        # Back-compat: older callers passed ``entry_label`` for the meta bar.
        entry_label: str = "",
        # Disambiguates the on-disk asset dir + output file so two entries that
        # share a title (e.g. a cour split — "Vanitas" S1P1 and S1P2 both carry
        # the franchise title) don't render into the SAME ``assets_<title>``
        # folder and clobber each other's logo/poster/webp. Pass a per-entry
        # value (anilist id, or "<season>_<part>"); falls back to the title.
        variant_key: str | int | None = None,
    ) -> Path | None:
        """Render a thumbnail image from the tokenized HTML template.

        Every field maps to a ``{{TOKEN}}`` in ``thumbnail/index.html``. Missing
        fields degrade gracefully (blank text, bundled fallback art) so the card
        never renders with broken placeholders.

        Returns:
            Path to the generated WebP image, or None on failure.
        """
        meta_label = meta_label or entry_label
        genres = genres or []

        work_dir = Path(output_dir or _OUTPUT_DIR)
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:40]
        # Suffix a per-entry token so same-titled entries (cour splits) get their
        # own asset dir + output file instead of overwriting one another.
        if variant_key not in (None, ""):
            vk = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in str(variant_key)
            )[:24]
            safe_name = f"{safe_name}_{vk}" if safe_name else vk
        images_dir = work_dir / f"assets_{safe_name}"
        images_dir.mkdir(parents=True, exist_ok=True)

        # ── Images — download real art; fall back to bundled assets so the card
        # is never a broken box. The HTML lives in images_dir, so a fallback must
        # be copied next to it for the relative ref to resolve. ──
        import shutil

        def _with_default(local: Path | None, asset: str) -> str:
            if local is not None:
                return local.name
            src = _TEMPLATE_DIR / asset
            if src.exists():
                shutil.copyfile(src, images_dir / asset)
            return asset

        bg_local = await _download_image(bg_url, images_dir / "background.webp") if bg_url else None
        logo_local = await _download_image(logo_url, images_dir / "logo.png") if logo_url else None
        poster_local = await _download_image(poster_url, images_dir / "poster.webp") if poster_url else None
        flag_local = await _download_image(flag_url or _flag_url(country),
                                           images_dir / "flag.png")

        bg_path = _with_default(bg_local, "background.webp")
        logo_path = _with_default(logo_local, "logo.png")
        poster_path = _with_default(poster_local, "poster.webp")
        flag_path = flag_local.name if flag_local else ""

        # ── Score ring maths (AniList 0-100). Blank score → full-neutral ring. ──
        try:
            score = float(anilist_score) if anilist_score is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))
        dashoffset = round(_RING_CIRCUMFERENCE * (1 - score / 100), 2)
        # The template already prints a literal "%" after {{ANILIST_SCORE}}, so the
        # value here must be the bare number — otherwise it renders "86%%".
        score_text = f"{int(round(score))}" if score else "—"

        rating_text = ""
        if tmdb_rating not in (None, "", 0):
            rating_text = str(tmdb_rating)

        genre_html = "\n".join(_genre_pill(g) for g in _fit_genres(genres))

        # ── Token substitution ──
        esc = html_module.escape
        tokens = {
            "{{BRAND_NAME}}": esc("Anime Weebs"),
            "{{TITLE}}": esc(_truncate(title, 40)),
            "{{NATIVE_TITLE}}": esc(native_title or title),
            "{{ROMAJI_TITLE}}": esc(romaji_title or title),
            "{{META_LABEL}}": esc(meta_label),
            "{{LANGUAGE}}": esc(language),
            "{{SYNOPSIS}}": esc(_truncate(synopsis, _SYNOPSIS_MAX_CHARS)),
            "{{STUDIO}}": esc(studio or "—"),
            "{{TMDB_RATING}}": esc(rating_text),
            "{{ANILIST_SCORE}}": score_text,
            "{{ANILIST_DASHOFFSET}}": str(dashoffset),
            "{{GENRE_PILLS}}": genre_html,
            "{{BG_IMAGE}}": bg_path,
            "{{LOGO_IMAGE}}": logo_path,
            "{{POSTER_IMAGE}}": poster_path,
            "{{FLAG_IMAGE}}": flag_path,
        }
        # Owner-tunable shadow/size styling (Senku settings → thumbnail_style),
        # resolved live so an edit shows on the next render.
        tokens.update(_style_tokens())
        html = _load_template()
        for token, value in tokens.items():
            html = html.replace(token, value)

        output_html = images_dir / "thumbnail.html"
        output_html.write_text(html, encoding="utf-8")

        # ── Render with Playwright at the template's native aspect ratio ──
        browser = await self._ensure_browser()
        context = page = None
        try:
            context = await browser.new_context(
                viewport={"width": _THUMBNAIL_WIDTH, "height": _THUMBNAIL_HEIGHT},
                device_scale_factor=_THUMBNAIL_SCALE,
            )
            page = await context.new_page()
            file_url = output_html.absolute().as_uri()
            # domcontentloaded is instant; the real gate is Tailwind's browser
            # JIT + webfonts finishing. networkidle can hang on the CDN, so we
            # wait explicitly for both instead.
            await page.goto(file_url, wait_until="domcontentloaded")
            await self._await_render_ready(page)
            output_path = work_dir / f"thumb_{safe_name}.webp"
            # Playwright's screenshot only emits png|jpeg — grab a lossless PNG
            # and transcode to the .webp the rest of the pipeline expects.
            png_bytes = await page.screenshot(type="png", full_page=False)
            # WEBP method=6 on a 4098×1923 image is CPU-heavy (hundreds of ms →
            # seconds). Do it in a worker thread so it never blocks the shared
            # event loop and starve Pyrogram's ping → session drop.
            await asyncio.to_thread(_encode_png_to_webp, png_bytes, output_path)
            log.info("thumbnail.rendered", path=str(output_path), title=title)
            return output_path
        except Exception as exc:
            log.warning("thumbnail.render.failed", title=title, error=str(exc))
            return None
        finally:
            if page:
                await page.close()
            if context:
                await context.close()

    async def _await_render_ready(self, page: Any) -> None:
        """Wait until Tailwind's browser build has applied and webfonts loaded,
        so the screenshot never catches a half-styled frame."""
        try:
            # Tailwind browser CDN injects styles asynchronously; wait for the
            # body to actually pick up its background color as a proxy for "CSS
            # applied", then for the font set to be ready.
            await page.wait_for_function(
                "() => document.fonts && document.fonts.status === 'loaded'",
                timeout=8000,
            )
        except Exception:
            # Fonts API can stall on some hosts — fall back to a fixed settle.
            pass
        await page.wait_for_timeout(1200)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
