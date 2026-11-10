import uuid

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import jsonb, pk, ts, uuid_fk

JOB_STATES = ("pending", "running", "done", "failed", "dead")
DELIVERY_STATES = ("pending", "sending", "sent", "failed", "dead")


class JobQueue(Base):
    """Durable queue claimed with FOR UPDATE SKIP LOCKED. 'dead' is the dead-letter state."""

    __tablename__ = "job_queue"
    id: Mapped[uuid.UUID] = pk()
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = jsonb()
    idempotency_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))  # lower runs first
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    run_at = ts()
    locked_by: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at = ts(default_now=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at = ts()
    finished_at = ts(default_now=False)
    __table_args__ = (
        UniqueConstraint("kind", "idempotency_key", name="uq_job_queue_kind_key"),
        CheckConstraint("status in (%s)" % ",".join(f"'{s}'" for s in JOB_STATES), name="status"),
        Index("ix_job_queue_claim", "status", "priority", "run_at"),
    )


class AlertDelivery(Base):
    """Transactional outbox: written in the same transaction as the event that caused it."""

    __tablename__ = "alert_deliveries"
    id: Mapped[uuid.UUID] = pk()
    channel: Mapped[str] = mapped_column(String(20), server_default="telegram")
    topic: Mapped[str | None] = mapped_column(String(60))
    payload: Mapped[dict] = jsonb()
    dedup_key: Mapped[str] = mapped_column(String(300))
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    funding_round_id: Mapped[uuid.UUID | None] = uuid_fk("funding_rounds.id", nullable=True, ondelete="SET NULL")
    job_posting_id: Mapped[uuid.UUID | None] = uuid_fk("job_postings.id", nullable=True, ondelete="SET NULL")
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("8"))
    next_attempt_at = ts()
    lease_expires_at = ts(default_now=False)
    external_message_id: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at = ts()
    sent_at = ts(default_now=False)
    __table_args__ = (
        UniqueConstraint("channel", "dedup_key", name="uq_alert_deliveries_channel_key"),
        CheckConstraint("status in (%s)" % ",".join(f"'{s}'" for s in DELIVERY_STATES), name="status"),
        Index("ix_alert_deliveries_claim", "status", "next_attempt_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_name: Mapped[str] = mapped_column(String(120), primary_key=True)
    last_seen_at = ts()
    started_at = ts()
    info: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


