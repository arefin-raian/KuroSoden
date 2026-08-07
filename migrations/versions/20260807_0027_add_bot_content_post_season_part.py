"""add split-season identity to bot content cards

Revision ID: 0027_add_bot_content_post_season_part
Revises: 0026_add_thumbnail_sources
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0027_add_bot_content_post_season_part"
down_revision: str | None = "0026_add_thumbnail_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bot_content_posts",
        sa.Column("season_part", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_content_posts", "season_part")
