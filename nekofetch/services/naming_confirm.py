"""Interactive filename + caption confirm gate for the Levi download worker.

The download worker is headless — it drives everything off Redis flags. To let an
admin review (and optionally edit) the file-naming template and the storage-pack
caption BEFORE they're applied, we reuse the proven channel-reply pattern:

    worker  →  posts a confirm card to the admin's chat (the same chat the live
               progress card lives in), arms a chat-scoped reply marker, sets an
               "awaiting" Redis flag, then BLOCK-POLLS that flag (with a timeout)
    admin   →  taps "Use it" (accept the computed default) or "Edit" (copy the
               shown example, change it, send it back)
    handler →  (Levi side, group=12) writes the admin's value to a Redis key,
               flips the awaiting flag, disarms the marker, edits the card
    worker  →  wakes, reads the value (or falls back to the default on timeout),
               resumes

Two gates, fired once per job (guarded by a ``:*_confirmed`` marker so the
per-chunk/per-tier processing loop never re-prompts):

* **filename** — before RenameStage. The admin edits an example (episode-1)
  filename; we infer the naming from their edit (no template variables).
* **caption**  — before the storage upload. The admin edits the two-line pack
  caption; it's threaded through as ``caption_override``.

Everything here is best-effort: if Redis or the Levi client is unavailable, or the
admin never answers within the timeout, the computed default is used and the job
proceeds — an interactive nicety must never wedge a download.
"""

from __future__ import annotations

import asyncio
import re

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import (
    safe_redis_delete,
    safe_redis_get,
    safe_redis_set,
)

log = get_logger(__name__)

# How long the worker waits for the admin before proceeding on the default.
_CONFIRM_TIMEOUT_S = 600          # 10 minutes
_POLL_INTERVAL_S = 3.0

# Redis key shapes (job-scoped, mirroring nf:job:{id}:* control flags).
_AWAIT_KEY = "nf:job:{job_id}:await_{kind}"        # "1" while worker is blocked
_VALUE_KEY = "nf:job:{job_id}:{kind}_value"        # admin's edited text (or "__use__")
_DONE_KEY = "nf:job:{job_id}:{kind}_confirmed"     # once-per-job guard

_USE_DEFAULT = "__use__"          # sentinel the handler writes for the "Use it" tap


def await_key(job_id: int, kind: str) -> str:
    return _AWAIT_KEY.format(job_id=job_id, kind=kind)


def value_key(job_id: int, kind: str) -> str:
    return _VALUE_KEY.format(job_id=job_id, kind=kind)


def done_key(job_id: int, kind: str) -> str:
    return _DONE_KEY.format(job_id=job_id, kind=kind)


# ── filename parsing (inverse of templates.render_filename) ───────────────────

# Resolution / audio tokens appear as bracketed tags in the rendered name, e.g.
# "… S01E01 [1080p] [Dual] @AniXWeebs". We only need to recover the STRUCTURAL
# bits the admin might have changed; the episode number varies per file so it is
# NOT taken from the example — only season/audio/resolution + the title stem.
_SEASON_EP_RE = re.compile(r"S(\d{1,3})\s*E(\d{1,4})", re.IGNORECASE)
_RES_RE = re.compile(r"\b(\d{3,4})p\b", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_AUDIO_WORDS = {
    "sub": "sub", "subbed": "sub",
    "dub": "dub", "dubbed": "dub",
    "dual": "dual", "dual audio": "dual",
    "multi": "multi",
}


def parse_filename_edit(text: str) -> dict:
    """Recover the naming structure from an admin-edited example filename.

    Returns ``{title, season, episode, audio, resolution, template}`` — any field
    that couldn't be parsed is ``None``. ``template`` is the edited name with the
    season/episode/resolution/audio replaced by ``{...}`` tokens so it can be
    re-rendered per file (the operator's "edit the example, no variables" UX: we
    reverse-engineer the variables for them).
    """
    raw = (text or "").strip()
    # Strip an extension if they pasted one.
    stem = re.sub(r"\.(mkv|mp4|avi|mov)$", "", raw, flags=re.IGNORECASE)

    season = episode = None
    m = _SEASON_EP_RE.search(stem)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))

    resolution = None
    mr = _RES_RE.search(stem)
    if mr:
        resolution = f"{mr.group(1)}p"

    audio = None
    for inner in _BRACKET_RE.findall(stem):
        key = inner.strip().lower()
        if key in _AUDIO_WORDS:
            audio = _AUDIO_WORDS[key]
            break

    # Build a re-renderable template: swap the recognised tokens for placeholders.
    template = stem
    if m:
        template = _SEASON_EP_RE.sub("S{season}E{episode}", template, count=1)
    if mr:
        template = _RES_RE.sub("{resolution}", template, count=1)
    # The title stem is everything before the first "S01E01"/"[" marker.
    title = stem
    cut = None
    if m:
        cut = m.start()
    else:
        b = stem.find("[")
        cut = b if b > 0 else None
    if cut:
        title = stem[:cut].strip(" -_·")

    return {
        "title": title or None,
        "season": season,
        "episode": episode,
        "audio": audio,
        "resolution": resolution,
        "template": template,
    }


