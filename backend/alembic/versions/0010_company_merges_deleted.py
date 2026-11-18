"""Phase 13: company_merges.deleted keeps a snapshot of rows dropped while duplicate rounds collapse

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_merges", sa.Column("deleted", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column("company_merges", "deleted")
