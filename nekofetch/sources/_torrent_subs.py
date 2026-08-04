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

# Style line for the injected branding cue. Mirrors the streaming _subs Brand
# style (Trebuchet MS, bold, outline+shadow) but under our namespaced name.
_BRAND_STYLE_LINE = (
    f"Style: {_BRAND_STYLE_NAME},Trebuchet MS,72,&H00FFFFFF,&H000000FF,"
    "&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,3.6,2,2,50,50,60,1"
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

    The parse is line-oriented and forgiving — it keeps the three ASS sections in
    order, appends the brand style at the end of ``[V4+ Styles]`` and the brand
    cues at the end of ``[Events]``. Malformed input (no ``[Events]``) is returned
    unchanged rather than raising, so one odd track never sinks the remux.
    """
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
        for i, ln in enumerate(lines):
            out.append(ln)
            if i == last_style and styles_idx != -1:
                out.append(_BRAND_STYLE_LINE)
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


async def brand_torrent_subtitles(
    src, dest, *, sub_tracks: list[dict], video_ms: int | None = None,
    container_title: str | None = None, brand_subtitle_title=None,
) -> dict:
    """Extract text subs, inject the Telegram cue, remux back into ``dest``.

    ``sub_tracks`` is the list from ``stages._sub_tracks`` ({index,codec,lang,
    title}). Text tracks are extracted to ``.ass``, branded via
    :func:`brand_ass_text`, and remuxed as the new subtitle streams (image subs
    pass through by copy). Every subtitle track's TITLE is set to
    ``brand_subtitle_title(original_title, ordinal)`` — for torrents the caller
    passes a helper that keeps the ORIGINAL title (e.g. "Signs & Songs") and adds
    the ``〘 @AniXWeebs 〙`` handle, instead of a language label. Audio/video are
    copied as-is; a container-level ENCODED_BY credit is added.

    Returns a manifest ``{branded_tracks, total_cues, ok}``. Best-effort: on any
    ffmpeg failure it leaves ``dest`` unwritten and reports ``ok=False`` so the
    caller can fall back to a metadata-only branding pass.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    from nekofetch.sources._hls import find_ffmpeg

    src, dest = Path(src), Path(dest)
    ffmpeg = find_ffmpeg()
    result = {"branded_tracks": 0, "total_cues": 0, "ok": False}
    if not ffmpeg:
        return result

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        # 1. Extract every TEXT subtitle track to its own .ass first (no branding
        #    yet). We need ALL tracks' cues in hand before choosing windows.
        extracted: dict[int, tuple[Path, str]] = {}  # rel index -> (path, raw text)
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
            extracted[rel] = (ass_out, raw)

        # 2. POOL every track's cues and pick the branding windows ONCE, so each
        #    slot is subtitle-free across ALL tracks (a gap in one track that has
        #    dialogue in another is never chosen). Every track then gets the SAME
        #    windows stamped in, keeping the on-screen branding in lockstep.
        pooled_cues: list[Cue] = []
        for _path, raw in extracted.values():
            pooled_cues.extend(extract_ass_cues(raw))
        shared_windows = branding_windows(pooled_cues, video_ms)

        # 3. Brand each extracted track with the shared windows.
        branded_ass: dict[int, Path] = {}  # sub-relative index -> branded .ass
        for rel, (ass_out, raw) in extracted.items():
            try:
                new_text, cues = brand_ass_text(raw, video_ms, windows=shared_windows)
                if cues:
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

        # 5. Per-track TITLE metadata (torrent rule: original title + 〘 handle 〙).
        for out_i, tr in enumerate(sub_tracks):
            if brand_subtitle_title is not None:
                title = brand_subtitle_title(tr.get("title", ""), out_i + 1)
                cmd += [f"-metadata:s:s:{out_i}", f"title={title}"]
            if tr.get("lang"):
                cmd += [f"-metadata:s:s:{out_i}", f"language={tr['lang']}"]
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
