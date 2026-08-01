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

import re

from nekofetch.domain.enums import AudioType

_BOT_NAME_LIMIT = 64


# Non-Latin script ranges (mirrors nekofetch.sources._normalize._SCRIPTS). A title
# containing ANY of these is a foreign-script synonym (Japanese/Korean/Thai/…)
# and must never be chosen for a caption or filename — English script only.
_NON_LATIN = re.compile(
    "["
    "぀-ヿ"      # hiragana / katakana
    "가-힣"      # hangul
    "ऀ-ॿ"      # devanagari
    "؀-ۿ"      # arabic
    "֐-׿"      # hebrew
    "฀-๿"      # thai
    "Ѐ-ӿ"      # cyrillic
    "一-鿿"      # CJK ideographs
    "]"
)


def is_latin_script(s: str | None) -> bool:
    """True when ``s`` is a usable English/Latin-script title.

    Rejects any string carrying a non-Latin script character (the Filipino/Thai/
    Korean/Japanese synonyms AniList mixes in). Empty/whitespace is not usable.
    Latin-1 accents (é, ñ, â…) are fine — those are still English-readable names.
    """
    t = (s or "").strip()
    if not t:
        return False
    return _NON_LATIN.search(t) is None


def latin_only(titles) -> list[str]:
    """Filter an iterable of titles to English/Latin-script ones (order-preserving)."""
    return [t for t in (titles or []) if is_latin_script(t)]


def root_titles(anilist_blob: dict | None, fallback_title: str = "") -> dict:
    """Base-series titles from a prefetched ``anilist.json`` blob.

    Names must derive from the franchise ROOT, not the operator-confirmed
    installment (a sequel like "Kisekoi 2" would otherwise leak into filenames /
    captions). The franchise walk tags the seed entry ``relation == "ROOT"``; we
    read its ``english_title`` and ``titles`` ([english, romaji, native]).

    Returns ``{"english", "romaji", "titles": [...]}`` — falling back to the
    search blob, then ``fallback_title``, when no ROOT entry is cached. All
    returned strings are guaranteed Latin-script (native is dropped if not).
    """
    blob = anilist_blob or {}
    search = blob.get("search") or {}
    walk = blob.get("franchise") or {}

    english = romaji = ""
    titles: list[str] = []

    # 1. Prefer the ROOT franchise entry.
    entries = walk.values() if isinstance(walk, dict) else (walk or [])
    root = next((e for e in entries
                 if isinstance(e, dict) and (e.get("relation") == "ROOT")), None)
    if root:
        rt = root.get("titles") or []
        english = root.get("english_title") or (rt[0] if len(rt) > 0 else "")
        romaji = rt[1] if len(rt) > 1 else ""
        titles = list(rt)

    # 2. Fall back to the confirmed-media search blob.
    if not english:
        english = search.get("english") or ""
    if not romaji:
        romaji = search.get("romaji") or ""
    if not titles:
        titles = list(search.get("titles") or [])

    # 3. Last resort.
    if not english and fallback_title:
        english = fallback_title

    return {
        "english": english if is_latin_script(english) else "",
        "romaji": romaji if is_latin_script(romaji) else "",
        "titles": latin_only(titles),
    }


