"""Jobs track J6: saved searches and their run history

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("filters", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("interval_value", sa.Integer(), nullable=False),
        sa.Column("interval_unit", sa.String(10), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("timezone", sa.String(60), server_default="Asia/Kolkata", nullable=False),
        sa.Column("status", sa.String(10), server_default="active", nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("interval_unit in ('minutes','hours','days')", name=op.f("ck_saved_searches_interval_unit")),
        sa.CheckConstraint("status in ('active','paused')", name=op.f("ck_saved_searches_status")),
        sa.CheckConstraint("interval_value > 0", name=op.f("ck_saved_searches_interval_value")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_saved_searches_due", "saved_searches", ["status", "next_run_at"])
    op.create_table(
        "saved_search_runs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("search_id", UUID(as_uuid=True), sa.ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("total_matches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_matches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("search_id", "run_no", name="uq_saved_search_runs_no"),
        sa.CheckConstraint("state in ('ok','error')", name=op.f("ck_saved_search_runs_state")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_saved_search_runs_started", "saved_search_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("saved_search_runs")
    op.drop_table("saved_searches")
