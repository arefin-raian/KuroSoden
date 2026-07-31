"""Telegraph API client — create galleries for asset selection.

Generates Telegraph pages with numbered image galleries so admins can visually
browse available logos, posters, and backdrops from TMDB and select one by number.

API docs: https://telegra.ph/api
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"


@dataclass
class TelegraphPage:
    path: str
    url: str
    title: str
    description: str = ""


@dataclass
class ImageEntry:
    """One image in a Telegraph gallery."""
    url: str                 # full TMDB image URL (absolute)
    caption: str             # human-readable caption (e.g. "3 — Attack on Titan (English)")
    alt_text: str | None = None


class TelegraphError(Exception):
    """Telegraph API returned an error."""


class TelegraphClient:
    """Client for the Telegraph API.

    Usage::

        client = TelegraphClient("your_access_token")
        page = await client.create_gallery(
            title="Attack on Titan — Logos",
            images=[
                ImageEntry(url="https://...", caption="1 — English Logo"),
                ImageEntry(url="https://...", caption="2 — Japanese Logo"),
            ],
        )
        print(page.url)  # https://telegra.ph/... — share this with admins
    """

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _call(self, method: str, **params) -> dict:
        """Call a Telegraph API method."""
        url = f"{TELEGRAPH_API}/{method}"
        params.setdefault("access_token", self.access_token)
        try:
            resp = await self.http.post(url, json=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise TelegraphError(f"Telegraph API request failed: {exc}") from exc

        if not data.get("ok"):
            error = data.get("error", "unknown error")
            raise TelegraphError(f"Telegraph API error: {error}")
        return data.get("result", {})

    async def create_gallery(
        self,
        title: str,
        images: list[ImageEntry],
        *,
        author_name: str = "NekoFetch",
        author_url: str = "",
    ) -> TelegraphPage:
        """Create a Telegraph page with a gallery of images.

        Each image is rendered with its caption underneath. The page content
        uses the Telegraph native ``<img/>`` tag inside ``<figure>`` elements
        for a clean gallery layout.
        """
        # Build Telegraph DOM content nodes
        # Each image: <figure><img src="..."/><figcaption>caption</figcaption></figure>
        content: list[dict] = []
        for img in images:
            # Telegraph expects a <figure> with <img> and optional <figcaption>
            fig_children: list[dict] = [
                {"tag": "img", "attrs": {"src": img.url}},
            ]
            if img.caption:
                fig_children.append({
                    "tag": "figcaption",
                    "children": [img.caption],
                })
            content.append({
                "tag": "figure",
                "children": fig_children,
            })

        result = await self._call(
            "createPage",
            title=title,
            author_name=author_name,
            author_url=author_url,
            content=content,
        )
        path = result.get("path", "")
        url = result.get("url", f"https://telegra.ph/{path}")
        log.info("telegraph.gallery.created", path=path, images=len(images))
        return TelegraphPage(path=path, url=url, title=title)

    async def upload_image(
        self, file_bytes: bytes, *, mime_type: str = "image/jpeg",
    ) -> str | None:
        """Upload raw image bytes to Telegraph's file host; return the full URL.

        Telegram DISABLED the original ``telegra.ph/upload`` endpoint (Durov
        turned off new media uploads for the platform), so we try the still-live
        ``graph.org`` mirror FIRST and fall back to ``telegra.ph`` only in case
        it ever comes back. Returns an absolute URL, or ``None`` on any failure
        so the caller can fall back to another host. Anonymous — no token needed.
        """
        if not file_bytes:
            return None
        files = {"file": ("card.jpg", file_bytes, mime_type)}
        # graph.org first (telegra.ph/upload is disabled as of 2024).
        for host in ("https://graph.org", "https://telegra.ph"):
            try:
                resp = await self.http.post(f"{host}/upload", files=files)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegraph.upload.failed", host=host, error=str(exc))
                continue
            # Success shape: [{"src": "/file/abc.jpg"}]; error: {"error": "..."}.
            if isinstance(data, list) and data and isinstance(data[0], dict):
                src = data[0].get("src")
                if src:
                    url = src if src.startswith("http") else f"{host}{src}"
                    log.info("telegraph.upload.ok", host=host, url=url,
                             bytes=len(file_bytes))
                    return url
            log.warning("telegraph.upload.bad_response", host=host,
                        body=str(data)[:200])
        return None


# Module-level shared instance (lazy-initialized via container).
_default_client: TelegraphClient | None = None


def get_telegraph_client(access_token: str) -> TelegraphClient:
    """Return (or create) the shared Telegraph client."""
    global _default_client
    if _default_client is None:
        _default_client = TelegraphClient(access_token)
    return _default_client


async def close_telegraph_client() -> None:
    """Clean up the shared Telegraph client's HTTP session."""
    global _default_client
    if _default_client is not None:
        await _default_client.close()
        _default_client = None