def audio_tag(audios: set) -> str:
    """The audio-type label for a set of available audio tracks.

    Distinguishes genuine Dual Audio (both languages in one file) from
    separate Sub + Dub files — "Dual" only appears when a DUAL_AUDIO
    file exists.

    User spec for channel titles:
    - dual audio → "Dual Audio, Sub & Dub"
    - separate sub+dub (not dual) → "Sub & Dub"
    - multi audio → "Multi Audio, Sub, Dub & Dual"
    - dub only → "Dub"
    - sub only → "Sub"
    """
    vals = {a.value if isinstance(a, AudioType) else str(a) for a in audios}
    has_dual = AudioType.DUAL_AUDIO.value in vals
    has_sub = AudioType.SUBBED.value in vals
    has_dub = AudioType.DUBBED.value in vals
    multi = AudioType.MULTI.value in vals

    if multi:
        return "Multi Audio, Sub, Dub & Dual"
    if has_dual:
        return "Dual Audio, Sub & Dub"
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
    raw = {l.strip().lower() for l in (languages or set()) if l and l.strip()}
    if not raw:
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
    # Priority ordering: English first, then Japanese, then the rest
    # alphabetically — yields "English & Japanese" and "English, Japanese &
    # Hindi" (the reading order the user expects), never alphabetical drift
    # like "English, Hindi & Japanese".
    _priority = {"english": 0, "japanese": 1}
    langs = sorted(raw, key=lambda l: (_priority.get(names.get(l, l).lower(), 2),
                                       names.get(l, l.title())))
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
    show the English name, the native (Japanese) name, then the same
    audio/languages/qualities suffix. User spec:

        "English〢Romaji《 audio 》« languages » resolutions"

    Rules:
    - If English == Romaji/Japanese (same text), keep ONE name only.
    - Audio labels: "Dual Audio, Sub & Dub" / "Sub & Dub" / "Multi Audio, Sub, Dub & Dual".
    - Languages: "English & Japanese" or "English, Japanese & Hindi" (Oxford comma before last).
    - Omit ANY section whose info is unavailable (no languages, no resolutions, etc).
    - Native half is dropped first when space is tight; the audio/quality suffix
      is always preserved (it's what users scan for).

    Example: "Takopi's Original Sin〢Takopii no Genzai《 Dual Audio, Sub & Dub 》« English & Japanese » 1080p 720p 480p"
    """
    english = (english or "").strip()
    native = (native or "").strip()
    title = english or native or "Anime"

    tag = audio_tag(audios or set())
    langs = language_label(languages)
    quals = " ".join(qualities) if qualities else ""

    # The audio/language brackets abut each other and the title with NO spaces
    # ("…Genzai《 Dual Audio 》« English & Japanese »"); only the resolutions get a
    # leading space. Each bracket keeps interior spaces ("《 x 》", "« x »").
    def _build_suffix() -> str:
        s = ""
        if tag:
            s += f"《 {tag} 》"
        if langs:
            s += f"« {langs} »"
        if quals:
            s += (" " if s else "") + quals
        return s

    suffix = _build_suffix()

    # If English and native are identical, keep only one (avoid "Same〢Same").
    # Otherwise build "English〢Native" with the separator 〢.
    if native and native != english:
        head = f"{english}〢{native}"
    else:
        head = english or native or "Anime"

    # Try full "head+suffix", then drop native, then truncate.
    candidate = f"{head}{suffix}" if suffix else head
    if len(candidate) <= limit:
        return candidate

    # Fallback: drop native (if any), keep English + suffix.
    candidate = f"{english}{suffix}" if suffix else english
    if len(candidate) <= limit:
        return candidate

    # Last resort: preserve the suffix, truncate the head.
    if suffix:
        room = limit - len(suffix)
        if room > 3:
            return f"{title[:room - 1].rstrip()}…{suffix}"[:limit]
    return title[:limit]


# ── storage-channel pack caption ─────────────────────────────────────────────
# The caption posted before a pack's files, two bold lines:
#
#     ➠ TAKOPI'S ORIGINAL SIN : SEASON 1
#     ➠ 480p [DUAL ∽ ENG + JPN]
#
# Telegram wraps the title line onto a second row past ~38 characters, so the
# builder keeps line 1 within that budget by shortening — in order — the season
# label (SEASON 1 → S1), then the title itself (full → shortest synonym →
# acronym). The title is kept as full as possible; the acronym is a last resort.
_CAPTION_ARROW = "➠"          # U+27A0 heavy round-tipped rightwards arrow
_CAPTION_SWUNG = "∽"          # U+223D reversed tilde, the audio∽languages joiner
_CAPTION_LINE_LIMIT = 38      # chars before a mobile client breaks the line in two

# Audio-line by track kind: (TAG, language spread). These mirror the operator's
# canonical variants exactly — DUAL is always ENG+JPN, MULTI adds HIN, SUB is the
# Japanese track with English subs, DUB is the English track.
_AUDIO_CAPTION = {
    AudioType.DUAL_AUDIO: ("DUAL", "ENG + JPN"),
    AudioType.MULTI:      ("MULTI", "ENG + JPN + HIN"),
    AudioType.SUBBED:     ("SUB", "JPN + EngSubs"),
    AudioType.DUBBED:     ("DUB", "English"),
}


def _acronym(title: str) -> str:
    """Initialism from a title's words: 'Takopi's Original Sin' → 'TOS'.

    Skips articles/particles so a long title still yields a tight acronym, and
    keeps a leading digit ('86' → '86', '5 Centimeters' → '5C')."""
    stop = {"the", "a", "an", "of", "no", "to", "and", "&", "wa", "ga", "wo"}
    words = re.findall(r"[0-9A-Za-z']+", title)
    letters = [w[0] for w in words if w and w.lower() not in stop]
    return "".join(letters).upper()


def _season_tokens(season, season_part, content_type):
    """(long, short) season labels for the caption, e.g. ('SEASON 1', 'S1').

    Non-season packs (Movie / OVA / ONA / Special) have no number — the content
    type itself is the label and both forms collapse to it (e.g. 'MOVIE')."""
    ct = (content_type or "Season").strip()
    if season is None or ct.lower() != "season":
        lbl = ct.upper()
        return lbl, lbl
    if season_part:
        return f"SEASON {season} PART {season_part}", f"S{season}P{season_part}"
    return f"SEASON {season}", f"S{season}"


def _shortest_alt(full_upper: str, alt_titles) -> str:
    """The shortest English-script alternative title strictly shorter than the
    full title.

    Only Latin-script names are eligible (no Filipino/Thai/Korean synonyms); among
    those we take the shortest usable one (the operator's 'shortest one' rule).
    Returns '' when no alt title actually helps."""
    best = ""
    for raw in alt_titles or []:
        if not is_latin_script(raw):
            continue
        alt = re.sub(r"\s+", " ", (raw or "").strip()).upper()
        if not alt or alt == full_upper:
            continue
        if len(alt) >= len(full_upper):
            continue
        if not best or len(alt) < len(best):
            best = alt
    return best


def build_pack_caption(
    title: str, *, season, season_part, resolution: str, audio,
    content_type: str = "Season", alt_titles=None,
    line_limit: int = _CAPTION_LINE_LIMIT, arrow: str = _CAPTION_ARROW,
) -> str:
    """Two-line bold pack caption fit to ``line_limit`` characters on line 1.

    Line 1 : ``➠ {TITLE} : {SEASON}``  (uppercase, shortened to fit)
    Line 2 : ``➠ {resolution} [{AUDIO} ∽ {languages}]``

    Shortening ladder for line 1 (first that fits wins):
        full+SEASON → full+S# → synonym+SEASON → synonym+S# → acronym+SEASON →
        acronym+S#. Falls through to acronym+S# even if still over budget (only a
        pathologically long acronym, which can't be shortened further)."""
    full = re.sub(r"\s+", " ", (title or "Anime").strip()).upper()
    long_season, short_season = _season_tokens(season, season_part, content_type)
    syn = _shortest_alt(full, alt_titles)
    acr = _acronym(full)

    # Title candidates in preference order; skip a synonym/acronym that doesn't
    # actually shorten anything (or an acronym under 2 letters — useless).
    heads: list[str] = [full]
    if syn:
        heads.append(syn)
    if acr and len(acr) >= 2 and acr != full:
        heads.append(acr)

    prefix = f"{arrow} "
    fixed = len(prefix) + len(" : ")

    def _line(head: str, season_label: str) -> str:
        return f"{prefix}{head} : {season_label}"

    chosen = _line(acr or full, short_season)   # last-resort default
    for head in heads:
        for season_label in (long_season, short_season):
            if fixed + len(head) + len(season_label) <= line_limit:
                chosen = _line(head, season_label)
                break
        else:
            continue
        break

    tag, langs = _AUDIO_CAPTION.get(
        audio if isinstance(audio, AudioType) else None, ("", ""))
    if tag:
        line2 = f"{arrow} {resolution} [{tag} {_CAPTION_SWUNG} {langs}]"
    else:
        line2 = f"{arrow} {resolution}"

    return f"<b>{chosen}</b>\n<b>{line2}</b>"
