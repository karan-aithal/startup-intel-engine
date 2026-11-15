"""llm_runs and event_embeddings

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_event_id", sa.UUID(), nullable=False),
        sa.Column("task", sa.String(30), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("result", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("valid", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("eval_tokens", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["source_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_event_id", "task", name="uq_llm_runs_event_task"),
    )
    op.create_table(
        "event_embeddings",
        sa.Column("source_event_id", sa.UUID(), nullable=False),
        sa.Column("embedding", Vector(768)),
        sa.Column("duplicate_of", sa.UUID()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["source_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["duplicate_of"], ["source_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("source_event_id"),
    )
    op.create_index("ix_event_embeddings_hnsw", "event_embeddings", ["embedding"], postgresql_using="hnsw",
                    postgresql_ops={"embedding": "vector_cosine_ops"})


def downgrade() -> None:
    op.drop_table("event_embeddings")
    op.drop_table("llm_runs")
