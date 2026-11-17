"""Mark investor/founder rows written by Phase 7 verification so they can be reconciled (removed) when their round loses verification

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_investors", sa.Column("promoted_by", sa.String(20)))
    op.add_column("founders", sa.Column("promoted_by", sa.String(20)))
    # rows whose proof is a funding-round evidence row can only have come from verification
    for t in ("company_investors", "founders"):
        op.execute(f"""UPDATE {t} SET promoted_by = 'phase7' WHERE evidence_id IN (SELECT id FROM evidence WHERE funding_round_id IS NOT NULL)""")


def downgrade() -> None:
    op.drop_column("founders", "promoted_by")
    op.drop_column("company_investors", "promoted_by")
