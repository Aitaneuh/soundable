"""initial schema

Revision ID: 212be9759bd4
Revises:
Create Date: 2026-11-17 18:51:50.510600
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "212be9759bd4"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Accent-insensitive search ("beyonce" finds "Beyoncé").
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_table(
        "playlists",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlists")),
    )
    op.create_table(
        "tracks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("album", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Double(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("codec", sa.String(length=32), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'youtube', 'soundcloud')", name=op.f("ck_tracks_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracks")),
        sa.UniqueConstraint("content_sha256", name=op.f("uq_tracks_content_sha256")),
        sa.UniqueConstraint("file_path", name=op.f("uq_tracks_file_path")),
        sa.UniqueConstraint("source_key", name=op.f("uq_tracks_source_key")),
    )
    op.create_index(
        "ix_tracks_artist_lower", "tracks", [sa.literal_column("lower(artist)")], unique=False
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.SmallInteger(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_track_id", sa.UUID(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name=op.f("ck_jobs_status")
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_jobs_progress")),
        sa.ForeignKeyConstraint(
            ["result_track_id"],
            ["tracks.id"],
            name=op.f("fk_jobs_result_track_id_tracks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_queued",
        "jobs",
        ["run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_table(
        "playlist_tracks",
        sa.Column("playlist_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_playlist_tracks_playlist_id_playlists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_playlist_tracks_track_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("playlist_id", "position", name=op.f("pk_playlist_tracks")),
    )
    op.create_index(
        op.f("ix_playlist_tracks_track_id"), "playlist_tracks", ["track_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_playlist_tracks_track_id"), table_name="playlist_tracks")
    op.drop_table("playlist_tracks")
    op.drop_index(
        "ix_jobs_queued", table_name="jobs", postgresql_where=sa.text("status = 'queued'")
    )
    op.drop_table("jobs")
    op.drop_index("ix_tracks_artist_lower", table_name="tracks")
    op.drop_table("tracks")
    op.drop_table("playlists")
    op.drop_table("api_tokens")
