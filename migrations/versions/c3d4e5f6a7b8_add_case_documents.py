"""Add private case-document metadata and tenant-bound storage references.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_case_tenant", "application_cases", ["case_id", "tenant_id"],
    )
    op.create_table(
        "case_documents",
        sa.Column("document_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("case_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("uploaded_by", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=256), nullable=False),
        sa.Column("review_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_bytes > 0", name="ck_case_document_size"),
        sa.CheckConstraint(
            "review_status IN ('PENDING_REVIEW','ACCEPTED','REJECTED')",
            name="ck_case_document_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            name="fk_case_document_case_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "idx_case_document_tenant_case",
        "case_documents",
        ["tenant_id", "case_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_case_document_tenant_case", table_name="case_documents")
    op.drop_table("case_documents")
    op.drop_constraint("uq_case_tenant", "application_cases", type_="unique")
