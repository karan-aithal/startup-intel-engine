"""LLM attempts, claim staging, event relationships, ATS/source persistence, outbox resend flag

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UUID_PK = dict(server_default=sa.text("gen_random_uuid()"), nullable=False)
NOW = dict(server_default=sa.func.now(), nullable=False)


def _event_fk(col="source_event_id", **kw):
    return sa.Column(col, sa.UUID(), sa.ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False, **kw)


def upgrade() -> None:
    op.drop_column("event_embeddings", "duplicate_of")
    op.create_table(
        "llm_attempts", sa.Column("id", sa.UUID(), **UUID_PK), _event_fk(),
        sa.Column("task", sa.String(30), nullable=False), sa.Column("model", sa.String(80), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False), sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("raw_output", sa.Text()), sa.Column("error", sa.Text()), sa.Column("latency_ms", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()), sa.Column("created_at", sa.DateTime(timezone=True), **NOW),
        sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_llm_attempts_event", "llm_attempts", ["source_event_id"])
    op.create_table(
        "event_relationships", sa.Column("id", sa.UUID(), **UUID_PK), _event_fk("event_id"), _event_fk("related_event_id"),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("score", sa.Numeric(5, 4)),
        sa.Column("method", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), **NOW),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("event_id", "related_event_id", name="uq_event_relationships_pair"),
        sa.CheckConstraint("kind in ('same_event','syndicated_copy','follow_up')", name="ck_event_relationships_kind"))
    op.create_table(
        "funding_round_claims", sa.Column("id", sa.UUID(), **UUID_PK), _event_fk(),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("company_name", sa.Text()), sa.Column("stage", sa.String(40)), sa.Column("amount_value", sa.Numeric(24, 2)),
        sa.Column("currency", sa.String(8)), sa.Column("amount_text", sa.Text()),
        sa.Column("is_rumour", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(12), server_default="unverified", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **NOW), sa.Column("updated_at", sa.DateTime(timezone=True), **NOW),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("source_event_id", name="uq_funding_round_claims_event"),
        sa.CheckConstraint("status in ('unverified','rejected','verified')", name="ck_funding_round_claims_status"))
    for table, extra, uq in (("company_investor_claims", [sa.Column("role", sa.String(20))], "uq_investor_claims"),
                             ("company_founder_claims", [], "uq_founder_claims")):
        op.create_table(
            table, sa.Column("id", sa.UUID(), **UUID_PK), _event_fk(),
            sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.Text(), nullable=False), sa.Column("canonical_name", sa.Text(), nullable=False),
            sa.Column("evidence_quote", sa.Text()), sa.Column("origin", sa.String(10), nullable=False),
            sa.Column("status", sa.String(12), server_default="unverified", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), **NOW), *extra,
            sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("company_id", "source_event_id", "canonical_name", name=uq))
    for col in (
        sa.Column("evidence", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("verification_status", sa.String(12), server_default="unverified", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)), sa.Column("last_job_count", sa.Integer()),
        sa.Column("promoted_at", sa.DateTime(timezone=True)), sa.Column("promotion_reason", sa.String(60)),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("ats_boards", col)
    for name in ("verified_at", "last_polled_at", "next_poll_at"):
        op.add_column("company_sources", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column("alert_deliveries", sa.Column("possible_duplicate", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_index("uq_discovery_candidates_event", "company_discovery_candidates", ["source_event_id"], unique=True,
                    postgresql_where=sa.text("source_event_id is not null"))


def downgrade() -> None:
    op.drop_index("uq_discovery_candidates_event", "company_discovery_candidates")
    op.drop_column("alert_deliveries", "possible_duplicate")
    for name in ("verified_at", "last_polled_at", "next_poll_at"):
        op.drop_column("company_sources", name)
    for name in ("evidence", "verification_status", "verified_at", "last_job_count", "promoted_at", "promotion_reason", "next_poll_at"):
        op.drop_column("ats_boards", name)
    for t in ("company_founder_claims", "company_investor_claims", "funding_round_claims", "event_relationships", "llm_attempts"):
        op.drop_table(t)
    op.add_column("event_embeddings", sa.Column("duplicate_of", sa.UUID()))
