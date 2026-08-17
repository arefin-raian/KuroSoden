"""send_screen recovers artwork on MEDIA_EMPTY by uploading bytes; text only last.

The recurring bug: a card's artwork is a TMDB/AniList backdrop URL. Telegram's
server-side fetch of that URL intermittently fails with ``[400 MEDIA_EMPTY]``
(a ``BadRequest`` subclass) even though the URL is a 200 with valid JPEG from our
side — so early Senku cards lost their artwork and degraded to text.

Fix (screens.py): on a photo BadRequest for a REMOTE URL, we fetch the bytes
OURSELVES and re-upload them (Telegram never touches the origin, so the fetch
can't fail); the returned file_id is cached so later sends are instant. Only when
we can't fetch the bytes either does the card degrade to a text bubble. These
tests pin: byte-recovery on fresh send + edit paths, the text last-resort, and no
needless work when the first send is fine.
"""

from __future__ import annotations

import io

import pytest
from pyrogram.errors import MediaEmpty

from nekofetch.ui import screens
from nekofetch.ui.screens import Screen, send_screen


class _Msg:
    """Stand-in for a sent Pyrogram Message; carries a fake photo w/ file_id."""

    def __init__(self, kind: str, **kw):
        self.kind = kind
        self.photo = kw.get("photo")
        self.text = kw.get("text")
        self.id = 1

    async def delete(self):
        return None


class _Photo:
    def __init__(self, file_id: str):
        self.file_id = file_id


class _FakeClient:
    """Records sends. ``url_photo_raises`` makes a URL/file_id photo raise
    MEDIA_EMPTY; an uploaded BytesIO succeeds (mirrors the real behaviour where
    Telegram can't fetch the URL but accepts our uploaded bytes)."""

    def __init__(self, *, url_photo_raises=True, bytes_also_raises=False):
        self.url_photo_raises = url_photo_raises
        self.bytes_also_raises = bytes_also_raises
        self.photo_calls: list[dict] = []
        self.text_calls: list[dict] = []

    async def send_photo(self, chat_id, **kw):
        photo = kw.get("photo")
        self.photo_calls.append({"chat_id": chat_id, **kw})
        is_bytes = hasattr(photo, "read")  # BytesIO/file-like = our upload
        if is_bytes:
            if self.bytes_also_raises:
                raise MediaEmpty.__new__(MediaEmpty)
            return _Msg("photo", photo=_Photo("FILEID_FROM_BYTES"))
        if self.url_photo_raises:
            raise MediaEmpty.__new__(MediaEmpty)
        return _Msg("photo", photo=_Photo("FILEID_FROM_URL"))

    async def send_message(self, chat_id, text, **kw):
        self.text_calls.append({"chat_id": chat_id, "text": text, **kw})
        return _Msg("text", text=text)


@pytest.fixture(autouse=True)
def _clear_file_id_cache():
    screens._FILE_ID_CACHE.clear()
    yield
    screens._FILE_ID_CACHE.clear()


@pytest.fixture
def _fake_download(monkeypatch):
    """Make the byte-fetch return a small in-memory JPEG (no network)."""
    async def _dl(url):
        bio = io.BytesIO(b"\xff\xd8\xff\xe0JFIFdata")
        bio.name = "artwork.jpg"
        return bio
    monkeypatch.setattr(screens, "_download_photo_upload", _dl)


@pytest.mark.asyncio
async def test_media_empty_url_recovers_by_uploading_bytes(_fake_download):
    client = _FakeClient(url_photo_raises=True)
    screen = Screen(caption="<b>Frieren</b> — Episode 1",
                    image="https://image.tmdb.org/t/p/w1280/x.jpg")

    msg = await send_screen(client, 555, screen)  # fresh send path

    # It tried the URL, failed, then UPLOADED bytes successfully → a PHOTO card.
    assert len(client.photo_calls) == 2  # URL attempt + bytes attempt
    assert msg.kind == "photo"
    assert len(client.text_calls) == 0  # never degraded to text
    # The file_id from the byte upload is cached under the URL for next time.
    assert screens._FILE_ID_CACHE["https://image.tmdb.org/t/p/w1280/x.jpg"] == "FILEID_FROM_BYTES"


@pytest.mark.asyncio
async def test_text_fallback_only_when_bytes_also_fail(monkeypatch):
    # Byte fetch fails (returns None) → the LAST resort is a text card.
    async def _dl_none(url):
        return None
    monkeypatch.setattr(screens, "_download_photo_upload", _dl_none)

    client = _FakeClient(url_photo_raises=True)
    screen = Screen(caption="Vinland Saga", image="https://dead.example/y.jpg")

    msg = await send_screen(client, 777, screen)

    assert msg.kind == "text"
    assert len(client.text_calls) == 1 and "Vinland Saga" in client.text_calls[0]["text"]


@pytest.mark.asyncio
async def test_local_path_image_does_not_attempt_download(monkeypatch):
    # A local path that Telegram rejects is NOT a URL → no byte-fetch, straight
    # to text (there's nothing to re-fetch; the path upload already failed).
    called = {"n": 0}

    async def _dl(url):
        called["n"] += 1
        return None
    monkeypatch.setattr(screens, "_download_photo_upload", _dl)

    client = _FakeClient(url_photo_raises=True)
    screen = Screen(caption="Monster", image="/local/art_01.jpg")

    msg = await send_screen(client, 888, screen)

    assert called["n"] == 0  # never tried to download a local path
    assert msg.kind == "text"


@pytest.mark.asyncio
async def test_edit_in_place_recovers_by_uploading_bytes(_fake_download):
    """An in-place photo EDIT that hits MEDIA_EMPTY re-uploads bytes and edits
    again in place (keeps the card position, no send-new / delete flicker)."""
    edits: list[str] = []

    class _EditClient:
        async def edit_message_media(self, chat_id, message_id, media, **kw):
            photo = media.media
            if hasattr(photo, "read"):  # our uploaded bytes → succeeds
                edits.append("bytes")
                return _Msg("photo", photo=_Photo("FILEID_FROM_BYTES"))
            edits.append("url")
            raise MediaEmpty.__new__(MediaEmpty)

    class _Old(_Msg):
        def __init__(self):
            super().__init__("photo", photo=_Photo("old"))
            from types import SimpleNamespace
            self.chat = SimpleNamespace(id=1234)  # _try_edit_in_place reads chat.id

        async def edit_media(self):  # marks this as a real Message (not a ref)
            return None

    screen = Screen(caption="Bocchi", image="https://image.tmdb.org/t/p/original/z.jpg")
    old = _Old()

    result = await screens._try_edit_in_place(
        _EditClient(), old, screen, str(screen.image), screen.caption,
    )

    assert result is not None            # edit succeeded (no fall-through to send-new)
    assert edits == ["url", "bytes"]     # tried URL, then recovered with bytes
    assert screens._FILE_ID_CACHE[str(screen.image)] == "FILEID_FROM_BYTES"


@pytest.mark.asyncio
async def test_good_artwork_still_sends_photo_without_download(monkeypatch):
    """Sanity: a fine image sends a photo directly — no byte-fetch, no text."""
    called = {"n": 0}

    async def _dl(url):
        called["n"] += 1
        return None
    monkeypatch.setattr(screens, "_download_photo_upload", _dl)

    client = _FakeClient(url_photo_raises=False)
    screen = Screen(caption="Made in Abyss", image="https://ok.example/z.jpg")

    msg = await send_screen(client, 999, screen)

    assert len(client.photo_calls) == 1 and called["n"] == 0
    assert len(client.text_calls) == 0 and msg.kind == "photo"
