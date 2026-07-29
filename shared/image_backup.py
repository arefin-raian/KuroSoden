"""Durable image backup — mirror a post's images onto independent public hosts.

When the main channel is banned we rebuild every post byte-for-byte on a fresh
channel. That rebuild reads image URLs out of the DB, so those URLs must outlive
the original channel and the original CDN. A single host is a single point of
failure, so every image is mirrored to a **primary** host with fallbacks:

    catbox.moe  (primary — permanent, anonymous, 200 MB cap)
      └─ telegra.ph/upload  (fallback — anonymous, different operator)
          └─ ImgBB          (last resort — free public API, needs IMGBB_API_KEY)

:func:`backup_image` downloads the source bytes once, then tries each host in
order, returning every URL that sticks (and which host produced it). The caller
persists them so a later restore can prefer the mirror and, if even that is
gone, fall back to the next. Everything is best-effort: a total failure returns
a :class:`BackupImage` with all-``None`` mirrors rather than raising, so one dead
image never aborts a whole-channel backup.

ImgBB note: its upload response carries several sizes — ``data.url`` is the
full-resolution original, while ``data.thumb.url`` / ``data.medium.url`` are
downscaled. We deliberately keep **only** ``data.url`` so a restored post never
degrades to a low-resolution thumbnail.

:func:`backup_bytes` is the same mirror pipeline for bytes already in hand (e.g.
an admin-uploaded asset) — it skips the download step.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 60.0


@dataclass(slots=True)
class BackupImage:
    """Where a single source image now lives, mirrored."""
    source_url: str
    catbox_url: str | None = None
    telegraph_url: str | None = None
    imgbb_url: str | None = None

    @property
    def primary(self) -> str | None:
        """The best URL to rebuild from: mirrors first, then original source."""
        return (self.catbox_url or self.telegraph_url or self.imgbb_url
                or self.source_url or None)


_IMGBB_ENDPOINT = "https://api.imgbb.com/1/upload"


async def _upload_imgbb(container: Container, blob: bytes) -> str | None:
    """Push bytes to ImgBB (needs ``IMGBB_API_KEY``). Returns the URL or None.

    ImgBB accepts the image either as a base64 ``image`` form field or as a
    multipart binary upload. We try base64 first (its documented default) and,
    on any rejection, retry as multipart binary — some keys/edge payloads 400 on
    one form but succeed on the other. We return **only** ``data.url`` (the full-
    resolution original) — never ``data.thumb.url`` / ``data.medium.url``, which
    are downscaled and would degrade a restored post.

    Crucially, we do NOT ``raise_for_status()`` blindly: on a non-2xx we read the
    response body and log ImgBB's actual ``error.message`` (e.g. "Invalid API v1
    key", "Can't get target upload source info") so failures are diagnosable
    instead of a bare ``400 Bad Request``. Best-effort — always returns a URL or
    None, never raises.
    """
    key = getattr(getattr(container, "env", None), "imgbb_api_key", "") or ""
    if not key:
        log.warning("imgbackup.imgbb.no_key",
                    hint="Set IMGBB_API_KEY in .env to enable the ImgBB mirror")
        return None
    if not blob:
        log.warning("imgbackup.imgbb.empty_blob")
        return None

    async def _post(cli: httpx.AsyncClient, *, use_multipart: bool):
        if use_multipart:
            return await cli.post(_IMGBB_ENDPOINT, params={"key": key},
                                  files={"image": ("image", blob)})
        payload = base64.b64encode(blob).decode("ascii")
        return await cli.post(_IMGBB_ENDPOINT, params={"key": key},
                              data={"image": payload})

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
        for use_multipart in (False, True):
            mode = "multipart" if use_multipart else "base64"
            try:
                r = await _post(cli, use_multipart=use_multipart)
            except Exception as exc:  # noqa: BLE001 — transport hiccup, try next mode
                log.warning("imgbackup.imgbb.transport", mode=mode, error=str(exc))
                continue
            if r.status_code >= 400:
                # Surface ImgBB's real error message, not a bare status code.
                try:
                    err = (r.json().get("error") or {}).get("message")
                except Exception:  # noqa: BLE001
                    err = (r.text or "")[:200]
                log.warning("imgbackup.imgbb.http_error", mode=mode,
                            status=r.status_code, imgbb_error=err)
                continue
            try:
                body = r.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("imgbackup.imgbb.bad_json", mode=mode, error=str(exc))
                continue
            url = ((body or {}).get("data") or {}).get("url")
            if isinstance(url, str) and url.startswith("http"):
                log.info("imgbackup.imgbb.ok", mode=mode, url=url, bytes=len(blob))
                return url
            log.warning("imgbackup.imgbb.bad_body", mode=mode,
                        success=(body or {}).get("success"),
                        status=(body or {}).get("status"))
    return None


async def _download(source_url: str) -> tuple[bytes, str] | None:
    """Fetch ``source_url`` → (bytes, mime). None on any failure/empty body."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            r = await cli.get(source_url)
            r.raise_for_status()
            blob = r.content
    except Exception as exc:  # noqa: BLE001 — any transport hiccup → None
        log.warning("imgbackup.download.failed", url=source_url, error=str(exc))
        return None
    if not blob:
        return None
    mime = (r.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip()
    return blob, (mime or "image/jpeg")


async def _upload_catbox(blob: bytes, mime: str, ext: str, source_url: str) -> str | None:
    """Push bytes to catbox.moe. Returns the URL or None (best-effort)."""
    try:
        from nekofetch.providers.catbox import CatboxUploadError, upload_bytes

        return await upload_bytes(blob, filename=f"post{ext}", mime_type=mime)
    except (CatboxUploadError, httpx.HTTPError) as exc:
        log.warning("imgbackup.catbox.failed", url=source_url, error=str(exc))
        return None


async def _upload_telegraph(
    container: Container, blob: bytes, mime: str, source_url: str,
) -> str | None:
    """Push bytes to the telegra.ph file host. Returns the URL or None."""
    try:
        token = getattr(
            getattr(container.config, "thumbnail_channel", None),
            "telegraph_access_token", "",
        )
        from nekofetch.providers.metadata.telegraph_client import TelegraphClient

        client = TelegraphClient(token or "")
        url = await client.upload_image(blob, mime_type=mime)
        await client.close()
        return url
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("imgbackup.telegraph.failed", url=source_url, error=str(exc))
        return None


# Default host order when config doesn't specify one. Each host is independent so
# a single operator outage can't strand every mirror.
_DEFAULT_HOST_ORDER = ("catbox", "telegraph", "imgbb")


def _host_order(container: Container) -> tuple[str, ...]:
    """The configured image-host mirror order (``bot.image_host_order``).

    Unknown host names are dropped and any missing known host is appended so
    every mirror is still attempted — a typo can't silently disable a host."""
    raw = getattr(getattr(container.config, "bot", None), "image_host_order", None)
    if not raw:
        return _DEFAULT_HOST_ORDER
    order = [h for h in raw if h in _DEFAULT_HOST_ORDER]
    for h in _DEFAULT_HOST_ORDER:
        if h not in order:
            order.append(h)
    return tuple(order)


async def backup_bytes(
    container: Container, blob: bytes, *,
    mime: str = "image/jpeg", source_url: str = "",
) -> BackupImage:
    """Mirror image ``blob`` across every configured host, in ``image_host_order``.

    The shared upload core for bytes already in hand (admin uploads) and for the
    tail of :func:`backup_image` (post-download). Pushes to every host so the
    restore path has independent copies; any field may be ``None`` if that host
    rejected the upload. ``source_url`` is recorded (may be empty for a fresh
    upload) as the final rebuild fallback. Never raises.

    The mirror order is config-driven (``bot.image_host_order``) but the result
    always records each host's URL in its own field — :attr:`BackupImage.primary`
    picks catbox → telegraph → imgbb → source regardless of upload order.
    """
    result = BackupImage(source_url=source_url)
    if not blob:
        return result
    ext = ".png" if mime == "image/png" else ".jpg"

    for host in _host_order(container):
        if host == "catbox":
            result.catbox_url = await _upload_catbox(blob, mime, ext, source_url)
        elif host == "telegraph":
            result.telegraph_url = await _upload_telegraph(container, blob, mime, source_url)
        elif host == "imgbb":
            result.imgbb_url = await _upload_imgbb(container, blob)

    return result


async def backup_image(container: Container, source_url: str) -> BackupImage:
    """Mirror ``source_url`` across every host (catbox → telegraph → ImgBB).

    Downloads the bytes once, then delegates to :func:`backup_bytes` so the
    restore path has independent copies. Returns a :class:`BackupImage`; any
    field may be ``None`` if that host rejected the upload. Never raises.
    """
    if not source_url:
        return BackupImage(source_url=source_url)

    fetched = await _download(source_url)
    if fetched is None:
        return BackupImage(source_url=source_url)
    blob, mime = fetched
    return await backup_bytes(container, blob, mime=mime, source_url=source_url)
