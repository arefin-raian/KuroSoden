"""add ImgBB mirror column to published post backups

ImgBB replaced the dead envs.sh as the last-resort durable image host. The
backup row previously stored only the catbox + telegraph mirrors, so a post
whose catbox AND telegraph uploads both failed (but ImgBB succeeded) would lose
the surviving mirror on restore. This column persists the ImgBB full-resolution
URL (``data.url`` — never the thumb/medium size) so every mirror is durable.

Revision ID: 0021_add_backup_imgbb_url
Revises: 0020_add_channel_creation_scope
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0021_add_backup_imgbb_url"
down_revision: str | None = "0020_add_channel_creation_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "published_post_backups",
        sa.Column("image_imgbb_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("published_post_backups", "image_imgbb_url")
