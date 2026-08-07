from __future__ import annotations

from types import SimpleNamespace

from bots.lelouch import screens

from kurosoden.shared.work_service import WorkItem
from kurosoden.tests.helpers import _create_request


def _buttons(screen):
    return [
        (button.text, button.callback_data)
        for row in screen.keyboard.inline_keyboard
        for button in row
    ]


def test_owner_panel_has_redo_and_clear_database_row():
    screen = screens.admin_panel(
        mode="normal", requests_open=True, total=2, working=1, is_owner=True,
    )
    buttons = _buttons(screen)
    assert ("🗂 Manage REQ/WRK", "mg|reqs|0") in buttons
    assert ("🔁 Redo", "redo|new") in buttons
    assert ("🧨 Clear Database", "lelouch|dbclear") in buttons


def test_non_owner_panel_hides_redo_and_clear_database():
    screen = screens.admin_panel(
        mode="normal", requests_open=True, total=2, working=1, is_owner=False,
    )
    buttons = _buttons(screen)
    assert ("🗂 Manage REQ/WRK", "mg|reqs|0") in buttons
    assert not any(data in {"redo|new", "lelouch|dbclear"} for _, data in buttons)


# ── Manage REQ/WRK list (9.6): REQ + WRK rows in one list ────────────────────


def _container(sessionmaker):
    return SimpleNamespace(pg_sessionmaker=sessionmaker)


async def test_manage_list_contains_req_and_wrk_rows(session, sessionmaker):
    from bots.lelouch.handlers.management import req_wrk_rows

    await _create_request(session, code="REQ-1073", anime_doc_id="anilist:1073",
                          status="queued")
    session.add(WorkItem(
        code="WRK-42", added_by_admin_id=1, anime_title="Fresh Work",
        anime_doc_id="anilist:42", stage="download", status="open",
    ))
    await session.commit()

    rows, total = await req_wrk_rows(_container(sessionmaker), limit=20)
    labels = "\n".join(label for label, _ in rows)
    callbacks = {cb_data for _, cb_data in rows}

    assert total == 2
    assert "REQ-1073" in labels
    assert "WRK-42" in labels
    # Both rows reach the same detail callback; the handler branches on WRK-.
    assert "mg|reqdet|REQ-1073" in callbacks
    assert "mg|reqdet|WRK-42" in callbacks


async def test_wrk_row_exposes_working_cancel_action(session, sessionmaker):
    from kurosoden.shared.work_service import WorkService

    session.add(WorkItem(
        code="WRK-7", added_by_admin_id=1, anime_title="Cancel Me",
        anime_doc_id="anilist:7", stage="encode", status="open",
    ))
    session.add(WorkItem(
        code="WRK-8", added_by_admin_id=1, anime_title="Keep Me",
        anime_doc_id="anilist:8", stage="download", status="claimed",
    ))
    await session.commit()

    svc = WorkService(sessionmaker)
    assert await svc.cancel("WRK-7") is True
    assert await svc.cancel("WRK-7") is False   # already cancelled
    assert await svc.cancel("WRK-404") is False  # absent

    from sqlalchemy import select
    async with sessionmaker() as s:
        cancelled = (await s.execute(select(WorkItem).where(
            WorkItem.code == "WRK-7"))).scalars().first()
        assert cancelled.status == "cancelled"
        kept = (await s.execute(select(WorkItem).where(
            WorkItem.code == "WRK-8"))).scalars().first()
        assert kept.status == "claimed"
