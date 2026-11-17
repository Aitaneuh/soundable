"""playlist names unique (case-insensitive)

Revision ID: 0002
Revises: 212be9759bd4
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "212be9759bd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_playlists_name_lower",
        "playlists",
        [sa.literal_column("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_playlists_name_lower", table_name="playlists")
