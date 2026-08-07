"""Regression: redo/abandon must delete a pack's END STICKER too.

Root cause: at upload (`storage_channel_service.upload_pack`), the end sticker's
message id is stored as ``StoragePack.end_message_id`` but is NOT appended to
``file_message_ids`` (that list holds media only). ``_purge_pack_messages`` took
the ``file_message_ids`` branch and skipped the mutually-exclusive range branch,
so on the normal upload path the sticker was never deleted — every redo orphaned
one sticker in the storage channel, and they accumulated.

This pins that the sticker id is included in the delete set on BOTH shapes:
the media-list path (production) and the start/end range path (no file ids).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekofetch.services.request_service import RequestService


class _RecordingClient:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple, range)):
            self.deleted.extend(int(m) for m in ids)
        else:
            self.deleted.append(int(ids))


def _svc(client):
    return RequestService(SimpleNamespace(admin_client=client))


@pytest.mark.asyncio
async def test_end_sticker_deleted_on_media_list_path():
    """Production shape: header + file_message_ids populated, sticker id is a
    separate end_message_id beyond the last file. It MUST be deleted."""
    client = _RecordingClient()
    pack = SimpleNamespace(
        channel_id=-100123,
        header_message_id=5,
        file_message_ids=[6, 7, 8, 9, 10, 11],
        start_message_id=6,
        end_message_id=12,  # the end sticker — one past the last file
    )
    await _svc(client)._purge_pack_messages(pack)

    assert 12 in client.deleted, "end sticker (end_message_id) was orphaned"
    assert 5 in client.deleted  # header still deleted
    assert set(range(6, 12)) <= set(client.deleted)  # all files still deleted


@pytest.mark.asyncio
async def test_range_path_still_deletes_end_and_no_duplicate():
    """No file_message_ids → range branch. end_message_id is inside the range,
    so it's already covered; the guard must not double-add it."""
    client = _RecordingClient()
    pack = SimpleNamespace(
        channel_id=-100123,
        header_message_id=9,
        file_message_ids=None,
        start_message_id=10,
        end_message_id=20,
    )
    await _svc(client)._purge_pack_messages(pack)

    assert 20 in client.deleted
    assert client.deleted.count(20) == 1, "end_message_id added twice"


@pytest.mark.asyncio
async def test_no_end_sticker_no_spurious_delete():
    """When there's no end sticker, end_message_id equals the last file id, so
    it's already in file_message_ids — the guard must not re-add it."""
    client = _RecordingClient()
    pack = SimpleNamespace(
        channel_id=-100123,
        header_message_id=5,
        file_message_ids=[6, 7, 8],
        start_message_id=6,
        end_message_id=8,  # no sticker → last file id
    )
    await _svc(client)._purge_pack_messages(pack)

    assert client.deleted.count(8) == 1
    assert sorted(client.deleted) == [5, 6, 7, 8]
