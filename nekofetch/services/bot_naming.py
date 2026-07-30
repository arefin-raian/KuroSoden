"""Distribution-bot display-name formatting.

A per-title bot's name encodes, at a glance, what it carries:

    "<Title>『 <audio type> 』« <languages> » <qualities>"

The audio type is derived from which audio tracks actually exist:

    * dual audio (one file, both tracks) → "Dual Audio"
    * separate sub + dub files           → "Sub & Dub"
    * dub only                           → "Dub"
    * sub only                           → "Sub"

Telegram caps a bot name at 64 chars, so the title half is truncated to fit while
the tag is always preserved (the tag is the part users scan for).
"""

from __future__ import annotations

from nekofetch.domain.enums import AudioType

_BOT_NAME_LIMIT = 64


def audio_tag(audios: set) -> str:
    """The audio-type label for a set of available audio tracks.

    Distinguishes genuine Dual Audio (both languages in one file) from
    separate Sub + Dub files — "Dual" only appears when a DUAL_AUDIO
    file exists."""
    vals = {a.value if isinstance(a, AudioType) else str(a) for a in audios}
    has_dual = AudioType.DUAL_AUDIO.value in vals
    has_sub = AudioType.SUBBED.value in vals
    has_dub = AudioType.DUBBED.value in vals
    multi = AudioType.MULTI.value in vals

    if multi:
        return "Multi Audio"
    if has_dual:
        return "Dual Audio"
    if has_sub and has_dub:
        return "Sub & Dub"
    if has_dub:
        return "Dub"
    if has_sub:
        return "Sub"
    return ""


def language_label(languages: set | None) -> str:
    """Human-readable language list: 'Japanese & English', 'Japanese', etc.

    Recognises BOTH the canonical full names (``"english"``, ``"japanese"``)
    AND the 2-letter ISO codes (``"en"``, ``"ja"``) so callers using either
    form get the same canonical word on the bot name AND inside the season
    card. Without both keys, one surface would render ``"English & Japanese"``
    and the other ``"En & Ja"`` — the exact drift the alignment pass set
    out to eliminate.
    """
    langs = sorted({l.strip().lower() for l in (languages or set()) if l and l.strip()})
    if not langs:
        return ""
    names = {
        # Full names — canonical, fed in by bot_factory._gather.
        "japanese": "Japanese", "english": "English", "hindi": "Hindi",
        "korean":  "Korean",  "chinese": "Chinese", "spanish": "Spanish",
        # 2-letter ISO codes — also recognised; feeding "en" / "ja" / "hi"
        # otherwise produces "En, Hi & Ja".
        "ja": "Japanese", "en": "English", "hi": "Hindi",
        "ko": "Korean", "zh": "Chinese", "es": "Spanish",
    }
    labelled = [names.get(l, l.title()) for l in langs]
    if len(labelled) == 1:
        return labelled[0]
    return " & ".join([", ".join(labelled[:-1]), labelled[-1]])


def format_bot_name(
    english: str | None, romaji: str | None, *,
    audios: set, languages: set | None = None,
    qualities: list[str] | None = None, limit: int = _BOT_NAME_LIMIT,
) -> str:
    """Build the bot's display name:
    '<Title>『 <audio> 』« <languages> » <qualities>', fit to ``limit``."""
    english = (english or "").strip()
    romaji = (romaji or "").strip()
    title = english or romaji or "Anime"

    tag = audio_tag(audios)
    langs = language_label(languages)
    quals = " ".join(qualities) if qualities else ""

    suffix_parts = []
    if tag:
        suffix_parts.append(f"『 {tag} 』")
    if langs:
        suffix_parts.append(f"« {langs} »")
    if quals:
        suffix_parts.append(quals)
    suffix = " ".join(suffix_parts)

    if not suffix:
        return title[:limit]

    # Preserve the suffix; truncate the title half to fit the 64-char limit.
    room = limit - len(suffix) - 1  # -1 for the space between title and suffix
    if len(title) > room and room > 3:
        title = title[: max(0, room - 1)].rstrip() + "…"
    return f"{title} {suffix}"[:limit]


