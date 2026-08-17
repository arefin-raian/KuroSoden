"""Archive extraction: pull episode files out of a downloaded zip / rar / 7z.

DDL sources deliver an archive rather than loose files. ``.zip`` needs no external
tooling (stdlib ``zipfile``); ``.rar`` and ``.7z`` require the 7-Zip binary, which
the launcher scripts install automatically (``p7zip`` on Linux/macOS, ``7z.exe``
dropped into ``tools/`` on Windows). We locate that binary the same way the torrent
downloader locates aria2c: PATH first, then a bundled copy under ``tools/``.

The public surface is tiny — :func:`extract_archive` unpacks an archive into a
destination directory and returns the video files it found, so a caller can feed
them straight into :func:`nekofetch.sources._torrent.order_episodes`.
"""

from __future__ import annotations

import asyncio
import shutil
import zipfile
from pathlib import Path

from nekofetch.core.logging import get_logger
from nekofetch.sources._torrent import VIDEO_EXT

log = get_logger(__name__)

# Magic bytes so we classify an archive by content, not just a (possibly wrong or
# missing) extension. A direct link often ends in a redirect/query string.
_ZIP_MAGIC = b"PK\x03\x04"
_RAR_MAGIC = b"Rar!\x1a\x07"          # RAR4 and RAR5 both start "Rar!\x1a\x07"
_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"


def find_7z() -> str | None:
    """Locate a 7-Zip CLI binary (handles zip, rar AND 7z).

    Mirrors :func:`nekofetch.sources._torrentdl.find_aria2`: PATH first (covers a
    ``p7zip``/``7-Zip`` system install), then a binary bundled under ``tools/`` next
    to the repo root or the CWD. ``7zz``/``7za`` are the common p7zip names; ``7z``
    is the Windows/most-distros name.
    """
    for name in ("7z", "7zz", "7za", "7z.exe"):
        found = shutil.which(name)
        if found:
            return found
    for base in (Path(__file__).resolve().parents[2], Path.cwd()):
        for name in ("7zz", "7za", "7z", "7z.exe"):
            cand = base / "tools" / name
            if cand.exists():
                return str(cand)
    return None


