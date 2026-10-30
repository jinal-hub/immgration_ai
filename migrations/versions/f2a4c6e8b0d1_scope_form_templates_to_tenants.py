"""Scope consultancy-created form templates to their tenant.

Revision ID: f2a4c6e8b0d1
Revises: e1d2c3b4a5f6
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a4c6e8b0d1"
down_revision: str | Sequence[str] | None = "e1d2c3b4a5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("form_templates") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.UUID(as_uuid=False)))
        batch_op.create_foreign_key(
            "fk_form_template_tenant",
            "consultancy_tenants",
            ["tenant_id"],
            ["tenant_id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_form_template_key_version", type_="unique")
        batch_op.create_unique_constraint(
            "uq_form_template_tenant_key_version",
            ["tenant_id", "form_key", "version"],
        )

    op.create_index(
        "idx_form_template_tenant_program",
        "form_templates",
        ["tenant_id", "program_id"],
    )
    op.create_index(
        "uq_form_template_global_key_version",
        "form_templates",
        ["form_key", "version"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_form_template_global_key_version",
        table_name="form_templates",
    )
    op.drop_index(
        "idx_form_template_tenant_program",
        table_name="form_templates",
    )
    with op.batch_alter_table("form_templates") as batch_op:
        batch_op.drop_constraint(
            "uq_form_template_tenant_key_version",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_form_template_key_version",
            ["form_key", "version"],
        )
        batch_op.drop_constraint("fk_form_template_tenant", type_="foreignkey")
        batch_op.drop_column("tenant_id")
