"""Phase 7: claim-to-round link, scores, idempotent signals and evidence

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("funding_round_claims", sa.Column("funding_round_id", sa.UUID(), sa.ForeignKey("funding_rounds.id", ondelete="SET NULL")))
    op.add_column("funding_rounds", sa.Column("score_global", sa.SmallInteger()))
    op.add_column("funding_rounds", sa.Column("score_india", sa.SmallInteger()))
    op.add_column("companies", sa.Column("emerging_global", sa.SmallInteger()))
    op.add_column("companies", sa.Column("emerging_india", sa.SmallInteger()))
    op.add_column("company_signals", sa.Column("signal_key", sa.Text()))
    op.create_unique_constraint("uq_company_signals_key", "company_signals", ["company_id", "signal_type", "signal_key"])
    op.create_index("uq_evidence_round_event", "evidence", ["funding_round_id", "source_event_id"], unique=True,
                    postgresql_where=sa.text("funding_round_id is not null and source_event_id is not null"))


def downgrade() -> None:
    op.drop_index("uq_evidence_round_event", "evidence")
    op.drop_constraint("uq_company_signals_key", "company_signals")
    op.drop_column("company_signals", "signal_key")
    op.drop_column("companies", "emerging_india")
    op.drop_column("companies", "emerging_global")
    op.drop_column("funding_rounds", "score_india")
    op.drop_column("funding_rounds", "score_global")
    op.drop_column("funding_round_claims", "funding_round_id")
