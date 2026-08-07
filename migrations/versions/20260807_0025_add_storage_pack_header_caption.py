"""add editable header caption to storage_packs

Revision ID: 0025_add_storage_pack_header_caption
Revises: 0024_add_bot_content_post_anilist_id
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0025_add_storage_pack_header_caption"
down_revision: str | None = "0024_add_bot_content_post_anilist_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_packs",
        sa.Column("header_caption", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storage_packs", "header_caption")
