"""Jobs track J5: job_postings keeps the lowest required years of experience the description states

Only the number is stored, never the description text (the board adapters keep title, location, team and link only).

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("years_min", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_postings", "years_min")
