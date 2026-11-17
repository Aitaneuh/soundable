"""play counts

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("play_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("tracks", sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tracks_last_played_at", "tracks", ["last_played_at"])


def downgrade() -> None:
    op.drop_index("ix_tracks_last_played_at", table_name="tracks")
    op.drop_column("tracks", "last_played_at")
    op.drop_column("tracks", "play_count")