def _sniff_kind(path: Path) -> str:
    """Return 'zip' | 'rar' | '7z' | 'unknown' from magic bytes, extension fallback."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        head = b""
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if head.startswith(_RAR_MAGIC):
        return "rar"
    if head.startswith(_7Z_MAGIC):
        return "7z"
    ext = path.suffix.lower()
    return {".zip": "zip", ".rar": "rar", ".7z": "7z"}.get(ext, "unknown")


def _collect_videos(root: Path) -> list[Path]:
    """Every video file under ``root`` (recursive), stable-sorted by path."""
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.name.lower().endswith(VIDEO_EXT)
    )


def _nested_archives(root: Path) -> list[Path]:
    """Archive files (zip/rar/7z, or split .001/.partN) sitting under ``root``.

    Release hosts (MoviesMod et al.) routinely wrap the video in an archive
    INSIDE the downloaded archive, so a first-pass extract yields only more
    archives. We classify by magic bytes (extension is unreliable) plus the
    common split-part names so we can recurse into them.
    """
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if _sniff_kind(p) in ("zip", "rar", "7z"):
            out.append(p)
        elif name.endswith((".001",)) or ".part1." in name or name.endswith(".part1.rar"):
            out.append(p)  # first part of a split set — 7z follows the rest
    return sorted(out)


def _looks_like_text(path: Path) -> bool:
    """True when the 'archive' is really an HTML/JSON error page, not a binary
    archive — the worker link expired or returned an interstitial."""
    try:
        with path.open("rb") as fh:
            head = fh.read(512).lstrip()
    except OSError:
        return False
    return head[:1] in (b"<", b"{") or head[:5].lower() == b"<!doc"


async def _run_7z(binary: str, archive: Path, dest: Path, *, timeout: float = 1800.0) -> None:
    """Extract ``archive`` into ``dest`` with the 7-Zip CLI (flat-tree via ``x``).

    Hardened against hangs: ``stdin`` is closed (a password-protected archive
    would otherwise block forever waiting for a password on the pipe), and the
    whole run is bounded by ``timeout`` (killed + raised on expiry) so a stuck
    subprocess can never freeze the job.
    """
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "x", "-y", f"-o{dest}", str(archive)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            f"7-Zip timed out after {int(timeout)}s extracting {archive.name} "
            "(archive may be password-protected or corrupt)."
        ) from None
    if proc.returncode != 0:
        tail = (out or b"").decode(errors="replace").strip()[-400:] or "(no output)"
        raise RuntimeError(f"7-Zip failed to extract {archive.name}: {tail}")


async def _extract_once(archive: Path, dest_dir: Path, *, on_file=None) -> None:
    """Unpack a SINGLE archive into ``dest_dir`` (stdlib zip → 7-Zip fallback).

    No video assertion here — the caller decides whether the result is usable or
    needs a nested pass. When ``on_file(done, total, name)`` is given and the
    archive is a zip, members are extracted one-by-one so the caller can show
    REAL per-file extraction progress. Raises ``RuntimeError`` when a rar/7z
    arrives with no 7-Zip binary installed.
    """
    kind = _sniff_kind(archive)
    if kind == "zip":
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.infolist() if not m.is_dir()]
                total = len(members)
                if on_file is None or total == 0:
                    await asyncio.to_thread(zf.extractall, dest_dir)
                else:
                    for i, m in enumerate(members, start=1):
                        await asyncio.to_thread(zf.extract, m, dest_dir)
                        try:
                            await on_file(i, total, m.filename)
                        except Exception:  # noqa: BLE001 — progress is cosmetic
                            pass
            return
        except zipfile.BadZipFile:
            log.warning("archive.zip.bad", archive=archive.name)
            # fall through to 7-Zip (handles some zips stdlib rejects)

    binary = find_7z()
    if binary is None:
        raise RuntimeError(
            f"cannot extract {archive.name}: 7-Zip is not installed. It installs "
            "automatically on the next launch (run.sh / run.bat) — restart the bots "
            "and retry, or send a .zip which needs no extra tooling."
        )
    # 7-Zip is a single subprocess — no per-member hook; signal start/finish.
    if on_file is not None:
        try:
            await on_file(0, 1, archive.name)
        except Exception:  # noqa: BLE001
            pass
    await _run_7z(binary, archive, dest_dir)
    if on_file is not None:
        try:
            await on_file(1, 1, archive.name)
        except Exception:  # noqa: BLE001
            pass


async def _count_7z_videos(binary: str, archive: Path) -> int:
    """Best-effort count of video entries inside an archive via ``7z l`` (0 on any
    failure). Gives the extraction bar a correct denominator for rar/7z (and zips
    stdlib rejected) so it shows ``i/12`` instead of ``1/1``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "l", "-ba", "-slt", str(archive),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except Exception:  # noqa: BLE001 — listing is best-effort
        return 0
    n = 0
    for line in (out or b"").decode(errors="replace").splitlines():
        s = line.strip()
        if s.lower().startswith("path =") and s.split("=", 1)[1].strip().lower().endswith(VIDEO_EXT):
            n += 1
    return n


async def _expected_video_count(archive: Path) -> int:
    """How many video files this archive should yield (best-effort, 0 if unknown).

    Used only as the progress-bar denominator — a wrong/zero value never affects
    the actual extraction, just the ``i/n`` display."""
    if _sniff_kind(archive) == "zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                return sum(
                    1 for m in zf.infolist()
                    if not m.is_dir() and m.filename.lower().endswith(VIDEO_EXT)
                )
        except Exception:  # noqa: BLE001 — fall through to 7-Zip listing
            pass
    binary = find_7z()
    return await _count_7z_videos(binary, archive) if binary else 0


