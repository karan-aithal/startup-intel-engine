import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    created_at = ts()
    __table_args__ = (Index("ix_event_embeddings_hnsw", "embedding", postgresql_using="hnsw",
                            postgresql_ops={"embedding": "vector_cosine_ops"}),)


class LlmAttempt(Base):
    """Every model call, successful or not (timeouts and server errors included)."""

    __tablename__ = "llm_attempts"
    id: Mapped[uuid.UUID] = pk()
    source_event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    task: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(80))
    attempt_no: Mapped[int] = mapped_column(SmallInteger)
    outcome: Mapped[str] = mapped_column(String(20))  # ok / invalid_json / schema_error / grounding_empty / timeout / http_error
    raw_output: Mapped[str | None] = mapped_column(Text)  # truncated to 2 KB
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at = ts()
    __table_args__ = (Index("ix_llm_attempts_event", "source_event_id"),)


class EventRelationship(Base):
    """Two events that describe the same thing. The earlier event is `related_event_id`."""

    __tablename__ = "event_relationships"
    id: Mapped[uuid.UUID] = pk()
    event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    related_event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    kind: Mapped[str] = mapped_column(String(20))
    score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    method: Mapped[str] = mapped_column(String(20))  # sync_rule / semantic
    created_at = ts()
    __table_args__ = (
        UniqueConstraint("event_id", "related_event_id", name="uq_event_relationships_pair"),
        CheckConstraint("kind in ('same_event','syndicated_copy','follow_up')", name="kind"),
    )


class FundingRoundClaim(Base):
    """Unverified funding facts from one article. Phase 7 verifies; only then do they become funding_rounds."""

    __tablename__ = "funding_round_claims"
    id: Mapped[uuid.UUID] = pk()
    source_event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    company_name: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(String(40))
    amount_value: Mapped[float | None] = mapped_column(Numeric(24, 2))  # in the original currency
    currency: Mapped[str | None] = mapped_column(String(8))
    amount_text: Mapped[str | None] = mapped_column(Text)
    is_rumour: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    origin: Mapped[str] = mapped_column(String(10))  # llm / regex / merged
    evidence: Mapped[dict] = jsonb()  # per-field quotes and grounding status, plus conflicting alternatives
    status: Mapped[str] = mapped_column(String(12), server_default="unverified")
    funding_round_id: Mapped[uuid.UUID | None] = uuid_fk("funding_rounds.id", nullable=True, ondelete="SET NULL")  # set by Phase 7
    created_at = ts()
    updated_at = ts()
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_funding_round_claims_event"),
        CheckConstraint("status in ('unverified','rejected','verified')", name="status"),
    )


class _PersonClaim:
    id: Mapped[uuid.UUID] = pk()
    source_event_id: Mapped[uuid.UUID] = uuid_fk("source_events.id")
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    name: Mapped[str] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), server_default="unverified")
    created_at = ts()


class CompanyInvestorClaim(_PersonClaim, Base):
    __tablename__ = "company_investor_claims"
    role: Mapped[str | None] = mapped_column(String(20))  # lead / participant
    __table_args__ = (UniqueConstraint("company_id", "source_event_id", "canonical_name", name="uq_investor_claims"),)


class CompanyFounderClaim(_PersonClaim, Base):
    __tablename__ = "company_founder_claims"
    __table_args__ = (UniqueConstraint("company_id", "source_event_id", "canonical_name", name="uq_founder_claims"),)