# ── preview builders (shared by the worker's confirm cards) ───────────────────

def build_example_filename(container, request, *, resolution: str,
                           audio, season: int | None, episode: int = 1) -> str:
    """Render the episode-1 example filename exactly as RenameStage would.

    Mirrors the RenameStage token assembly (same ``_short_title`` + template +
    ``_AUDIO_TAG``) so the confirm card shows the real name, not an approximation.
    """
    from nekofetch.services.branding_service import BrandingService
    from nekofetch.services.processing.stages import _AUDIO_TAG, _short_title
    from nekofetch.ui import templates

    cfg = container.config.rename
    anime_title = request.anime_title
    franchise_data = request.franchise_data or {}
    short = _short_title(anime_title, franchise_data)
    raw_audio = (getattr(audio, "value", audio) or "").lower()
    audio_short = _AUDIO_TAG.get(raw_audio, raw_audio)
    group = BrandingService(container).group
    return templates.render_filename(
        cfg.template,
        title=anime_title, short_title=short,
        season=f"{season or 1:02d}", season_part="",
        episode=f"{episode:02d}", content_type="Season",
        resolution=resolution or "1080p", audio=audio_short,
        source=request.source, group=group,
    )


def build_example_caption(container, request, *, resolution: str,
                          audio, season: int | None, alt_titles=None) -> str:
    """Render the pack caption example (same builder the storage upload uses)."""
    from nekofetch.services.bot_naming import build_pack_caption

    return build_pack_caption(
        request.anime_title, season=season, season_part=None,
        resolution=resolution or "480p", audio=audio, content_type="Season",
        alt_titles=alt_titles or [],
    )


def extract_caption_title(edited: str) -> str:
    """Pull the title out of an edited caption line-1 (``➠ TITLE : SEASON``).

    Returns just the ``TITLE`` portion so it can be re-fed to ``build_pack_caption``
    as the chosen title (line 2, the quality/audio line, is always auto-derived
    per pack and is never taken from the edit). Falls back to the whole first line
    when the arrow/season markers aren't found.
    """
    first = (edited or "").strip().splitlines()[0] if edited.strip() else ""
    # Drop bold tags and the leading arrow.
    first = re.sub(r"</?b>", "", first).strip()
    first = first.lstrip("➠").strip()
    # Drop a trailing " : SEASON …" if present.
    first = re.split(r"\s:\s", first, maxsplit=1)[0].strip()
    return first


def usable_names(container, anilist_blob, request) -> list[str]:
    """The English/Latin-script names the admin can build from: ROOT english,
    romaji, then Latin-only synonyms — for the "Usable names" block on both
    confirm cards."""
    from nekofetch.services.bot_naming import is_latin_script, root_titles

    names: list[str] = []
    seen: set[str] = set()

    def _add(v):
        if v and is_latin_script(v) and v not in seen:
            seen.add(v)
            names.append(v)

    root = root_titles(anilist_blob, fallback_title=request.anime_title)
    _add(root.get("english"))
    _add(root.get("romaji"))
    for v in root.get("titles") or []:
        _add(v)
    fd = request.franchise_data or {}
    for v in (fd.get("synonyms") or []):
        _add(v)
    return names[:8]


# ── the confirm gate (worker side) ────────────────────────────────────────────


