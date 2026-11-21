"""Jobs track J1: job_postings gets source_id, company_name and a (source_id, external_job_id) key

(ats_board_id, external_job_id) cannot dedupe jobs from remote boards (the board is NULL), so the record is keyed by the route that listed it.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("source_id", UUID(as_uuid=True), nullable=True))
    op.add_column("job_postings", sa.Column("company_name", sa.Text(), nullable=True))
    op.create_foreign_key(op.f("fk_job_postings_source_id_source_registry"), "job_postings", "source_registry", ["source_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_job_postings_source_job", "job_postings", ["source_id", "external_job_id"])


def downgrade() -> None:
    op.drop_constraint("uq_job_postings_source_job", "job_postings", type_="unique")
    op.drop_constraint(op.f("fk_job_postings_source_id_source_registry"), "job_postings", type_="foreignkey")
    op.drop_column("job_postings", "company_name")
    op.drop_column("job_postings", "source_id")
