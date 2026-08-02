"""Make AniKoto usable as a *dual-audio* source.

AniKoto serves sub and dub as two separate streams; KickAssAnime usually serves
one file with two audio tracks. To fall back to AniKoto for a dual-audio request
we must decide whether AniKoto's sub video and dub video are the *same cut* \u2014 if
they are, we can keep one video and graft both audio tracks onto it (one dual
file); if they differ (different runtime \u2192 different cut), we must keep them as
separate sub and dub files.

The key trick: we decide **without downloading the videos**. An HLS VOD playlist
lists every segment's exact duration (``#EXTINF``); summing them gives the precise
runtime from a few KB of manifest. Equal runtimes \u21d2 same cut \u21d2 mergeable.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from nekofetch.core.logging import get_logger
from nekofetch.sources._branding import ENCODED_BY_TAG, brand_audio_title
from nekofetch.sources._hls import find_ffmpeg

log = get_logger(__name__)

_EXTINF = re.compile(r"#EXTINF:\s*([0-9.]+)")
_IS_MASTER = re.compile(r"#EXT-X-STREAM-INF", re.IGNORECASE)


async def playlist_duration(http: httpx.AsyncClient, url: str,
                            headers: dict | None = None, *, _depth: int = 0) -> float | None:
    """Total runtime of an HLS VOD stream, summed from the manifest only.

    Resolves a master playlist down to its first media playlist, then adds up the
    ``#EXTINF`` values. Returns seconds, or ``None`` if it can't be determined.
    No media segments are downloaded.
    """
    if _depth > 2:
        return None
    try:
        resp = await http.get(url, headers=headers or {})
        text = resp.text
    except (httpx.HTTPError, ValueError):
        return None
    if _IS_MASTER.search(text):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return await playlist_duration(http, urljoin(url, line), headers,
                                               _depth=_depth + 1)
        return None
    total = sum(float(m.group(1)) for m in _EXTINF.finditer(text))
    return total or None


def are_mergeable(dur_a: float | None, dur_b: float | None, *,
                  tol_seconds: float = 2.5, tol_ratio: float = 0.01) -> bool:
    """True if two runtimes are close enough to be the same cut (mergeable).

    A small absolute *or* relative tolerance handles encoder rounding while still
    rejecting genuinely different cuts (e.g. censored vs uncensored, recap vs full).
    """
    if not dur_a or not dur_b:
        return False
    diff = abs(dur_a - dur_b)
    return diff <= max(tol_seconds, tol_ratio * max(dur_a, dur_b))


async def merge_dual(sub_file: Path, dub_file: Path, dest: Path) -> bool:
    """Remux a subbed file + a dubbed file into one dual-audio MKV.

    Keeps the sub file's video + (Japanese) audio + subtitles, and grafts the dub
    file's audio in as the second (English) track. Stream-copy only \u2014 no
    re-encode \u2014 so it's fast and lossless. Returns True on success.

    Audio track titles go through the chrome-bracket helper so MediaInfo / VLC
    shows the brand on every merged release, matching the style used by
    the new-download path (``_mux.py``) and the manual re-mux path
    (``_normalize.py``).
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    args = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(sub_file), "-i", str(dub_file),
        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0", "-map", "0:s?",
        "-c", "copy",
        "-metadata", f"ENCODED_BY={ENCODED_BY_TAG}",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0",
        f"title={brand_audio_title('Japanese', 1)}",
        "-metadata:s:a:1", "language=eng", "-metadata:s:a:1",
        f"title={brand_audio_title('English', 2)}",
        "-disposition:a:0", "default", str(dest),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("dualaudio.merge.failed", error=err.decode(errors="ignore")[:200])
        return False
    return True
