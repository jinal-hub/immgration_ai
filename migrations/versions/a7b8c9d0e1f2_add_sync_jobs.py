"""Add durable sync job and per-source progress tables.

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("sync_jobs"):
        return

    op.create_table(
        "sync_jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("total_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','INTERRUPTED')",
            name="ck_sync_job_status",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("idx_sync_job_status", "sync_jobs", ["status"])
    op.create_index("idx_sync_job_started", "sync_jobs", ["started_at"])

    op.create_table(
        "sync_job_sources",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("fetch_status", sa.String(length=32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=36), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["sync_jobs.job_id"], ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','CHANGED','UNCHANGED','ERROR','SKIPPED','INTERRUPTED')",
            name="ck_sync_job_source_status",
        ),
        sa.PrimaryKeyConstraint("job_id", "source_id"),
    )
    op.create_index(
        "idx_sync_job_source_status", "sync_job_sources", ["job_id", "status"],
    )
    op.create_index(
        "idx_sync_job_source_source", "sync_job_sources", ["source_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_sync_job_source_source", table_name="sync_job_sources")
    op.drop_index("idx_sync_job_source_status", table_name="sync_job_sources")
    op.drop_table("sync_job_sources")
    op.drop_index("idx_sync_job_started", table_name="sync_jobs")
    op.drop_index("idx_sync_job_status", table_name="sync_jobs")
    op.drop_table("sync_jobs")
