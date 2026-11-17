"""Phase 8: adaptive schedule, activity score, circuit breaker, persisted rate limits

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_registry", sa.Column("next_poll_at", sa.DateTime(timezone=True)))
    op.add_column("source_registry", sa.Column("activity_score", sa.SmallInteger()))
    op.add_column("source_registry", sa.Column("circuit_open_until", sa.DateTime(timezone=True)))
    op.create_table(
        "rate_limits",
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("refilled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("window_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("rate_limits")
    op.drop_column("source_registry", "circuit_open_until")
    op.drop_column("source_registry", "activity_score")
    op.drop_column("source_registry", "next_poll_at")
