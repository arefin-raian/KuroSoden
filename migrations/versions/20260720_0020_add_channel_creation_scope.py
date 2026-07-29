"""add channel creation scope + owning userbot account

Two-scope channel creation (feature #41): a distribution channel is created
either by an admin ("own" — the admin adds our bots as admins) or by a pooled
userbot session ("userbot"). For userbot-scoped channels we record which account
(``Account.name``) created it so each session's channel count can be tallied
against its per-account quota — a session at capacity is skipped when picking one.

Revision ID: 0020_add_channel_creation_scope
Revises: 0019_add_index_section_repurposed
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0020_add_channel_creation_scope"
down_revision: str | None = "0019_add_index_section_repurposed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("creation_scope", sa.String(16), nullable=True))
    op.add_column("bots", sa.Column("userbot_account", sa.String(64), nullable=True))
    op.create_index(
        op.f("ix_bots_userbot_account"), "bots", ["userbot_account"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bots_userbot_account"), table_name="bots")
    op.drop_column("bots", "userbot_account")
    op.drop_column("bots", "creation_scope")
