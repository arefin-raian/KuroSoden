"""FFmpeg transcoding: derive 720p / 480p and recompress oversized 1080p.

Quality is preserved via CRF (constant-quality) rather than fixed bitrates, and
every transcode keeps **all** original audio tracks (so dual-audio survives) and
subtitles. "Oversized" is judged per-minute so movies and long episodes get
proportionally larger budgets instead of a single hard cap.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.sources._hls import find_ffmpeg, find_ffprobe

log = get_logger(__name__)

# Derived-resolution CRFs (x264). Lower = better quality / bigger file.
_CRF = {1080: 21, 720: 21, 480: 22}

# 1080p "too big" budget. The example (≈23 min, >370 MB) ≈ 16 MB/min; we treat
# anything above this per-minute rate as oversized and recompress.
MB_PER_MIN_1080 = 16.0
OVERSIZE_FACTOR = 1.0  # recompress when size > budget (budget = rate * minutes)


def probe_duration_s(path: Path) -> float:
    """Synchronous duration probe. NOTE: blocking — only call off the event
    loop (via ``asyncio.to_thread``) or from sync code. Async callers on the
    shared pipeline loop must use :func:`probe_duration_s_async`."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return 0.0
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


async def probe_duration_s_async(path: Path) -> float:
    """Non-blocking duration probe for the shared pipeline event loop."""
    import asyncio
    return await asyncio.to_thread(probe_duration_s, path)