async def _extract_tree(archive: Path, dest_dir: Path, *, _depth: int = 0) -> list[Path]:
    """Extract ``archive`` (recursing up to two levels into nested archives) and
    return the videos found. Progress is handled by the poller in
    :func:`extract_archive`, so this stays quiet."""
    await _extract_once(archive, dest_dir)
    vids = _collect_videos(dest_dir)
    if vids:
        return vids
    if _depth < 2:
        for inner in _nested_archives(dest_dir):
            sub = inner.parent / f"{inner.stem}__x"
            try:
                await _extract_tree(inner, sub, _depth=_depth + 1)
            except Exception as exc:  # noqa: BLE001 — try the rest
                log.warning("archive.nested.failed", inner=inner.name, error=str(exc))
        vids = _collect_videos(dest_dir)
    return vids


async def extract_archive(
    archive: Path, dest_dir: Path, *, on_file=None, _depth: int = 0,
) -> list[Path]:
    """Unpack ``archive`` into ``dest_dir`` and return the video files found.

    Recurses up to two levels into nested archives (the video is often wrapped
    in an inner zip/rar by release hosts). When ``on_file(done, total, name)``
    (async) is given, a REAL per-file extraction bar is driven off the video
    files landing in ``dest_dir`` — uniform across zip / rar / 7z / nested, so a
    rar or a nested-inside-a-zip release shows ``i/12`` instead of a frozen
    ``1/1``. Raises ``RuntimeError`` with an actionable message when the download
    isn't an archive at all (an expired link / HTML interstitial) or when it
    extracts but yields no video anywhere.
    """
    archive = Path(archive)
    dest_dir = Path(dest_dir)

    # A download that isn't a real archive (worker returned HTML/JSON) would
    # otherwise fail deep inside 7-Zip with a confusing message — surface it.
    # No real archive starts with '<' or '{' (zip=PK, rar=Rar!, 7z=7z magic), so
    # the text sniff is reliable even when the file is *named* .zip (an expired
    # link served an HTML page).
    if _depth == 0 and _looks_like_text(archive):
        raise RuntimeError(
            f"{archive.name} is not an archive — the link returned a web page, not "
            "a file (it likely expired or needs a fresh direct link)."
        )

    # No progress hook → extract quietly and return.
    if on_file is None:
        vids = await _extract_tree(archive, dest_dir, _depth=_depth)
        if vids:
            return vids
        raise RuntimeError(
            f"no video files found inside {archive.name} after extraction "
            f"(looked for {', '.join(VIDEO_EXT)}; also recursed into nested archives). "
            "The archive may hold only samples/subs, use an unsupported container, or "
            "be password-protected."
        )

    # Progress path: poll the destination for video files as they appear and
    # report a real i/n bar, with a best-effort pre-count for the denominator.
    expected = await _expected_video_count(archive)
    stop = asyncio.Event()

    async def _poll() -> None:
        last = -1
        while not stop.is_set():
            have = len(_collect_videos(dest_dir))
            if have != last:
                last = have
                try:
                    await on_file(have, expected or max(have, 1), "")
                except Exception:  # noqa: BLE001 — progress is cosmetic
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.4)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    poller = asyncio.ensure_future(_poll())
    try:
        vids = await _extract_tree(archive, dest_dir, _depth=_depth)
    finally:
        stop.set()
        try:
            await poller
        except BaseException:  # noqa: BLE001 — poller teardown never fails extraction
            pass

    # Final authoritative tick so the bar lands on n/n.
    try:
        await on_file(len(vids), expected or len(vids) or 1, "")
    except Exception:  # noqa: BLE001
        pass

    if vids:
        return vids
    raise RuntimeError(
        f"no video files found inside {archive.name} after extraction "
        f"(looked for {', '.join(VIDEO_EXT)}; also recursed into nested archives). "
        "The archive may hold only samples/subs, use an unsupported container, or "
        "be password-protected."
    )
