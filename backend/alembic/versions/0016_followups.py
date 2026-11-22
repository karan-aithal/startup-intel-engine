"""Jobs track J8: follow-up log, pinned next date and stop flag on tracked jobs

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracked_jobs", sa.Column("next_pin_on", sa.Date()))
    op.add_column("tracked_jobs", sa.Column("reminders_stopped_at", sa.DateTime(timezone=True)))
    op.create_table(
        "followup_log",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tracked_job_id", UUID(as_uuid=True), sa.ForeignKey("tracked_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logged_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("channel", sa.String(40), nullable=False),
        sa.Column("contact", sa.String(300)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_followup_log_job", "followup_log", ["tracked_job_id", "logged_on"])


def downgrade() -> None:
    op.drop_table("followup_log")
    op.drop_column("tracked_jobs", "reminders_stopped_at")
    op.drop_column("tracked_jobs", "next_pin_on")