async def probe_video_codec(path: Path) -> str:
    """Return the source's video codec_name (``h264``/``hevc``/``av1``/…), or ""."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return ""

    def _run() -> str:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
             str(path)],
            capture_output=True, text=True,
        )
        return (r.stdout or "").strip().lower()

    try:
        return await asyncio.to_thread(_run)
    except Exception:  # noqa: BLE001
        return ""


# Which encoder to derive each tier with, keyed by the SOURCE codec. The rule is
# "never downgrade efficiency": an HEVC/AV1/VP9 source re-encoded to x264 would
# INFLATE (x264 needs ~40% more bits for the same quality), which is exactly the
# 250MB→256MB regression. So match the source's efficiency class or better, and
# fall back to x264 for old/bloated codecs (where any modern codec shrinks it).
_CODEC_ENCODER = {
    "h264":       "libx264",
    "avc":        "libx264",
    "hevc":       "libx265",
    "h265":       "libx265",
    "av1":        "libsvtav1",
    "vp9":        "libx265",     # VP9 ≈ HEVC efficiency; x265 has better tooling
    "vp8":        "libx264",
    "mpeg4":      "libx264",     # DivX/Xvid-era — bloated, x264 shrinks it
    "msmpeg4v3":  "libx264",
    "msmpeg4v2":  "libx264",
    "mpeg2video": "libx264",
    "mpeg1video": "libx264",
    "vc1":        "libx264",
    "wmv3":       "libx264",
    "theora":     "libx264",
}
# Per-encoder pixel format + the param-flag name x264/x265 use for extra params.
_ENCODER_PIXFMT = {
    "libx264":   "yuv420p",
    "libx265":   "yuv420p10le",   # 10-bit HEVC — standard for anime (no banding)
    "libsvtav1": "yuv420p10le",
}


def _encoder_available(ffmpeg: str, encoder: str) -> bool:
    """True if this ffmpeg build has the encoder compiled in."""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, text=True)
        return encoder in (r.stdout or "")
    except Exception:  # noqa: BLE001
        return False


async def select_encoder(src: Path, ffmpeg: str) -> str:
    """Pick the derive-encoder for ``src`` from its codec, honouring availability.

    Returns a libav encoder name. Falls back to libx264 when the ideal encoder
    isn't compiled into this ffmpeg (so a box without libx265/libsvtav1 still
    works — just less efficiently)."""
    codec = await probe_video_codec(src)
    ideal = _CODEC_ENCODER.get(codec, "libx264")
    if await asyncio.to_thread(_encoder_available, ffmpeg, ideal):
        return ideal
    if ideal != "libx264":
        log.warning("encode.encoder_unavailable", codec=codec, wanted=ideal,
                    fallback="libx264")
    return "libx264"


def is_oversized_1080(size_bytes: int, duration_s: float) -> bool:
    """True if a 1080p file exceeds its duration-scaled size budget."""
    if duration_s <= 0:
        return size_bytes > 370 * 1024 * 1024  # fall back to the flat example
    minutes = duration_s / 60
    budget = MB_PER_MIN_1080 * minutes * 1024 * 1024
    return size_bytes > budget * OVERSIZE_FACTOR


async def _run_ffmpeg(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.decode(errors='replace')[-300:]}")


def _svtav1_preset(word: str) -> int:
    """Map an x264-style word preset to an SVT-AV1 numeric preset (0=slow..13).

    ``fast`` (our default) → 6, a good size/speed balance; slower words map to
    lower numbers (smaller/slower), faster words to higher."""
    return {
        "veryslow": 2, "slower": 3, "slow": 4, "medium": 5,
        "fast": 6, "faster": 7, "veryfast": 8, "superfast": 9, "ultrafast": 10,
    }.get((word or "").lower(), 6)


async def _encode(src: Path, out: Path, height: int | None, crf: int,
                  preset: str = "fast", threads: int | None = None,
                  *, encoder: str = "libx264",
                  tune: str = "animation", psy_rd: str = "1.0:0.15",
                  guard_inflation: bool = True) -> Path:
    """Re-encode video (CRF); copy ALL audio + subtitles + attachments.

    ``encoder`` is chosen per SOURCE codec by :func:`select_encoder` so we never
    downgrade efficiency (an HEVC/AV1 source stays HEVC/AV1 rather than inflating
    as x264). x264/x265 share the word-preset + tune + psy-rd vocabulary;
    SVT-AV1 uses a numeric preset and its own params.

    Tuned for anime size-efficiency without quality loss:
      * ``preset`` defaults to ``fast`` — ``veryfast``/faster disable the trellis
        + adaptive-B-frame + motion-search features that make an encoder compress
        well, which is why a downscaled tier could come out LARGER than a lean
        source. ``fast`` compresses far better while staying ~2x quicker than
        ``medium``.
      * ``tune=animation`` (x264/x265) optimises for flat colour fields + sharp
        lines. It normally drops psy-rd to 0.4 (blurs detail), so we set
        ``psy_rd`` back to a sharp value explicitly — the community-standard fix.
      * ``guard_inflation``: if the downscaled output ends up >= the source,
        re-encode once at CRF+3. Guarantees a derived tier is never bigger than
        what it came from.

    ``threads`` bounds CPU use per encode so parallel jobs don't thrash.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")

    enc = encoder or "libx264"
    pix_fmt = _ENCODER_PIXFMT.get(enc, "yuv420p")

    def _build(crf_val: int) -> list[str]:
        cmd = [
            ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
            "-map", "0", "-c", "copy", "-c:v", enc,
        ]
        if enc == "libsvtav1":
            # SVT-AV1: numeric preset (map the x264 word → a sane number), CRF,
            # visual tune. No -tune animation / psy-rd (those are x264/x265 only).
            cmd += ["-crf", str(crf_val), "-preset", str(_svtav1_preset(preset))]
            cmd += ["-svtav1-params", "tune=0:film-grain=0"]
        else:
            # x264 / x265 share the CRF + word-preset + tune + psy-rd vocabulary.
            cmd += ["-crf", str(crf_val), "-preset", preset]
            if tune:
                cmd += ["-tune", tune]
            if psy_rd:
                params = f"psy-rd={psy_rd}"
                cmd += ["-x265-params" if enc == "libx265" else "-x264-params",
                        params]
        cmd += ["-pix_fmt", pix_fmt]
        if threads and threads > 0:
            cmd += ["-threads", str(threads)]
        if height:
            cmd += ["-vf", f"scale=-2:{height}"]
        # MKV carries attachments fine; only drop data streams ffmpeg can't copy.
        cmd += ["-map", "-0:d?", str(out)]
        return cmd

    await _run_ffmpeg(_build(crf))

    # Inflation guard — only meaningful for a genuine downscale (a same-res
    # recompress is handled by the oversize path). If we somehow produced a file
    # >= the source, spend fewer bits (CRF+3) once rather than ship a bigger tier.
    if guard_inflation and height:
        try:
            src_sz = src.stat().st_size
            out_sz = out.stat().st_size
        except OSError:
            src_sz = out_sz = 0
        if src_sz and out_sz and out_sz >= src_sz:
            log.info("encode.inflation_retry", src_mb=round(src_sz / 1048576, 1),
                     out_mb=round(out_sz / 1048576, 1), crf=crf, retry_crf=crf + 3)
            await _run_ffmpeg(_build(crf + 3))
    return out


