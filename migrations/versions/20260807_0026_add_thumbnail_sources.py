"""add durable thumbnail source inputs

Revision ID: 0026_add_thumbnail_sources
Revises: 0025_add_storage_pack_header_caption
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0026_add_thumbnail_sources"
down_revision: str | None = "0025_add_storage_pack_header_caption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thumbnail_sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("anime_doc_id", sa.String(length=48), nullable=False),
        # -1 is the durable key for mapping-only/root thumbnails.
        sa.Column("anilist_id", sa.BigInteger(), nullable=False, server_default=sa.text("-1")),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("image_path", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thumbnail_sources")),
        sa.UniqueConstraint("anime_doc_id", "anilist_id", name="uq_thumbnail_source_entry"),
    )
    op.create_index(
        op.f("ix_thumbnail_sources_anime_doc_id"),
        "thumbnail_sources", ["anime_doc_id"], unique=False,
    )
    op.create_index(
        op.f("ix_thumbnail_sources_anilist_id"),
        "thumbnail_sources", ["anilist_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_thumbnail_sources_anilist_id"), table_name="thumbnail_sources")
    op.drop_index(op.f("ix_thumbnail_sources_anime_doc_id"), table_name="thumbnail_sources")
    op.drop_table("thumbnail_sources")