def format_bot_username(
    base: str, anime_doc_id: str, *,
    suffix: str | None = None,
    is_channel: bool = False,
) -> str:
    """A valid, reasonably-unique bot/channel username candidate (5–32 chars).

    Telegram requires bot usernames to end in 'bot'; channel usernames do not.
    Set ``is_channel=True`` to drop the 'bot' suffix (channels use e.g. ``_axw``).
    When ``suffix`` is None, the default from ``BotConfig`` is used.
    """
    import re

    if suffix is None:
        from nekofetch.core.config import get_app_config
        cfg = get_app_config().bot
        suffix = cfg.channel_username_suffix if is_channel else cfg.bot_username_suffix

    slug = re.sub(r"[^a-z0-9]+", "_", (base or "anime").lower()).strip("_")
    # leave room for the "_<suffix>" tail (and "_bot" if applicable) within 32 chars
    tail = f"_{suffix}"
    if not is_channel:
        tail += "_bot"
    slug = slug[: max(1, 32 - len(tail))].strip("_") or "anime"
    name = f"{slug}{tail}"
    return name[:32]


def format_channel_username_candidates(
    *, english: str | None = None, romaji: str | None = None,
    synonyms: list[str] | None = None, suffix: str | None = None,
) -> list[str]:
    """Several valid @username candidates (5–32 chars) for a channel.

    Telegram won't let you claim an arbitrary exact handle (length + uniqueness),
    so Senku offers a short menu built from the title's own names — English,
    Romaji, then any synonyms — each slugified with the ``_<suffix>`` (``_axw``)
    tail and de-duplicated in priority order. The admin picks whichever is free.
    """
    import re

    if suffix is None:
        from nekofetch.core.config import get_app_config
        suffix = get_app_config().bot.channel_username_suffix

    tail = f"_{suffix}"
    seen: set[str] = set()
    out: list[str] = []
    sources = [english, romaji, *(synonyms or [])]
    for src in sources:
        base = (src or "").strip()
        if not base:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
        slug = slug[: max(1, 32 - len(tail))].strip("_")
        if not slug:
            continue
        cand = f"{slug}{tail}"[:32]
        # Telegram floor is 5 chars; skip anything that came out too short.
        if len(cand) < 5 or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def format_channel_title(
    english: str | None, native: str | None, *,
    audios: set | None = None, languages: set | None = None,
    qualities: list[str] | None = None, limit: int = 128,
) -> str:
    """Channel title with BOTH names + the decorative audio/language/quality tags.

    Distribution *channels* allow a 128-char title (vs a bot's 64), so we can
    show the English name, the native (Japanese) name in guillemets, then the
    same ``『 audio 』« languages » qualities`` suffix used for bot names:

        "<English> «<Native>» 『 Dual Audio 』« Japanese & English » 1080p 720p 480p"

    The native half is dropped first when space is tight; the audio/quality
    suffix is always preserved (it's what users scan for).
    """
    english = (english or "").strip()
    native = (native or "").strip()
    title = english or native or "Anime"

    tag = audio_tag(audios or set())
    langs = language_label(languages)
    quals = " ".join(qualities) if qualities else ""

    suffix_parts = []
    if tag:
        suffix_parts.append(f"『 {tag} 』")
    if langs:
        suffix_parts.append(f"« {langs} »")
    if quals:
        suffix_parts.append(quals)
    suffix = " ".join(suffix_parts)

    # Try full "English «Native» suffix", then drop native, then just English.
    for head in ([f"{english} «{native}»" if native and native != english else english,
                  english] if english else [native]):
        candidate = f"{head} {suffix}".strip() if suffix else head
        if len(candidate) <= limit:
            return candidate
    # Last resort: preserve the suffix, truncate the head.
    if suffix:
        room = limit - len(suffix) - 1
        if room > 3:
            return f"{title[:room - 1].rstrip()}… {suffix}"[:limit]
    return title[:limit]
