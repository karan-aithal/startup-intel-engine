import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import jsonb, pk, ts, uuid_fk


class LlmRun(Base):
    """One local-model call per (event, task): the validated result plus inference metrics."""

    __tablename__ = "llm_runs"
    id: Mapped[uuid.UUID] = pk()
    source_event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    task: Mapped[str] = mapped_column(String(30))  # funding / job
    model: Mapped[str] = mapped_column(String(80))
    result: Mapped[dict] = jsonb()  # validated and grounded output; {} when rejected
    valid: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    eval_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at = ts()
    __table_args__ = (UniqueConstraint("source_event_id", "task", name="uq_llm_runs_event_task"),)


class EventEmbedding(Base):
    __tablename__ = "event_embeddings"
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_events.id", ondelete="CASCADE"), primary_key=True)
    embedding = mapped_column(Vector(768))
    duplicate_of: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    created_at = ts()
    __table_args__ = (Index("ix_event_embeddings_hnsw", "embedding", postgresql_using="hnsw",
                            postgresql_ops={"embedding": "vector_cosine_ops"}),)
