from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EntryState:
    done: bool = False
    substate: str = "select_logo"


def advance(entries: list[EntryState], index: int, action: str) -> tuple[int | None, str]:
    """Pure model of Senku's approve/redo transition for one entry."""
    if action == "approve":
        entries[index].done = True
        for nxt in range(index + 1, len(entries)):
            if not entries[nxt].done:
                return nxt, "select_logo"
        return None, "complete"
    if action == "redo":
        entries[index] = EntryState(done=False, substate="select_logo")
        return index, "select_logo"
    raise ValueError(action)


def test_approve_advances_to_next_pending_entry():
    entries = [EntryState(), EntryState()]
    assert advance(entries, 0, "approve") == (1, "select_logo")
    assert entries[0].done is True


def test_redo_keeps_target_entry_pending_and_preserves_previous_done_entry():
    entries = [EntryState(done=True), EntryState(done=True)]
    assert advance(entries, 1, "redo") == (1, "select_logo")
    assert entries[0].done is True
    assert entries[1].done is False


def test_all_done_gates_completion():
    entries = [EntryState(done=True), EntryState(done=False)]
    assert advance(entries, 0, "approve") == (1, "select_logo")
    assert advance(entries, 1, "approve") == (None, "complete")
