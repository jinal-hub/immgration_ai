"""Add consultancy workflow: invitations, checklist, forms, document versioning.

Revision ID: e1d2c3b4a5f6
Revises: c3d4e5f6a7b8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1d2c3b4a5f6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_invitations",
        sa.Column("invitation_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('CONSULTANT', 'REVIEWER', 'APPLICANT')",
            name="ck_tenant_invitation_role",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["consultancy_tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("invitation_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("idx_invitation_tenant_email", "tenant_invitations", ["tenant_id", "email"])
    op.create_index("idx_invitation_token", "tenant_invitations", ["token_hash"])

    op.create_table(
        "case_checklist_items",
        sa.Column("item_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("case_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("requirement_key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="PENDING"),
        sa.Column("evidence_document_id", sa.UUID(as_uuid=False)),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("reviewed_by", sa.UUID(as_uuid=False)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','SUBMITTED','ACCEPTED','REJECTED','WAIVED')",
            name="ck_case_checklist_status",
        ),
        sa.CheckConstraint(
            "category IN ('document','information','form')",
            name="ck_case_checklist_category",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["consultancy_tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            name="fk_case_checklist_case_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"], ["case_documents.document_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["reviewed_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("case_id", "requirement_key", name="uq_case_checklist_requirement"),
    )
    op.create_index("idx_case_checklist_case", "case_checklist_items", ["case_id", "status"])

    op.create_table(
        "form_templates",
        sa.Column("template_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("form_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("program_id", sa.String(length=32)),
        sa.Column("schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PUBLISHED"),
        sa.Column("created_by", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["program_id"], ["immigration_programs.program_id"]),
        sa.ForeignKeyConstraint(["created_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("template_id"),
        sa.UniqueConstraint("form_key", "version", name="uq_form_template_key_version"),
    )
    op.create_index("idx_form_template_program", "form_templates", ["program_id"])

    op.create_table(
        "form_answers",
        sa.Column("answer_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("case_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("template_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("answers_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="DRAFT"),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("applicant_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by", sa.UUID(as_uuid=False)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','SUBMITTED','REVIEWED','APPROVED')",
            name="ck_form_answer_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["consultancy_tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            name="fk_form_answer_case_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["template_id"], ["form_templates.template_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["api_users.user_id"]),
        sa.PrimaryKeyConstraint("answer_id"),
        sa.UniqueConstraint("case_id", "template_id", name="uq_form_answer_case_template"),
    )
    op.create_index("idx_form_answer_case", "form_answers", ["case_id", "status"])

    with op.batch_alter_table("application_cases") as batch_op:
        batch_op.add_column(
            sa.Column("assessment_id", sa.UUID(as_uuid=False))
        )
        batch_op.add_column(
            sa.Column("ruleset_id", sa.String(length=36))
        )
        batch_op.create_foreign_key(
            "fk_application_case_assessment",
            "assessments",
            ["assessment_id"],
            ["assessment_id"],
        )
        batch_op.create_foreign_key(
            "fk_application_case_ruleset",
            "rulesets",
            ["ruleset_id"],
            ["ruleset_id"],
        )

    with op.batch_alter_table("case_documents") as batch_op:
        batch_op.add_column(
            sa.Column("checklist_item_id", sa.UUID(as_uuid=False))
        )
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("replaces_document_id", sa.UUID(as_uuid=False))
        )
        batch_op.add_column(
            sa.Column("scanner_status", sa.String(length=24), nullable=False, server_default="NOT_CONFIGURED")
        )
        batch_op.add_column(
            sa.Column("scanner_detail", sa.Text())
        )
        batch_op.add_column(
            sa.Column("rejection_reason", sa.Text())
        )
        batch_op.add_column(
            sa.Column("reviewed_by", sa.UUID(as_uuid=False))
        )
        batch_op.add_column(
            sa.Column("reviewed_at", sa.DateTime(timezone=True))
        )
        batch_op.create_foreign_key(
            "fk_case_document_checklist_item",
            "case_checklist_items",
            ["checklist_item_id"],
            ["item_id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_case_document_replaces",
            "case_documents",
            ["replaces_document_id"],
            ["document_id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_case_document_reviewed_by",
            "api_users",
            ["reviewed_by"],
            ["user_id"],
        )
        batch_op.create_check_constraint(
            "ck_case_document_scanner_status",
            "scanner_status IN ('NOT_CONFIGURED','PENDING','CLEAN','INFECTED','ERROR')",
        )


def downgrade() -> None:
    with op.batch_alter_table("case_documents") as batch_op:
        batch_op.drop_constraint("ck_case_document_scanner_status", type_="check")
        batch_op.drop_constraint("fk_case_document_reviewed_by", type_="foreignkey")
        batch_op.drop_constraint("fk_case_document_replaces", type_="foreignkey")
        batch_op.drop_constraint("fk_case_document_checklist_item", type_="foreignkey")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("rejection_reason")
        batch_op.drop_column("scanner_detail")
        batch_op.drop_column("scanner_status")
        batch_op.drop_column("replaces_document_id")
        batch_op.drop_column("version")
        batch_op.drop_column("checklist_item_id")

    with op.batch_alter_table("application_cases") as batch_op:
        batch_op.drop_constraint("fk_application_case_ruleset", type_="foreignkey")
        batch_op.drop_constraint("fk_application_case_assessment", type_="foreignkey")
        batch_op.drop_column("ruleset_id")
        batch_op.drop_column("assessment_id")

    op.drop_index("idx_form_answer_case", table_name="form_answers")
    op.drop_table("form_answers")
    op.drop_index("idx_form_template_program", table_name="form_templates")
    op.drop_table("form_templates")
    op.drop_index("idx_case_checklist_case", table_name="case_checklist_items")
    op.drop_table("case_checklist_items")
    op.drop_index("idx_invitation_token", table_name="tenant_invitations")
    op.drop_index("idx_invitation_tenant_email", table_name="tenant_invitations")
    op.drop_table("tenant_invitations")
