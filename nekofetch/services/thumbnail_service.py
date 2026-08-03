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

import html as html_module
import math
from pathlib import Path
from typing import Any

import httpx

from nekofetch.core.logging import get_logger

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
# 1366×641). device_scale_factor=2 doubles the output resolution for crisp text.
_THUMBNAIL_WIDTH = 1366
_THUMBNAIL_HEIGHT = 641
_SYNOPSIS_MAX_CHARS = 300

# The SVG score ring: r=42 → circumference = 2·π·42 ≈ 263.89. dashoffset is the
# UNfilled remainder, so offset = C · (1 - pct/100).
_RING_CIRCUMFERENCE = 2 * math.pi * 42

# How many genre pills fit before the row visually overflows the design width.
# We budget by rendered character count rather than a fixed count, since "Slice
# of Life" eats far more room than "Action".
_GENRE_CHAR_BUDGET = 42
_GENRE_MAX_PILLS = 5


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
                                  anime_doc_id: str | None = None) -> dict:
    """Enrich a title into the display fields ``render_thumbnail`` consumes.

    Pulls facts from TMDB (overview, native title, studio, rating, country,
    genres, year/cert/runtime meta line) and AniList (romaji/native title, genres,
    studio, 0–100 score) and derives the audio-language label from the title's
    enabled ``StoragePack`` rows — the same lookup ``bot_factory`` uses. Every
    provider call is best-effort: a miss degrades one field, never the whole set.

    The user-picked logo/poster/backdrop are NOT set here (the caller owns those);
    this fills everything else. Returns a kwargs dict ready to splat into
    ``render_thumbnail`` alongside the chosen art.
    """
    synopsis = ""
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
        synopsis = _t("overview") or synopsis
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
    if anime_doc_id:
        try:
            from nekofetch.services.metadata_prefetch import load_cached

            ablob = await load_cached(container, anime_doc_id, "anilist",
                                      anime_doc_id=anime_doc_id)
            if ablob:
                anilist_cached = ablob.get("search") or None
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
        synopsis = synopsis or (_a("synopsis") or "")

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
            from nekofetch.services.bot_naming import language_label
            from nekofetch.domain.enums import AudioType
            from nekofetch.infrastructure.database.postgres.models import StoragePack
            from nekofetch.infrastructure.database.postgres.session import session_scope
            from sqlalchemy import select

            _AUDIO_LANGS = {
                AudioType.SUBBED.value: {"japanese"},
                AudioType.DUBBED.value: {"english"},
                AudioType.DUAL_AUDIO.value: {"japanese", "english"},
                AudioType.MULTI.value: {"japanese", "english", "hindi"},
            }
            async with session_scope(container.pg_sessionmaker) as session:
                packs = (await session.execute(
                    select(StoragePack).where(
                        StoragePack.anime_doc_id == anime_doc_id,
                        StoragePack.enabled.is_(True),
                    )
                )).scalars().all()
                audios = {p.audio.value for p in packs if p.audio is not None}
            langs: set = set()
            for a in audios:
                langs |= _AUDIO_LANGS.get(a, set())
            if langs:
                language = language_label(langs)
        except Exception as exc:  # noqa: BLE001
            log.debug("thumbfields.language.failed", error=str(exc))

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
                device_scale_factor=2,
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
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(png_bytes)) as im:
                im.save(output_path, format="WEBP", quality=90, method=6)
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
