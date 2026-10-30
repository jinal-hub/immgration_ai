"""Add consultancy tenants, memberships, and application cases.

Revision ID: b2c3d4e5f6a7
Revises: a7b8c9d0e1f2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "consultancy_tenants",
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED')",
            name="ck_consultancy_tenant_status",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("membership_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('OWNER','CONSULTANT','REVIEWER','APPLICANT')",
            name="ck_tenant_membership_role",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','REVOKED')",
            name="ck_tenant_membership_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["consultancy_tenants.tenant_id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_member_user"),
    )
    op.create_index(
        "idx_tenant_membership_user",
        "tenant_memberships",
        ["user_id", "status"],
    )
    op.create_index(
        "uq_tenant_active_owner",
        "tenant_memberships",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("role = 'OWNER' AND status = 'ACTIVE'"),
        sqlite_where=sa.text("role = 'OWNER' AND status = 'ACTIVE'"),
    )

    op.create_table(
        "application_cases",
        sa.Column("case_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("applicant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("assigned_consultant_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','IN_PROGRESS','NEEDS_INFO','READY_FOR_REVIEW',"
            "'CHANGES_REQUESTED','APPROVED','PACKAGE_READY','CLOSED')",
            name="ck_application_case_status",
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"], ["applicant_profiles.applicant_id"],
        ),
        sa.ForeignKeyConstraint(["created_by"], ["api_users.user_id"]),
        sa.ForeignKeyConstraint(
            ["assigned_consultant_id"], ["api_users.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["immigration_programs.program_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["consultancy_tenants.tenant_id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("case_id"),
    )
    op.create_index(
        "idx_case_tenant_status", "application_cases", ["tenant_id", "status"],
    )
    op.create_index(
        "idx_case_applicant", "application_cases", ["applicant_id"],
    )
    op.create_index(
        "idx_case_assigned_consultant",
        "application_cases",
        ["assigned_consultant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_case_assigned_consultant", table_name="application_cases")
    op.drop_index("idx_case_applicant", table_name="application_cases")
    op.drop_index("idx_case_tenant_status", table_name="application_cases")
    op.drop_table("application_cases")
    op.drop_index("uq_tenant_active_owner", table_name="tenant_memberships")
    op.drop_index("idx_tenant_membership_user", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
    op.drop_table("consultancy_tenants")
