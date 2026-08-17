"""Torrent subtitle branding — surgical, style-preserving.

Torrents arrive as a finished MKV whose subtitle tracks are already styled by the
fansub group (dialogue, signs & songs, karaoke). The streaming subtitle path
(:mod:`_subs`) re-styles everything to a single ``Default`` look, which would
DESTROY that work. So for torrents we do the opposite — a surgical injection:

  * parse the existing ``.ass`` verbatim (keep every ``[Script Info]`` line,
    ``[V4+ Styles]`` row, and ``[Events]`` line untouched),
  * add ONE extra style (``AXWBrand``) and a handful of branding ``Dialogue``
    lines placed in the longest subtitle-free gaps,
  * write it back — nothing else changes.

The on-screen cue matches the streaming look: ``Telegram: @AniXWeebs`` (Telegram
blue + white). Track TITLE metadata is handled by the caller (torrents keep the
ORIGINAL title + chrome brackets); this module only touches cue content.

``brand_ass_text`` is pure (string in → string out) so it is fully unit-testable
without ffmpeg. ``brand_subtitle_file`` is the thin file wrapper.
"""

from __future__ import annotations

import re

from nekofetch.core.logging import get_logger
from nekofetch.sources._branding import BRAND_HANDLE, ENCODED_BY_TAG
from nekofetch.sources._subs import (
    BRAND_PREFIX,
    TG_BLUE_ASS,
    WHITE_ASS,
    Cue,
    branding_windows,
)

log = get_logger(__name__)

# A dedicated, unlikely-to-collide style name so we never clash with a fansub's
# own "Brand"/"Sign" styles.
_BRAND_STYLE_NAME = "AXWBrand"

# The brand style's metrics were authored against a 1080-tall script (matching a
# typical modern fansub where a size-72 cue reads at a tasteful ~6.7% of frame
# height). But an ASS font size is expressed in the script's OWN PlayResY units —
# so the same "72" renders at 72/288 ≈ 25% of the frame on a 384×288 script (the
# giant-tag bug seen on Orb/Bisco) and 72/1080 ≈ 6.7% on a 1080 script (Sonny
# Boy, correct). Everything below is therefore scaled by PlayResY/1080 so the
# brand always occupies the SAME fraction of the screen, whatever the script res.
_BRAND_REF_RES_Y = 1080
_BRAND_BASE_FONTSIZE = 72
_BRAND_BASE_OUTLINE = 3.6
_BRAND_BASE_SHADOW = 2
_BRAND_BASE_MARGIN_V = 60
_BRAND_BASE_MARGIN_LR = 50
_BRAND_MIN_FONTSIZE = 8  # never scale below legibility

# Dialogue restyle constants — Noto Sans Bold reference style (OFL-licensed).
# Base values match a readable bold humanist sans at ~6.94% frame height (similar
# visual weight to Sonny Boy's Gandhi Sans Bold but legally bundleable).
_DIALOG_STYLE_NAME = "AXWDialog"
_DIALOG_REF_RES_Y = 1080
_DIALOG_BASE_FONTSIZE = 75
_DIALOG_BASE_OUTLINE = 3.375
_DIALOG_BASE_SHADOW = 0
_DIALOG_BASE_MARGIN_V = 45
_DIALOG_BASE_MARGIN_LR = 225
_DIALOG_FONT_NAME = "Noto Sans"
_DIALOG_BOLD = -1  # ASS Bold flag

# The bundled font faces that back _DIALOG_FONT_NAME. Embedded into the remux so
# the styled dialogue renders identically on a device that doesn't have the font
# installed (otherwise the player substitutes a thinner face). OFL-licensed —
# see resources/fonts/subtitle/OFL.txt.
_DIALOG_FONT_FILES = ("NotoSans-Bold.ttf", "NotoSans-BoldItalic.ttf")
_DIALOG_FONT_MIMETYPE = "application/x-truetype-font"


def _bundled_dialogue_fonts() -> list["Path"]:
    """Absolute paths of the bundled dialogue font faces that exist on disk.

    Resolved relative to the repo root (``resources/fonts/subtitle/``) or the CWD,
    mirroring the ``tools/`` lookup used elsewhere in this package."""
    from pathlib import Path

    out: list[Path] = []
    for base in (Path(__file__).resolve().parents[2], Path.cwd()):
        subdir = base / "resources" / "fonts" / "subtitle"
        if not subdir.is_dir():
            continue
        for name in _DIALOG_FONT_FILES:
            cand = subdir / name
            if cand.exists() and cand not in out:
                out.append(cand)
        if out:
            break
    return out


