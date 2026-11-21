"""Jobs track J4: job_postings keeps the ATS workplace type (remote | hybrid | onsite)

The event loses raw_metadata at retention, so the durable job record needs its own copy for Portals counts to match Explore.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("workplace", sa.String(10), nullable=True))
    op.execute("""UPDATE job_postings j SET workplace = e.raw_metadata->>'workplace' FROM source_events e
        WHERE j.source_event_id = e.id AND e.raw_metadata->>'workplace' IN ('remote', 'hybrid', 'onsite')""")


def downgrade() -> None:
    op.drop_column("job_postings", "workplace")
