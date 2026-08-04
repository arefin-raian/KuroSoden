"""Subtitle post-processing: clean watermarks, standardize styling, brand.

Pipeline per subtitle track:
  1. Parse WebVTT cues.
  2. Drop watermark / ad cues (e.g. the ``kaa.mx`` ruby tag KickAssAnime injects).
  3. Find the best subtitle-free gaps in three time zones (start / middle / end)
     and insert a branded cue in each:
        Telegram: @AniXWeebs
     ("Telegram" in Telegram blue, "@AniXWeebs" in white, larger font).
  4. Emit two renditions:
        * ``.vtt`` — standardized STYLE block + cue classes (web / mpv correct)
        * ``.ass`` — Advanced SubStation, so the colours/size render identically
          in VLC, mpv and any MKV-aware player after muxing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Channel handle + on-screen cue text. Imported from the shared chrome-bracket
# module so the chrome stays uniform across the new-download, manual re-mux,
# and dual-audio paths.
from nekofetch.sources._branding import BRAND_HANDLE

# Telegram brand blue.
TG_BLUE_HEX = "#229ED9"
TG_BLUE_ASS = "&H00D99E22"  # ASS is &HAABBGGRR  (22 9E D9 -> D9 9E 22)
WHITE_ASS = "&H00FFFFFF"

# Branding text (on-screen subtitle cue — the ASS-style "Telegram: @AniXWeebs"
# block is structurally different from the bracket track-title format used in
# ``_branding.brand_subtitle_title`` so it stays local to this module).
BRAND_PREFIX = "Telegram:"

# Max time the branding stays on screen, and margin kept clear of real dialogue.
# It sits in the longest subtitle-free gap (usually the OP), so a generous window
# is safe — branding_window still clips it to the gap so it never touches dialogue.
BRAND_MAX_MS = 8000
BRAND_MARGIN_MS = 400

# Cues whose (tag-stripped) text matches any of these are dropped as watermarks.
_WATERMARK_RE = re.compile(
    r"(kaa\.mx|kaa\.to|kickassanime|anizone|animekaizoku|"
    r"subscene|opensubtitles|downloaded\s+from|encoded\s+by|"
    r"\bripped\s+by\b|uploaded\s+by)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Cue:
    start: int            # ms
    end: int              # ms
    text: str             # may contain VTT inline tags / newlines
    settings: str = ""    # original VTT cue settings (position etc.)


def _ts_to_ms(ts: str) -> int:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        return 0
    sec, _, ms = s.partition(".")
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + int((ms + "000")[:3])


def _ms_to_vtt(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _ms_to_ass(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"


def parse_vtt(text: str) -> list[Cue]:
    """Parse WebVTT into cues (ignores NOTE / STYLE / header blocks)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # find the timing line
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        timing = lines[ti]
        m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)\s*(.*)", timing)
        if not m:
            continue
        start, end = _ts_to_ms(m.group(1)), _ts_to_ms(m.group(2))
        settings = m.group(3).strip()
        body = "\n".join(lines[ti + 1:]).strip()
        if not body:
            continue
        cues.append(Cue(start, end, body, settings))
    return cues


def clean_cues(cues: list[Cue]) -> tuple[list[Cue], int]:
    """Drop watermark/ad cues. Returns (kept_cues, removed_count)."""
    kept: list[Cue] = []
    removed = 0
    for c in cues:
        plain = _TAG_RE.sub("", c.text).strip()
        if not plain or _WATERMARK_RE.search(plain) or _WATERMARK_RE.search(c.text):
            removed += 1
            continue
        kept.append(c)
    return kept, removed


# Never place branding inside the last N ms of the episode (the ending sequence).
ENDING_EXCLUSION_MS = 180_000  # 3 minutes