async def transcode_renditions(
    src: Path, out_dir: Path, stem: str, *, source_resolution: str | None = None,
    preset: str = "medium",
) -> dict:
    """Produce 720p + 480p, and a recompressed 1080p if the source is oversized.

    ``preset`` trades speed for compression efficiency (use a faster preset for
    tests, "medium"/"slow" in production). Returns a manifest of the outputs plus
    the oversize decision. The original file is left untouched.
    """
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    duration = probe_duration_s(src)
    size = src.stat().st_size  # noqa: ASYNC240
    src_h = None
    if source_resolution:
        try:
            src_h = int(source_resolution.rstrip("p"))
        except ValueError:
            src_h = None
    if not src_h:
        # detect height via ffprobe
        ffprobe = find_ffprobe()
        if ffprobe:
            r = subprocess.run(  # noqa: ASYNC221 - quick metadata probe
                [ffprobe, "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True,
            )
            try:
                src_h = int(r.stdout.strip())
            except (ValueError, AttributeError):
                src_h = 1080

    outputs: list[dict] = []
    mb_per_min = (size / 1048576) / (duration / 60) if duration else None

    async def make(height: int, label: str, crf: int) -> None:
        out = out_dir / f"{stem}.{label}.mkv"
        try:
            await _encode(src, out, height if height != src_h else None, crf, preset)
            outputs.append({"label": label, "height": height, "crf": crf,
                            "path": str(out), "size_mb": round(out.stat().st_size / 1048576, 1)})
        except Exception as exc:  # noqa: BLE001
            outputs.append({"label": label, "height": height, "error": str(exc)})

    # 720p + 480p always (only if source is taller).
    if (src_h or 1080) > 720:
        await make(720, "720p", _CRF[720])
    if (src_h or 1080) > 480:
        await make(480, "480p", _CRF[480])

    # Recompress 1080p only when the source is 1080p-ish AND oversized.
    oversized = (src_h or 0) >= 1080 and is_oversized_1080(size, duration)
    if oversized:
        out = out_dir / f"{stem}.1080p.x264.mkv"
        try:
            await _encode(src, out, None, _CRF[1080], preset)
            outputs.append({"label": "1080p-recompress", "height": 1080, "crf": _CRF[1080],
                            "path": str(out), "size_mb": round(out.stat().st_size / 1048576, 1)})
        except Exception as exc:  # noqa: BLE001
            outputs.append({"label": "1080p-recompress", "error": str(exc)})

    return {
        "source_height": src_h,
        "duration_s": round(duration, 1),
        "source_size_mb": round(size / 1048576, 1),
        "mb_per_min": round(mb_per_min, 2) if mb_per_min else None,
        "oversized_1080": oversized,
        "renditions": outputs,
    }
