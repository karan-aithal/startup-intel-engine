"""enable required extensions

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Normally created by scripts/db_setup.sh (superuser). IF NOT EXISTS makes this a no-op then.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    pass  # extensions are shared infrastructure; leave them in place
