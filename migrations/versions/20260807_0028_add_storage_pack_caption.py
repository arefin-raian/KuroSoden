"""add canonical storage pack caption

Revision ID: 0028_add_storage_pack_caption
Revises: 0027_add_bot_content_post_season_part
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0028_add_storage_pack_caption"
down_revision: str | None = "0027_add_bot_content_post_season_part"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_packs",
        sa.Column("caption", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storage_packs", "caption")
