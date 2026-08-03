"""add anilist_id to bot_content_posts

The watch guide's quality text now deep-links to each entry's own season-card
message (``{BOT_QUAL#<anilist_id>:…}`` → ``t.me/<handle>/<msg_id>``). A
ban-restore reposts the backed-up cards onto a fresh channel, so it must remap
each entry's card to its NEW message id — which needs the card to carry its
``anilist_id``. This column persists it on the live ``bot_content_posts`` rows;
it's NULL for the info card / watch guide / footer (no single entry).

Revision ID: 0024_add_bot_content_post_anilist_id
Revises: 0023_add_assignment_offers
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0024_add_bot_content_post_anilist_id"
down_revision: str | None = "0023_add_assignment_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bot_content_posts",
        sa.Column("anilist_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_bot_content_posts_anilist_id",
        "bot_content_posts",
        ["anilist_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_bot_content_posts_anilist_id", table_name="bot_content_posts")
    op.drop_column("bot_content_posts", "anilist_id")