def find_gaps_in_zones(cues: list[Cue], cutoff_ms: int) -> list[tuple[int, int]]:
    """Find the best subtitle-free gap in each of three time zones:
    zone 1: 0-25%, zone 2: 25-75%, zone 3: 75%-cutoff (before outro).

    Returns up to 3 ``(start, end)`` tuples, one per zone (fewer if a
    zone has no usable gap of at least 1500 ms).
    """
    if not cues:
        # No dialogue at all — brand once early, once mid, once before cutoff.
        z1 = (1000, min(cutoff_ms, 1000 + BRAND_MAX_MS))
        z2 = (cutoff_ms // 2, min(cutoff_ms, cutoff_ms // 2 + BRAND_MAX_MS)) if cutoff_ms > 3000 else None
        z3_start = max(1000, cutoff_ms - BRAND_MAX_MS - 1000)
        z3 = (z3_start, min(cutoff_ms, z3_start + BRAND_MAX_MS)) if cutoff_ms > 3000 else None
        return [g for g in (z1, z2, z3) if g is not None]

    ordered = sorted(cues, key=lambda c: c.start)
    zones = [
        (0, int(cutoff_ms * 0.25)),           # zone 1: first 25%
        (int(cutoff_ms * 0.25), int(cutoff_ms * 0.75)),  # zone 2: middle
        (int(cutoff_ms * 0.75), cutoff_ms),    # zone 3: last 25% before outro
    ]

    result: list[tuple[int, int]] = []

    def best_in_range(z_start: int, z_end: int) -> tuple[int, int] | None:
        best = (-1, 0, 0)
        # lead-in inside zone
        lead = min(ordered[0].start, z_end)
        if lead > z_start and lead - z_start > best[0]:
            best = (lead - z_start, z_start, lead)
        prev_end = ordered[0].end
        for c in ordered[1:]:
            if prev_end >= z_end:
                break
            gs = max(prev_end, z_start)
            ge = min(c.start, z_end)
            if gs < ge and ge - gs > best[0]:
                best = (ge - gs, gs, ge)
            prev_end = max(prev_end, c.end)
        # tail inside zone
        if prev_end < z_end:
            gs = max(prev_end, z_start)
            ge = z_end
            if gs < ge and ge - gs > best[0]:
                best = (ge - gs, gs, ge)
        return (best[1], best[2]) if best[0] >= 1500 else None

    for zs, ze in zones:
        gap = best_in_range(zs, ze)
        if gap and gap[1] - gap[0] >= 1500:
            result.append(gap)

    return result


def branding_windows(cues: list[Cue], video_ms: int | None = None) -> list[tuple[int, int]]:
    """Pick up to 3 on-screen windows for branding cues (one per zone).

    Finds the best subtitle-free gap in each zone (start / middle / end),
    excluding the final 3 minutes (ending/outro). Each gap must be at least
    1.5 seconds wide. Returns a list of ``(start_ms, end_ms)`` tuples.
    """
    last_end = max((c.end for c in cues), default=0)
    end_ref = video_ms if video_ms else last_end
    cutoff = max(1000, end_ref - ENDING_EXCLUSION_MS)

    gaps = find_gaps_in_zones(cues, cutoff)
    windows: list[tuple[int, int]] = []
    for gstart, gend in gaps:
        gap = gend - gstart
        start = gstart + BRAND_MARGIN_MS
        dur = min(BRAND_MAX_MS, gap - 2 * BRAND_MARGIN_MS)
        if dur < 1000:
            start, dur = gstart, min(BRAND_MAX_MS, gap)
        windows.append((start, start + dur))
    return windows


# --------------------------------------------------------------------------- #
# VTT output
# --------------------------------------------------------------------------- #

_VTT_STYLE = (
    "WEBVTT\n\n"
    "STYLE\n"
    "::cue {\n"
    '  font-family: "Trebuchet MS", "Segoe UI", sans-serif;\n'
    "  color: #FFFFFF;\n"
    "  text-shadow: 0 0 3px rgba(0,0,0,0.9);\n"
    "}\n"
    "::cue(.tg) { color: " + TG_BLUE_HEX + "; font-weight: bold; }\n"
    "::cue(.handle) { color: #FFFFFF; font-weight: bold; }\n"
    "::cue(.brand) { font-size: 1.2em; }\n\n"
)


def build_vtt(cues: list[Cue], brands: list[tuple[int, int]]) -> str:
    out = [_VTT_STYLE]
    branding_lines = []
    for bstart, bend in brands:
        branding_lines.append(
            f"{_ms_to_vtt(bstart)} --> {_ms_to_vtt(bend)} line:82% align:center\n"
            f"<c.brand><c.tg>{BRAND_PREFIX}</c> <c.handle>{BRAND_HANDLE}</c></c>\n"
        )
    # Insert branding cues in chronological order among dialogue cues.
    all_items = [(b[0], True, line) for b, line in zip(brands, branding_lines)]
    all_items += [(c.start, False, c) for c in cues]
    all_items.sort(key=lambda x: x[0])
    for _, is_brand, item in all_items:
        if is_brand:
            out.append(item)  # branding line (already formatted)
        else:
            c = item
            settings = (" " + c.settings) if c.settings else ""
            out.append(f"{_ms_to_vtt(c.start)} --> {_ms_to_vtt(c.end)}{settings}\n{c.text}\n")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# ASS output (reliable colour/size in VLC / mpv after muxing)
# --------------------------------------------------------------------------- #

_ASS_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding"
)
# Default dialogue style — the common anime-fansub look (matches our reference
# release): Cabin, size 75, bold, white with a thick black outline + soft shadow,
# bottom-centred. Large enough to read comfortably on mobile. Players substitute
# the font if Cabin is absent but keep the size/weight/outline.
_ASS_STYLE_DEFAULT = (
    "Style: Default,Cabin,75,&H00FFFFFF,&H000000FF,&H00000000,"
    "&H96000000,-1,0,0,0,100,100,0,0,1,3.6,1.5,2,50,50,60,1"
)
# "Brand" style for the @AniXWeebs cue — same size as the dialogue so it doesn't
# shout, but a DIFFERENT font (Trebuchet MS vs the dialogue's Cabin), bold, with a
# touch of letter-spacing and a bit more shadow so it reads as a distinct, tidy
# little tag rather than a normal subtitle line. Colours come from inline tags
# (Telegram blue + white). Players substitute the font if absent but keep the look.
_ASS_STYLE_BRAND = (
    "Style: Brand,Trebuchet MS,75,&H00FFFFFF,&H000000FF,&H00000000,"
    "&H96000000,-1,0,0,0,100,100,1,0,1,3.6,2,2,50,50,60,1"
)
_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1920\nPlayResY: 1080\n"
    "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
    "[V4+ Styles]\n"
    f"{_ASS_FORMAT}\n{_ASS_STYLE_DEFAULT}\n{_ASS_STYLE_BRAND}\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def _vtt_text_to_ass(text: str) -> str:
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"<i>", r"{\\i1}", text, flags=re.IGNORECASE)
    text = re.sub(r"</i>", r"{\\i0}", text, flags=re.IGNORECASE)
    text = re.sub(r"<b>", r"{\\b1}", text, flags=re.IGNORECASE)
    text = re.sub(r"</b>", r"{\\b0}", text, flags=re.IGNORECASE)
    # drop ruby (watermark already gone, but keep base text of any other ruby)
    text = re.sub(r"<rt>.*?</rt>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG_RE.sub("", text)
    return text.replace("\n", "\\N").strip()


def build_ass(cues: list[Cue], brands: list[tuple[int, int]]) -> str:
    rows = [_ASS_HEADER]
    brand_text = (
        f"{{\\fad(400,400)}}{{\\c{TG_BLUE_ASS}}}{BRAND_PREFIX} "
        f"{{\\c{WHITE_ASS}}}{BRAND_HANDLE}"
    )
    for bstart, bend in brands:
        rows.append(
            f"Dialogue: 0,{_ms_to_ass(bstart)},{_ms_to_ass(bend)},Brand,,0,0,0,,{brand_text}"
        )
    for c in sorted(cues, key=lambda x: x.start):
        rows.append(
            f"Dialogue: 0,{_ms_to_ass(c.start)},{_ms_to_ass(c.end)},Default,,0,0,0,,"
            f"{_vtt_text_to_ass(c.text)}"
        )
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def content_signature(cues: list[Cue]) -> str:
    """Stable hash of a track's dialogue (timings + tag-stripped text).

    Used to deduplicate subtitle tracks that are identical across variants while
    keeping ones that differ in timing/wording/content.
    """
    import hashlib
    parts = [f"{c.start}|{c.end}|{_TAG_RE.sub('', c.text).strip()}"
             for c in sorted(cues, key=lambda c: (c.start, c.end))]
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def pooled_branding_windows(
    cue_lists: list[list[Cue]], video_ms: int | None = None
) -> list[tuple[int, int]]:
    """Branding windows chosen from the UNION of several tracks' cues.

    When a release ships multiple subtitle files, a gap that's silent in one
    track may sit on top of dialogue in another. Pooling every track's (cleaned)
    cues before picking the three windows guarantees each chosen slot is
    subtitle-free across ALL tracks, so the same on-screen branding times land in
    every track without ever covering dialogue.
    """
    pooled: list[Cue] = [c for cues in cue_lists for c in cues]
    return branding_windows(pooled, video_ms)


def shared_windows_for_vtts(
    vtt_paths: list[Path], video_ms: int | None = None
) -> list[tuple[int, int]]:
    """Read + clean every VTT and return branding windows common to them all.

    A thin convenience over :func:`pooled_branding_windows` for callers that hold
    file paths (the streaming mux + the manual-normalize path). Unreadable files
    are skipped rather than raising, so one bad track never blocks the rest.
    """
    cue_lists: list[list[Cue]] = []
    for p in vtt_paths:
        try:
            raw = Path(p).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — a bad track shouldn't sink the pool
            continue
        cleaned, _ = clean_cues(parse_vtt(raw))
        cue_lists.append(cleaned)
    return pooled_branding_windows(cue_lists, video_ms)


def process_subtitle(
    vtt_path: Path,
    video_ms: int | None = None,
    *,
    windows: list[tuple[int, int]] | None = None,
) -> dict:
    """Clean + style + brand a VTT file in place, and emit an .ass sibling.

    ``video_ms`` is the true video duration so the branding can exclude the final
    3 minutes. ``windows`` — pre-computed branding slots pooled across every
    subtitle track of the release (see :func:`shared_windows_for_vtts`); pass it
    so a multi-sub release stamps the SAME silent-in-all-tracks windows into each
    file. When ``None`` the windows are derived from THIS track's own cues.
    Returns metadata incl. a content signature for dedup.
    """
    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    cues = parse_vtt(raw)
    cleaned, removed = clean_cues(cues)
    sig = content_signature(cleaned)
    brands = windows if windows is not None else branding_windows(cleaned, video_ms)

    # ASS-only policy: emit the styled .ass and drop the source .vtt/.srt so no
    # non-ASS subtitle files linger. Everything downstream muxes the .ass.
    ass_path = vtt_path.with_suffix(".ass")
    ass_path.write_text(build_ass(cleaned, brands), encoding="utf-8")
    if ass_path != vtt_path:
        vtt_path.unlink(missing_ok=True)

    return {
        "vtt": None,
        "ass": str(ass_path),
        "cues_in": len(cues),
        "cues_kept": len(cleaned),
        "watermarks_removed": removed,
        "brand_at_ms": brands[0][0] if brands else 0,
        "brand_at": f"{brands[0][0]//60000}:{(brands[0][0]//1000)%60:02d}" if brands else "none",
        "brand_count": len(brands),
        "signature": sig,
    }