async def _source_attachments(src) -> tuple[int, set[str]]:
    """``(count, lower-cased filenames)`` of attachments already in ``src``.

    Best-effort via ffprobe; returns ``(0, set())`` when ffprobe is unavailable or
    the probe fails. The count is needed so a newly ``-attach``-ed font's
    ``-metadata:s:t:N`` index lands past the attachments preserved by
    ``-map 0:t?`` (which occupy output attachment streams ``t:0..t:count-1``). The
    filenames let us skip re-attaching a face the source already carries."""
    import asyncio
    import json

    from nekofetch.sources._hls import find_ffprobe

    ffprobe = find_ffprobe()
    if not ffprobe:
        return 0, set()
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "t",
            "-show_entries", "stream_tags=filename", "-of", "json", str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return 0, set()
        data = json.loads(out.decode(errors="replace") or "{}")
    except Exception:  # noqa: BLE001 — dedup is best-effort
        return 0, set()
    streams = data.get("streams", [])
    names: set[str] = set()
    for stream in streams:
        fn = (stream.get("tags") or {}).get("filename")
        if fn:
            names.add(fn.lower())
    return len(streams), names


def _parse_play_res_y(lines: list[str]) -> int | None:
    """Read ``PlayResY`` from an ASS ``[Script Info]`` block (None if absent)."""
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("playresy:"):
            try:
                return int(s.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def _median_style_fontsize(lines: list[str]) -> float | None:
    """Median font size across the file's own ``Style:`` rows (field index 2).

    Used as a fallback size reference when ``PlayResY`` is absent — matching the
    brand to the file's real text size keeps it proportional even when the script
    resolution is unknown."""
    sizes: list[float] = []
    for ln in lines:
        s = ln.strip()
        if not s.lower().startswith("style:"):
            continue
        fields = s.split(":", 1)[1].split(",")
        if len(fields) > 2:
            try:
                sizes.append(float(fields[2].strip()))
            except ValueError:
                continue
    if not sizes:
        return None
    sizes.sort()
    n = len(sizes)
    return sizes[n // 2] if n % 2 else (sizes[n // 2 - 1] + sizes[n // 2]) / 2


def _brand_style_line(lines: list[str]) -> str:
    """Build the ``AXWBrand`` style line scaled to THIS script's resolution.

    Primary signal is ``PlayResY`` (font size is in those units). When it's absent
    we fall back to the file's own median style font size so the brand still tracks
    the real subtitle size instead of ballooning. Outline, shadow, and margins are
    scaled by the same factor so the whole cue stays proportional."""
    play_res_y = _parse_play_res_y(lines)
    if play_res_y and play_res_y > 0:
        fs = round(_BRAND_BASE_FONTSIZE * play_res_y / _BRAND_REF_RES_Y)
    else:
        med = _median_style_fontsize(lines)
        # Our base 72 ≈ a 1080 script's ~75 default, so a file's own median size is
        # already the right target when we can't trust PlayResY.
        fs = round(med) if med else _BRAND_BASE_FONTSIZE
    fs = max(_BRAND_MIN_FONTSIZE, int(fs))
    k = fs / _BRAND_BASE_FONTSIZE
    outline = round(_BRAND_BASE_OUTLINE * k, 2)
    shadow = round(_BRAND_BASE_SHADOW * k, 2)
    margin_v = max(2, round(_BRAND_BASE_MARGIN_V * k))
    margin_lr = max(2, round(_BRAND_BASE_MARGIN_LR * k))
    return (
        f"Style: {_BRAND_STYLE_NAME},Trebuchet MS,{fs},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,{outline},{shadow},2,"
        f"{margin_lr},{margin_lr},{margin_v},1"
    )


def _ms_to_ass(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"


def _ass_time_to_ms(ts: str) -> int:
    """Parse an ASS ``H:MM:SS.CS`` timestamp to milliseconds (0 on garbage)."""
    m = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,2})\s*$", ts)
    if not m:
        return 0
    h, mnt, s, cs = (int(g) for g in m.groups())
    return ((h * 60 + mnt) * 60 + s) * 1000 + cs * 10


def _existing_cues(events_lines: list[str]) -> list[Cue]:
    """Extract (start,end) of every Dialogue line so we can find free gaps.

    Text is irrelevant for gap-finding, so we keep it empty — we only need the
    timing to know where dialogue/signs already occupy the screen.
    """
    cues: list[Cue] = []
    for ln in events_lines:
        if not ln.startswith("Dialogue:"):
            continue
        # Dialogue: Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text
        parts = ln.split(",", 9)
        if len(parts) < 3:
            continue
        start = _ass_time_to_ms(parts[1])
        end = _ass_time_to_ms(parts[2])
        if end > start:
            cues.append(Cue(start, end, ""))
    return cues


def _brand_dialogue_line(start_ms: int, end_ms: int) -> str:
    brand_text = (
        f"{{\\fad(400,400)}}{{\\c{TG_BLUE_ASS}}}{BRAND_PREFIX} "
        f"{{\\c{WHITE_ASS}}}{BRAND_HANDLE}"
    )
    return (
        f"Dialogue: 0,{_ms_to_ass(start_ms)},{_ms_to_ass(end_ms)},"
        f"{_BRAND_STYLE_NAME},,0,0,0,,{brand_text}"
    )


def extract_ass_cues(ass_text: str) -> list[Cue]:
    """Return the dialogue cues (timing only) of an ASS ``[Events]`` section.

    Used to POOL the cues of every subtitle track before choosing branding
    windows, so a gap we pick is silent in *all* tracks — not just the one we
    happen to be branding. Returns ``[]`` for input with no usable ``[Events]``.
    """
    text = ass_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    events_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == "[events]"), -1
    )
    if events_idx == -1:
        return []
    events_body: list[str] = []
    for ln in lines[events_idx + 1:]:
        if ln.strip().startswith("[") and ln.strip().endswith("]"):
            break
        events_body.append(ln)
    return _existing_cues(events_body)