class NamingConfirm:
    """Post a confirm card, block until the admin answers, return their choice."""

    def __init__(self, container: Container) -> None:
        self._c = container

    def _levi(self):
        mgr = getattr(self._c, "pipeline_manager", None)
        return getattr(mgr, "levi", None) if mgr else None

    async def already_confirmed(self, job_id: int, kind: str) -> bool:
        return bool(await safe_redis_get(
            self._c.redis, done_key(job_id, kind),
            label="naming_confirm.already"))

    async def _mark_confirmed(self, job_id: int, kind: str) -> None:
        if self._c.redis:
            await safe_redis_set(
                self._c.redis, done_key(job_id, kind), "1",
                label="naming_confirm.mark", ex=6 * 60 * 60)

    async def confirm(
        self, job_id: int, kind: str, *, default_text: str,
        card_text: str, chat_id: int | None, msg_id: int | None = None,
    ) -> str:
        """Show the confirm card and block until the admin answers or the timeout.

        Returns the admin's edited text, or ``default_text`` on Use-it / timeout /
        any infrastructure gap. Fires once per (job, kind); subsequent calls
        short-circuit to the default (already confirmed).

        When ``msg_id`` is given (the live progress-card message), the confirm
        card EVOLVES that message in place — name gate → caption gate → back to
        the live card — so the flow is one message, never three. Only when no
        such message exists do we send a fresh one.
        """
        if await self.already_confirmed(job_id, kind):
            return default_text
        redis = self._c.redis
        levi = self._levi()
        if redis is None or levi is None or chat_id is None:
            # No interactive channel — accept the default silently.
            await self._mark_confirmed(job_id, kind)
            return default_text

        from nekofetch.bots.channel_reply import arm as _arm
        from nekofetch.ui.components import cb

        state = f"levi_confirm_{kind}"
        # Clear any stale value, arm the awaiting flag + reply marker.
        await safe_redis_delete(redis, value_key(job_id, kind),
                                label="naming_confirm.clear_val")
        await safe_redis_set(redis, await_key(job_id, kind), "1",
                             label="naming_confirm.arm", ex=_CONFIRM_TIMEOUT_S + 60)
        await _arm(redis, chat_id, state, job_id=job_id, kind=kind)

        from pyrogram.enums import ParseMode
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Use it", callback_data=cb("levi", "nmuse", job_id, kind)),
            InlineKeyboardButton("✏️ Edit", callback_data=cb("levi", "nmedit", job_id, kind)),
        ]])
        card_id = None
        # Prefer evolving the live progress-card message in place.
        if msg_id is not None:
            try:
                await levi.edit_message_text(
                    chat_id, msg_id, card_text, parse_mode=ParseMode.HTML,
                    reply_markup=kb)
                card_id = msg_id
            except Exception as exc:  # noqa: BLE001 — fall through to send-new
                log.debug("naming_confirm.edit_inplace_failed",
                          job=job_id, kind=kind, error=str(exc))
        if card_id is None:
            try:
                sent = await levi.send_message(
                    chat_id, card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
                card_id = sent.id
            except Exception as exc:  # noqa: BLE001 — can't show card → use default
                log.warning("naming_confirm.card_failed", job=job_id, kind=kind, error=str(exc))
                await self._cleanup(job_id, kind, chat_id)
                await self._mark_confirmed(job_id, kind)
                return default_text
        # Stash the card ref so the handler edits THIS message in place.
        await safe_redis_set(
            redis, f"nf:job:{job_id}:{kind}_card",
            f"{chat_id}:{card_id}", label="naming_confirm.cardref",
            ex=_CONFIRM_TIMEOUT_S + 60)

        # ── block-poll the awaiting flag ──
        loops = int(_CONFIRM_TIMEOUT_S / _POLL_INTERVAL_S)
        result = default_text
        for _ in range(loops):
            await asyncio.sleep(_POLL_INTERVAL_S)
            still = await safe_redis_get(redis, await_key(job_id, kind),
                                         label="naming_confirm.poll")
            if not still:
                val = await safe_redis_get(redis, value_key(job_id, kind),
                                           label="naming_confirm.readval")
                if val and val != _USE_DEFAULT:
                    result = val
                break
        else:
            # Timed out — auto-continue on the default; tidy the card.
            log.info("naming_confirm.timeout", job=job_id, kind=kind)
            try:
                await levi.edit_message_text(
                    chat_id, card_id,
                    card_text + "\n\n<i>No response — using the default.</i>",
                    parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                pass

        await self._cleanup(job_id, kind, chat_id)
        await self._mark_confirmed(job_id, kind)
        return result

    async def _cleanup(self, job_id: int, kind: str, chat_id: int | None) -> None:
        redis = self._c.redis
        if redis is None:
            return
        from nekofetch.bots.channel_reply import disarm as _disarm
        for k in (await_key(job_id, kind), value_key(job_id, kind),
                  f"nf:job:{job_id}:{kind}_card"):
            await safe_redis_delete(redis, k, label="naming_confirm.cleanup")
        if chat_id is not None:
            await _disarm(redis, chat_id)
