"""Access scoping for Kuro Soden's multi-bot command surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kurosoden.shared.access_gate import is_owner, is_staff
from kurosoden.shared.command_menu import apply_for_user, default_commands


class _Client:
    def __init__(self) -> None:
        self.calls = []

    async def set_bot_commands(self, commands, *, scope=None) -> None:
        self.calls.append((list(commands), scope))


def _container(owner_id: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(security=SimpleNamespace(owner_id=owner_id)),
        env=SimpleNamespace(admin_ids=[owner_id]),
    )


def _user(role: str, telegram_id: int) -> SimpleNamespace:
    return SimpleNamespace(role=role, telegram_id=telegram_id)


def _command_names(commands) -> list[str]:
    return [cmd.command for cmd in commands]


def test_staff_only_bots_publish_staff_global_menu():
    # Staff-only bots surface their STAFF tier as the global default so the ☰
    # menu is never blank before per-user scoping kicks in (strangers are gated
    # by the auth middleware, not by an empty menu). Owner-only /settings must
    # NOT leak into the global default.
    for bot in ("levi", "senku", "gojo"):
        names = _command_names(default_commands(bot))
        assert "start" in names
        assert "tasks" in names
        assert "settings" not in names
    # Senku's staff tier specifically includes its operational commands.
    senku = _command_names(default_commands("senku"))
    assert {"create", "generate"}.issubset(senku)
    assert "edit_thumbnail" not in senku


def test_lelouch_global_menu_is_plain_user_only():
    names = _command_names(default_commands("lelouch"))
    assert names == ["start", "myrequests", "help"]
    assert "batch" not in names
    assert "admin" not in names
    assert "settings" not in names


@pytest.mark.asyncio
async def test_non_owner_admin_gets_staff_commands_without_owner_settings():
    client = _Client()
    await apply_for_user(client, _container(owner_id=100), "gojo", 200, _user("admin", 200))

    commands, scope = client.calls[-1]
    names = _command_names(commands)
    assert scope.chat_id == 200
    assert {"start", "tasks", "publish", "recover", "schedule"}.issubset(names)
    assert "settings" not in names


@pytest.mark.asyncio
async def test_owner_gets_owner_only_commands():
    client = _Client()
    await apply_for_user(client, _container(owner_id=100), "gojo", 100, _user("admin", 100))

    commands, scope = client.calls[-1]
    assert scope.chat_id == 100
    assert "settings" in _command_names(commands)


@pytest.mark.asyncio
async def test_owner_gets_levi_caption_editor_command():
    client = _Client()
    await apply_for_user(client, _container(owner_id=100), "levi", 100, _user("admin", 100))

    commands, scope = client.calls[-1]
    assert scope.chat_id == 100
    assert {"settings", "packcaptions"}.issubset(_command_names(commands))


@pytest.mark.asyncio
async def test_owner_gets_senku_thumbnail_editor_command():
    client = _Client()
    await apply_for_user(client, _container(owner_id=100), "senku", 100, _user("admin", 100))

    commands, scope = client.calls[-1]
    assert scope.chat_id == 100
    assert {"settings", "edit_thumbnail"}.issubset(_command_names(commands))


@pytest.mark.asyncio
async def test_lelouch_non_owner_admin_gets_profile_tier_not_command_console():
    client = _Client()
    await apply_for_user(client, _container(owner_id=100), "lelouch", 200, _user("admin", 200))

    names = _command_names(client.calls[-1][0])
    assert "batch" in names
    assert "admin" not in names
    assert "settings" not in names


def test_access_gate_role_helpers_use_resolved_user():
    owner = SimpleNamespace(nf_user=_user("admin", 100))
    staff = SimpleNamespace(nf_user=_user("staff", 200))
    plain = SimpleNamespace(nf_user=_user("user", 300))

    assert is_owner(_container(owner_id=100), owner)
    assert not is_owner(_container(owner_id=100), staff)
    assert is_staff(staff)
    assert is_staff(owner)
    assert not is_staff(plain)