def brand_ass_text(
    ass_text: str,
    video_ms: int | None = None,
    *,
    windows: list[tuple[int, int]] | None = None,
    normalize_dialogue: bool = True,
) -> tuple[str, int]:
    """Inject branding cues into an existing ASS, preserving everything else.

    Returns ``(new_ass_text, brand_count)``. Idempotent: if our ``AXWBrand`` style
    is already present the text is returned unchanged (count 0), so re-processing
    an already-branded file never stacks duplicate cues.

    ``windows`` — pre-computed ``(start_ms, end_ms)`` branding slots. When the
    caller has POOLED the cues of every subtitle track (see
    :func:`brand_torrent_subtitles`) it passes the shared windows here so the same
    silent-in-all-tracks slots are stamped into each track. When ``None`` the
    windows are derived from THIS track's own cues (the standalone/file path).

    ``normalize_dialogue`` — when True (default), plain dialogue lines are
    restyled to use a bold readable font (Noto Sans Bold). Positioned
    signs/songs/OP/ED are never touched. Set False to disable normalization.

    The parse is line-oriented and forgiving — it keeps the three ASS sections in
    order, appends the brand style at the end of ``[V4+ Styles]`` and the brand
    cues at the end of ``[Events]``. Malformed input (no ``[Events]``) is returned
    unchanged rather than raising, so one odd track never sinks the remux.
    """
    # Apply dialogue restyle first (if enabled) so both the brand AND the dialog
    # style are present in the final output.
    if normalize_dialogue:
        ass_text, _restyled = restyle_dialogue(ass_text)

    if _BRAND_STYLE_NAME in ass_text:
        return ass_text, 0

    text = ass_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    # Locate section headers (case-insensitive, tolerate surrounding spaces).
    styles_idx = events_idx = -1
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        if low.startswith("[v4") and low.endswith("styles]"):
            styles_idx = i
        elif low == "[events]":
            events_idx = i
    if events_idx == -1:
        return ass_text, 0  # not a usable ASS — leave it alone

    # Collect existing event lines (everything after [Events] until EOF/next [..]).
    events_body: list[str] = []
    for ln in lines[events_idx + 1:]:
        if ln.strip().startswith("[") and ln.strip().endswith("]"):
            break
        events_body.append(ln)

    if windows is None:
        cues = _existing_cues(events_body)
        windows = branding_windows(cues, video_ms)
    if not windows:
        return ass_text, 0

    brand_lines = [_brand_dialogue_line(s, e) for s, e in windows]

    # Insert the brand style at the end of the styles section (after the last
    # "Style:" line). If there's no styles section, we still inject cues — the
    # player falls back to its default for an unknown style name.
    out: list[str] = []
    if styles_idx != -1:
        # find the last Style: line index within the styles section
        last_style = styles_idx
        for i in range(styles_idx + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("[") and s.endswith("]"):
                break
            if s.startswith("Style:"):
                last_style = i
        brand_style_line = _brand_style_line(lines)
        for i, ln in enumerate(lines):
            out.append(ln)
            if i == last_style and styles_idx != -1:
                out.append(brand_style_line)
    else:
        out = list(lines)

    # Append the brand cues at the very end of the events section. Simplest
    # robust placement: after the last non-empty line of the file (events run to
    # EOF in practice for extracted tracks).
    # Find the events header again in `out` (indices shifted by the style insert).
    ev_i = next(
        (i for i, ln in enumerate(out) if ln.strip().lower() == "[events]"), -1
    )
    if ev_i == -1:
        out.extend(brand_lines)
    else:
        # insertion point = end of events body
        insert_at = len(out)
        for i in range(ev_i + 1, len(out)):
            s = out[i].strip()
            if s.startswith("[") and s.endswith("]"):
                insert_at = i
                break
        out[insert_at:insert_at] = brand_lines

    return "\n".join(out), len(brand_lines)


def _dialog_style_line(lines: list[str]) -> str:
    """Build the ``AXWDialog`` style line scaled to THIS script's resolution.

    Reference style: Noto Sans Bold at 75pt / 1080 PlayResY (≈6.94% frame height),
    matching Sonny Boy's visual weight but with an OFL-licensed font we can bundle.
    """
    play_res_y = _parse_play_res_y(lines)
    if not play_res_y or play_res_y <= 0:
        # Fallback: use the median of existing styles
        med = _median_style_fontsize(lines)
        play_res_y = _DIALOG_REF_RES_Y if not med else int(med * _DIALOG_REF_RES_Y / _DIALOG_BASE_FONTSIZE)

    k = play_res_y / _DIALOG_REF_RES_Y
    fs = max(8, int(_DIALOG_BASE_FONTSIZE * k))
    outline = round(_DIALOG_BASE_OUTLINE * k, 2)
    shadow = round(_DIALOG_BASE_SHADOW * k, 2)
    margin_v = max(2, int(_DIALOG_BASE_MARGIN_V * k))
    margin_lr = max(2, int(_DIALOG_BASE_MARGIN_LR * k))

    # ASS Style format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
    # OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY,
    # Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
    return (
        f"Style: {_DIALOG_STYLE_NAME},{_DIALOG_FONT_NAME},{fs},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H00000000,{_DIALOG_BOLD},0,0,0,100,100,0,0,1,{outline},{shadow},2,"
        f"{margin_lr},{margin_lr},{margin_v},1"
    )


def _is_dialogue_line(text: str, style: str) -> bool:
    """Classify an event line as plain dialogue (true) or signs/songs/positioned (false).

    Plain dialogue is ANY event that:
      - has NO positioning tags (\\pos \\move \\org \\clip \\iclip)
      - has NO rotation/drawing tags (\\frz \\frx \\fry \\p[1-9])
      - has NO karaoke tags (\\k \\kf \\ko \\K)
      - does NOT use a non-bottom alignment (\\an4-9, \\an[^123])
      - does NOT have a style name matching sign/song patterns

    Everything else (positioned signs, OP/ED, karaoke, typesetting) stays on its
    original style — we never touch them.
    """
    text_lower = text.lower()

    # Positioning / clipping / drawing / rotation tags
    positioned_tags = (
        r"\pos", r"\move", r"\org", r"\clip", r"\iclip",
        r"\frz", r"\frx", r"\fry", r"\p1", r"\p2", r"\p3", r"\p4",
    )
    for tag in positioned_tags:
        if tag in text_lower:
            return False

    # Karaoke tags
    if r"\k" in text_lower or r"\K" in text_lower:
        return False

    # Non-bottom alignment (\\an4-9 means middle/top rows)
    if r"\an" in text_lower:
        import re
        # Extract all \an<digit> tags
        for m in re.finditer(r"\\an(\d)", text_lower):
            align = int(m.group(1))
            if align not in (1, 2, 3):  # bottom row only
                return False

    # Style name patterns (case-insensitive)
    style_lower = style.lower()
    sign_patterns = [
        "sign", "op", "ed", "song", "title", "karaoke", "romaji", "kanji",
        "note", "credit", "insert", "logo", "caption", "typeset", "ts",
    ]
    for pattern in sign_patterns:
        if pattern in style_lower:
            return False

    return True


def restyle_dialogue(ass_text: str) -> tuple[str, int]:
    """Inject a bold readable dialogue style and reassign plain dialogue lines to it.

    Returns ``(new_ass_text, restyled_count)``. Idempotent: if the ``AXWDialog``
    style is already present, the text is returned unchanged (count 0).

    **What this does:**
    1. Adds one new style (``AXWDialog``) scaled to the script's PlayResY.
    2. For each ``Dialogue:`` event, checks if it's plain dialogue (no positioning,
       no karaoke, no sign/song style name). If plain, rewrites ONLY the Style field
       to ``AXWDialog``. Inline tags (``\\i1``, ``\\fs``, colors) are preserved.
    3. Positioned signs, OP/ED, karaoke, and any event with a sign/song style name
       are left completely untouched.

    **Safety:**
    - A plain, unpositioned bottom "note" can only be re-fonted, never mis-placed.
    - Real signs/songs are always marked with positioning or style names, so they're
      never restyled.
    - Inline overrides (per-line ``\\fs``, ``\\c``, etc.) still win, so a source's
      own intent is preserved within dialogue.
    """
    if _DIALOG_STYLE_NAME in ass_text:
        return ass_text, 0

    text = ass_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    # Locate section headers
    styles_idx = events_idx = -1
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        if low.startswith("[v4") and low.endswith("styles]"):
            styles_idx = i
        elif low == "[events]":
            events_idx = i

    if events_idx == -1:
        return ass_text, 0  # not a usable ASS

    # 1. Inject the AXWDialog style at the end of [V4+ Styles]
    out: list[str] = []
    if styles_idx != -1:
        last_style = styles_idx
        for i in range(styles_idx + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("[") and s.endswith("]"):
                break
            if s.startswith("Style:"):
                last_style = i

        dialog_style = _dialog_style_line(lines)
        for i, ln in enumerate(lines):
            out.append(ln)
            if i == last_style and styles_idx != -1:
                out.append(dialog_style)
    else:
        out = list(lines)

    # 2. Restyle plain dialogue events
    restyled = 0
    ev_i = next((i for i, ln in enumerate(out) if ln.strip().lower() == "[events]"), -1)
    if ev_i == -1:
        return "\n".join(out), 0

    for i in range(ev_i + 1, len(out)):
        ln = out[i]
        if not ln.startswith("Dialogue:"):
            continue

        # Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue

        style = parts[3].strip()
        text = parts[9]

        if _is_dialogue_line(text, style):
            parts[3] = _DIALOG_STYLE_NAME
            out[i] = ",".join(parts)
            restyled += 1

    return "\n".join(out), restyled


def brand_subtitle_file(ass_path, video_ms: int | None = None) -> int:
    """Brand an ``.ass`` file in place. Returns the number of cues inserted."""
    from pathlib import Path

    p = Path(ass_path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    new_text, count = brand_ass_text(raw, video_ms)
    if count:
        p.write_text(new_text, encoding="utf-8")
    return count


# Text subtitle codecs we can extract to .ass and inject branding cues into.
# Image subtitles (PGS/VobSub/dvb) carry no text stream, so they're copied
# through untouched (their track TITLE is still branded by the caller).
_TEXT_SUB_CODECS = frozenset({"ass", "ssa", "subrip", "srt", "webvtt", "vtt", "mov_text", "text"})


def is_text_sub(codec: str) -> bool:
    return (codec or "").lower() in _TEXT_SUB_CODECS


# ── Content-based subtitle language detection ─────────────────────────────────
# A subtitle track whose TITLE and tagged LANGUAGE are both missing used to fall
# straight through to the "〘 By @AniXWeebs 〙" placeholder. We can do better: the
# cue text itself tells us the language. A cheap deterministic Unicode-script
# pass nails the scripts that matter for anime (Japanese/Chinese/Korean/Hindi/
# Arabic/…); Latin-script languages fall through to ``langdetect`` (pure-Python,
# seeded for reproducibility). Result is a display word ("Japanese") or "".

# langdetect ISO codes (+ zh region variants) → the display words used on track
# labels. Value set kept in sync with ``stages._LANG_DISPLAY``.
_DETECT_LANG_NAMES = {
    "en": "English", "ja": "Japanese", "hi": "Hindi", "es": "Spanish",
    "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
    "ru": "Russian", "ar": "Arabic", "ko": "Korean", "zh": "Chinese",
    "zh-cn": "Chinese", "zh-tw": "Chinese", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "tr": "Turkish", "nl": "Dutch", "pl": "Polish",
    "uk": "Ukrainian", "fa": "Persian", "he": "Hebrew", "ta": "Tamil",
    "bn": "Bengali",
}

# Minimum meaningful characters before we trust a verdict — below this a stray
# English loanword or a "- Yes." line would be classified at random.
_DETECT_MIN_SIGNAL = 8
_DETECT_MIN_LATIN = 20  # langdetect (Latin) needs a bit more to be reliable

# Reverse of the display map: a detected display name → the ISO 639-2 code Matroska
# expects, so a track we identify by CONTENT also gets its ``language=`` tag set
# (players' own track menus then show the right language, not "undetermined").
_DISPLAY_TO_ISO = {
    "English": "eng", "Japanese": "jpn", "Hindi": "hin", "Spanish": "spa",
    "Portuguese": "por", "French": "fra", "German": "deu", "Italian": "ita",
    "Russian": "rus", "Arabic": "ara", "Korean": "kor", "Chinese": "zho",
    "Thai": "tha", "Vietnamese": "vie", "Indonesian": "ind", "Turkish": "tur",
    "Dutch": "nld", "Polish": "pol", "Ukrainian": "ukr", "Persian": "fas",
    "Hebrew": "heb", "Tamil": "tam", "Bengali": "ben",
}


def _ass_dialogue_text(ass_text: str) -> str:
    """Concatenate the visible TEXT of every ``Dialogue:``/``Comment:`` event,
    stripped of ASS override blocks and line-break escapes.

    Detection MUST run on dialogue text alone: a full ``.ass`` carries ~40 lines
    of ASCII ``[Script Info]``/``[V4+ Styles]`` headers that dilute a Japanese
    track's kana below the script-ratio threshold (measured 8.7% whole-file vs
    100% dialogue-only) — so feeding the whole file misclassifies it as Latin.
    Falls back to the raw text when no event lines are found.
    """
    text = ass_text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not (s.startswith("Dialogue:") or s.startswith("Comment:")):
            continue
        fields = ln.split(",", 9)
        if len(fields) >= 10:
            parts.append(fields[9])
    body = " ".join(parts) if parts else text
    body = re.sub(r"\{[^}]*\}", "", body)          # ASS override blocks {\pos…}
    for esc in (r"\N", r"\n", r"\h"):              # ASS line-break / hard-space
        body = body.replace(esc, " ")
    body = re.sub(r"<[^>]+>", "", body)            # stray srt/vtt markup
    return re.sub(r"\s+", " ", body).strip()


def _dominant_script_lang(text: str) -> str:
    """Language display name from the dominant non-Latin Unicode script, or "".

    Deterministic and dependency-free — handles the scripts anime subtitles
    actually ship in. Returns "" when the text is predominantly Latin (deferred
    to ``langdetect``) or too short to be sure.
    """
    kana = hangul = deva = thai = arab = cyr = hebr = han = latin = 0
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:                  # Hiragana + Katakana
            kana += 1
        elif 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF:  # Hangul
            hangul += 1
        elif 0x0900 <= o <= 0x097F:                # Devanagari (Hindi)
            deva += 1
        elif 0x0E00 <= o <= 0x0E7F:                # Thai
            thai += 1
        elif 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:  # Arabic
            arab += 1
        elif 0x0400 <= o <= 0x04FF:                # Cyrillic
            cyr += 1
        elif 0x0590 <= o <= 0x05FF:                # Hebrew
            hebr += 1
        elif 0x4E00 <= o <= 0x9FFF:                # CJK Han
            han += 1
        elif ch.isalpha():
            latin += 1
    signal = kana + hangul + deva + thai + arab + cyr + hebr + han
    if signal < _DETECT_MIN_SIGNAL:
        return ""
    # Only claim a script when it's a real share of the alphabetic content, so a
    # lone kanji in an English line doesn't read as Japanese.
    if signal < 0.25 * (signal + latin):
        return ""
    if kana > 0:                                   # any kana ⇒ Japanese (not zh)
        return "Japanese"
    if han > 0 and han >= max(hangul, deva, thai, arab, cyr, hebr):
        return "Chinese"
    best_n, best_lang = max(
        (hangul, "Korean"), (deva, "Hindi"), (thai, "Thai"),
        (arab, "Arabic"), (cyr, "Russian"), (hebr, "Hebrew"),
    )
    return best_lang if best_n > 0 else ""


def detect_subtitle_language(ass_text: str) -> str:
    """Best-effort language display name ("Japanese"/"Hindi"/"English") for an
    ``.ass`` subtitle track's CONTENT, or "" when undetermined.

    Two-stage: a deterministic Unicode-script pass (covers CJK/Hindi/Arabic/… —
    the anime-critical cases with no dependency), then ``langdetect`` for
    Latin-script languages. Seeded so results are reproducible run-to-run and in
    tests. Never raises — any failure yields "".
    """
    body = _ass_dialogue_text(ass_text or "")
    if not body:
        return ""
    by_script = _dominant_script_lang(body)
    if by_script:
        return by_script
    latin = sum(1 for c in body if c.isalpha() and ord(c) < 0x250)
    if latin < _DETECT_MIN_LATIN:
        return ""
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0
        code = (detect(body) or "").lower()
    except Exception:  # noqa: BLE001 — detection is best-effort, never fatal
        return ""
    return _DETECT_LANG_NAMES.get(code) or _DETECT_LANG_NAMES.get(code.split("-")[0], "")


# ── MoviesMod release-site stripping (DDL only) ───────────────────────────────
# MoviesMod DDL packs stamp their site into two places: standalone credit cues
# ("Downloaded from MoviesMod.org") inside the subtitle body, and audio/subtitle
# track TITLES ("Hindi - MoviesMod"). Both are stripped for DDL sources only
# (torrents keep their fansub cues + titles verbatim).
_MOVIESMOD_TOKEN = "moviesmod"


def strip_moviesmod_lines(ass_text: str) -> tuple[str, int]:
    """Remove every ``Dialogue:``/``Comment:`` event whose visible text mentions
    the MoviesMod site, returning ``(new_text, removed_count)``.

    The owner's rule: a cue that reads "Downloaded from MoviesMod.org" is dropped
    ENTIRELY (not just the URL token) — a legitimate line never contains the site
    name. Non-event lines (headers, styles) are untouched. Matching is
    case-insensitive on the event TEXT field only, so a style named after the
    site couldn't cause a false strip of real dialogue.
    """
    text = ass_text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    removed = 0
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("Dialogue:") or s.startswith("Comment:"):
            fields = ln.split(",", 9)
            body = fields[9] if len(fields) >= 10 else ln
            if _MOVIESMOD_TOKEN in body.lower():
                removed += 1
                continue
        out.append(ln)
    return "\n".join(out), removed


async def brand_torrent_subtitles(
    src, dest, *, sub_tracks: list[dict], video_ms: int | None = None,
    container_title: str | None = None, brand_subtitle_title=None,
    audio_tracks: list[dict] | None = None, brand_audio_title=None,
    lang_display=None, normalize_dialogue: bool = True,
    strip_domain: bool = False,
) -> dict:
    """Extract text subs, inject the Telegram cue, remux back into ``dest``.

    ``sub_tracks`` is the list from ``stages._sub_tracks`` ({index,codec,lang,
    title}). Text tracks are extracted to ``.ass``, branded via
    :func:`brand_ass_text`, and remuxed as the new subtitle streams (image subs
    pass through by copy).

    Each subtitle track's TITLE follows the full fallback chain, resolved HERE
    (not by the dumb ``brand_subtitle_title`` helper): a meaningful source title →
    the tagged language → the language DETECTED from the cue content
    (:func:`detect_subtitle_language`) → the bare ``〘 By @AniXWeebs 〙``
    placeholder. Content detection means an untagged, untitled track still gets
    its real language ("Japanese") instead of the anonymous placeholder.

    When ``audio_tracks`` + ``brand_audio_title`` are supplied, each AUDIO track's
    TITLE is branded IN THIS SAME REMUX (``-metadata:s:a:N title=…``). Doing it
    here — rather than only in a later mkvpropedit pass — means audio branding no
    longer depends on mkvpropedit being installed: the reported bug where subs
    came out branded but audio stayed plain ("English"/"Japanese") was exactly a
    silent mkvpropedit-unavailable fall-through. ``lang_display`` maps a language
    code to its display word for the audio fallback. Video is copied as-is; a
    container-level ENCODED_BY credit is added.

    ``strip_domain`` (DDL only) removes MoviesMod release-site cruft that torrents
    never carry: standalone "Downloaded from MoviesMod.org" cues are dropped from
    the subtitle body (:func:`strip_moviesmod_lines`), and an audio/subtitle TITLE
    that is just the site name (``is_moviesmod_title``) is treated as unusable so
    it falls back to the (detected/tagged) language.

    Returns a manifest ``{branded_tracks, total_cues, stripped_lines, ok}``.
    Best-effort: on any ffmpeg failure it leaves ``dest`` unwritten and reports
    ``ok=False`` so the caller can fall back to a metadata-only branding pass.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    from nekofetch.sources._branding import is_meaningful_track_name, is_moviesmod_title
    from nekofetch.sources._hls import find_ffmpeg

    src, dest = Path(src), Path(dest)
    ffmpeg = find_ffmpeg()
    result = {"branded_tracks": 0, "total_cues": 0, "stripped_lines": 0, "ok": False}
    if not ffmpeg:
        return result

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        # 1. Extract every TEXT subtitle track to its own .ass first (no branding
        #    yet). We need ALL tracks' cues in hand before choosing windows.
        extracted: dict[int, tuple[Path, str]] = {}  # rel index -> (path, raw text)
        stripped_rel: dict[int, int] = {}            # rel index -> lines removed
        for tr in sub_tracks:
            if not is_text_sub(tr.get("codec", "")):
                continue
            rel = tr["index"]
            ass_out = tmpd / f"sub_{rel}.ass"
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
                "-map", f"0:s:{rel}", "-c:s", "ass", str(ass_out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0 or not ass_out.exists() or ass_out.stat().st_size == 0:
                # Extraction failed for this track — skip it (copy through later).
                continue
            try:
                raw = ass_out.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — a bad track shouldn't sink the remux
                continue
            # DDL: drop MoviesMod credit cues BEFORE pooling, so a freed gap can be
            # reused for branding and the removal survives even a no-window track.
            if strip_domain:
                raw, removed = strip_moviesmod_lines(raw)
                if removed:
                    stripped_rel[rel] = removed
                    result["stripped_lines"] += removed
            extracted[rel] = (ass_out, raw)

        # 2. POOL every track's cues and pick the branding windows ONCE, so each
        #    slot is subtitle-free across ALL tracks (a gap in one track that has
        #    dialogue in another is never chosen). Every track then gets the SAME
        #    windows stamped in, keeping the on-screen branding in lockstep.
        pooled_cues: list[Cue] = []
        for _path, raw in extracted.values():
            pooled_cues.extend(extract_ass_cues(raw))
        shared_windows = branding_windows(pooled_cues, video_ms)

        # 3. Brand each extracted track with the shared windows. Write the .ass
        #    when we branded cues OR stripped MoviesMod lines — the remux always
        #    sources text tracks from these files, so a strip-only track must be
        #    re-written to disk for the removal to reach the output.
        branded_ass: dict[int, Path] = {}  # sub-relative index -> branded .ass
        for rel, (ass_out, raw) in extracted.items():
            try:
                new_text, cues = brand_ass_text(
                    raw, video_ms, windows=shared_windows,
                    normalize_dialogue=normalize_dialogue)
                if cues or stripped_rel.get(rel):
                    ass_out.write_text(new_text, encoding="utf-8")
                    result["total_cues"] += cues
                branded_ass[rel] = ass_out
            except Exception:  # noqa: BLE001 — a bad track shouldn't sink the remux
                continue

        # 4. Remux: video + audio copied; each subtitle stream either replaced by
        #    its branded .ass input, or copied from the source when not branded.
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
        for rel in sorted(branded_ass):
            cmd += ["-i", str(branded_ass[rel])]
        # Map original video + audio (copy).
        cmd += ["-map", "0:v", "-map", "0:a?"]
        # Map subtitles: branded ones from their extra input, others from source.
        input_of_branded = {rel: pos for pos, rel in enumerate(sorted(branded_ass), start=1)}
        for tr in sub_tracks:
            rel = tr["index"]
            if rel in input_of_branded:
                cmd += ["-map", f"{input_of_branded[rel]}:0"]
            else:
                cmd += ["-map", f"0:s:{rel}"]
        cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
        # Preserve attachments (fonts!) so styled .ass renders correctly.
        cmd += ["-map", "0:t?", "-c:t", "copy"]

        # Embed the bundled dialogue font faces so the restyled dialogue renders
        # identically on devices that lack the font (otherwise the player
        # substitutes a thinner face). Dedupe against attachments the source
        # already carries so we never create duplicate font records. The metadata
        # index starts past the preserved attachments (t:0..t:count-1 from the
        # -map 0:t? copy above) so it tags the right (newly attached) stream.
        if normalize_dialogue:
            attach_count, existing_attachments = await _source_attachments(src)
            attach_i = attach_count
            for font in _bundled_dialogue_fonts():
                if font.name.lower() in existing_attachments:
                    continue
                cmd += ["-attach", str(font),
                        f"-metadata:s:t:{attach_i}", f"mimetype={_DIALOG_FONT_MIMETYPE}"]
                attach_i += 1

        # 5. Per-track TITLE metadata. Subtitle chain: a meaningful source title
        #    → tagged language → language DETECTED from the cue content → the bare
        #    placeholder. For DDL (``strip_domain``) a MoviesMod site title is
        #    treated as unusable so it falls through to the language.
        for out_i, tr in enumerate(sub_tracks):
            title = tr.get("title", "")
            usable_title = is_meaningful_track_name(title) and not (
                strip_domain and is_moviesmod_title(title))
            resolved = ""
            detected = ""
            if usable_title:
                resolved = title
            else:
                if lang_display is not None:
                    resolved = lang_display(tr.get("lang", "")) or ""
                if not resolved:
                    ex = extracted.get(tr.get("index"))
                    if ex is not None:
                        detected = detect_subtitle_language(ex[1])
                        resolved = detected
            if brand_subtitle_title is not None:
                name = brand_subtitle_title(resolved, out_i + 1)
                cmd += [f"-metadata:s:s:{out_i}", f"title={name}"]
            # Language tag: keep the source's own tag; else stamp the ISO code of a
            # content-detected language so player track menus name it correctly.
            lang_tag = tr.get("lang") or (_DISPLAY_TO_ISO.get(detected, "") if detected else "")
            if lang_tag:
                cmd += [f"-metadata:s:s:{out_i}", f"language={lang_tag}"]
        # Audio TITLE metadata — branded in-remux so it never depends on a later
        # mkvpropedit pass (which, when unavailable, silently left audio plain
        # while subs came out branded). Chain: meaningful source title (never a
        # MoviesMod site title on DDL) → language word → ordinal. Audio carries no
        # text, so there's nothing to content-detect — the tagged language is the
        # only signal beyond the title.
        if brand_audio_title is not None:
            for out_i, tr in enumerate(audio_tracks or []):
                a_title = tr.get("title", "")
                if strip_domain and is_moviesmod_title(a_title):
                    a_title = ""  # drop the site watermark → fall back to language
                fb = None
                if lang_display is not None:
                    fb = lang_display(tr.get("lang", ""))
                title = brand_audio_title(a_title, out_i + 1, fallback_lang=fb)
                cmd += [f"-metadata:s:a:{out_i}", f"title={title}"]
        if container_title:
            cmd += ["-metadata", f"title={container_title}"]
        # Video credit: container-level ENCODED_BY on the torrent remux.
        cmd += ["-metadata", f"ENCODED_BY={ENCODED_BY_TAG}"]

        cmd += ["-map", "-0:d?", str(dest)]  # drop data streams ffmpeg can't copy
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            log.warning("torrent_subs.remux.failed",
                        rc=proc.returncode, err=err.decode(errors="replace")[-300:])
            return result

        result["branded_tracks"] = len(branded_ass)
        result["ok"] = True
        return result
