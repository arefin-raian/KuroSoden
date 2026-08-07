"""collapse duplicate storage-pack caption columns

Revision ID: 0029_collapse_storage_pack_caption
Revises: 0028_add_storage_pack_caption
Create Date: 2026-08-07

``caption`` is now the only storage-pack header caption field. The older
``header_caption`` column was always mirrored to the same value; this migration
backfills any legacy nulls before removing it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0029_collapse_storage_pack_caption"
down_revision: str | None = "0028_add_storage_pack_caption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE storage_packs "
            "SET caption = header_caption "
            "WHERE caption IS NULL AND header_caption IS NOT NULL"
        )
    )
    op.drop_column("storage_packs", "header_caption")


def downgrade() -> None:
    op.add_column(
        "storage_packs",
        sa.Column("header_caption", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE storage_packs "
            "SET header_caption = caption "
            "WHERE header_caption IS NULL AND caption IS NOT NULL"
        )
    )
