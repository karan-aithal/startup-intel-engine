"""Phase 13: audit table for company merges, renames and exclusions; companies.excluded_reason

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("excluded_reason", sa.Text()))
    op.create_table(
        "company_merges",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("companies.id")),
        sa.Column("old_name", sa.Text(), nullable=False),
        sa.Column("old_canonical_name", sa.Text(), nullable=False),
        sa.Column("new_name", sa.Text()),
        sa.Column("decision", sa.Text()),
        sa.Column("moved", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("left_behind", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("action in ('merge','rename','exclude')", name=op.f("ck_company_merges_action")),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("company_merges")
    op.drop_column("companies", "excluded_reason")
