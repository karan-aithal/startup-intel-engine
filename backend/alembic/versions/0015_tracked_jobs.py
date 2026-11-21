"""Jobs track J7: tracked jobs (the pipeline) and their status history

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NOW = sa.func.now()
STATES = ("saved", "applied", "responded", "assessment", "interview", "offer", "hired", "rejected", "withdrawn", "skipped")


def upgrade() -> None:
    op.create_table(
        "tracked_jobs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("source_registry.id", ondelete="SET NULL")),
        sa.Column("external_job_id", sa.String(200), nullable=False),
        sa.Column("job_posting_id", UUID(as_uuid=True), sa.ForeignKey("job_postings.id", ondelete="SET NULL")),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("company_name", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("contact", sa.String(300)),
        sa.Column("salary_text", sa.String(300)),
        sa.Column("next_step", sa.String(300)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.UniqueConstraint("source_id", "external_job_id", name="uq_tracked_jobs_job"),
        sa.CheckConstraint("status in (%s)" % ",".join(f"'{s}'" for s in STATES), name=op.f("ck_tracked_jobs_status")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tracked_jobs_status", "tracked_jobs", ["status", "status_changed_at"])
    op.create_table(
        "tracked_job_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tracked_job_id", UUID(as_uuid=True), sa.ForeignKey("tracked_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(12)),
        sa.Column("to_status", sa.String(12), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tracked_job_events_job", "tracked_job_events", ["tracked_job_id", "at"])


def downgrade() -> None:
    op.drop_table("tracked_job_events")
    op.drop_table("tracked_jobs")
