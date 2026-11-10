import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import Timestamps, jsonb, pk, ts, uuid_fk

CONNECTOR_TYPES = (
    "api", "feed", "email_alert_parser", "public_preview_poller",
    "stream", "page_monitor", "share_to_bot", "discovery",
)
HEALTH_STATES = ("HEALTHY", "DEGRADED", "UNAVAILABLE", "RECOVERING", "DISABLED")


class SourceRegistry(Base, Timestamps):
    """One row per (source, acquisition route). Disabling a route leaves the others running."""

    __tablename__ = "source_registry"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(120))
    connector_type: Mapped[str] = mapped_column(String(40))
    category: Mapped[str | None] = mapped_column(String(40))  # funding / hiring / community
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    access_method: Mapped[str | None] = mapped_column(Text)  # e.g. "RSS", "Greenhouse Job Board API"
    docs_url: Mapped[str | None] = mapped_column(Text)  # official documentation verified for this route
    auth_type: Mapped[str] = mapped_column(String(30), server_default="none")
    capabilities: Mapped[dict] = jsonb()  # search, streaming, history, incremental_sync, public_feed
    retention_days: Mapped[int] = mapped_column(Integer, server_default=text("90"))
    rate_limit: Mapped[dict] = jsonb()
    cost_class: Mapped[str] = mapped_column(String(20), server_default="free")
    cost_per_read: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    paid_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    health_status: Mapped[str] = mapped_column(String(20), server_default="DISABLED")
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_success_at = ts(default_now=False)
    last_error_at = ts(default_now=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = jsonb()

    __table_args__ = (
        UniqueConstraint("name", "connector_type", name="uq_source_registry_name_route"),
        CheckConstraint(
            "connector_type in (%s)" % ",".join(f"'{t}'" for t in CONNECTOR_TYPES), name="connector_type"
        ),
        CheckConstraint(
            "health_status in (%s)" % ",".join(f"'{t}'" for t in HEALTH_STATES), name="health_status"
        ),
        # Zero-cost guard: a source that bills per read cannot exist unless explicitly approved.
        CheckConstraint("cost_per_read = 0 OR paid_approved", name="zero_cost_guard"),
    )


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = uuid_fk("source_registry.id")
    started_at = ts()
    finished_at = ts(default_now=False)
    status: Mapped[str] = mapped_column(String(20), server_default="running")  # running/ok/error/skipped
    items_fetched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    items_new: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_source_runs_source_started", "source_id", "started_at"),)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = uuid_fk("source_registry.id")
    scope: Mapped[str] = mapped_column(String(300), server_default="")  # e.g. board token, channel
    cursor: Mapped[dict] = jsonb()  # ETag, last id, Jetstream time_us, ...
    updated_at = ts()
    __table_args__ = (UniqueConstraint("source_id", "scope", name="uq_sync_cursors_source_scope"),)


class SourceAccount(Base, Timestamps):
    """A watched account/profile (founder, investor, recruiter) on a source."""

    __tablename__ = "source_accounts"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = uuid_fk("source_registry.id")
    handle: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    role: Mapped[str | None] = mapped_column(String(40))
    weight: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))
    evidence: Mapped[dict] = jsonb()  # why we believe it belongs to the company
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    __table_args__ = (UniqueConstraint("source_id", "handle", name="uq_source_accounts_source_handle"),)


class SourceCommunity(Base, Timestamps):
    __tablename__ = "source_communities"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = uuid_fk("source_registry.id")
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    __table_args__ = (UniqueConstraint("source_id", "name", name="uq_source_communities_source_name"),)


class SourceEvent(Base):
    """Normalized event. The five dates are deliberately separate columns."""

    __tablename__ = "source_events"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = uuid_fk("source_registry.id")
    source_event_id: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(60))
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # exact-duplicate filter
    published_at = ts(default_now=False)  # source publication time
    detected_at = ts()  # first detection time
    occurred_at = ts(default_now=False)  # event occurrence time
    announced_at = ts(default_now=False)  # announcement time
    verified_at = ts(default_now=False)  # verification time
    company_name: Mapped[str | None] = mapped_column(Text)
    company_domain: Mapped[str | None] = mapped_column(String(255))
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    source_account_id: Mapped[uuid.UUID | None] = uuid_fk(
        "source_accounts.id", nullable=True, ondelete="SET NULL"
    )
    author_name: Mapped[str | None] = mapped_column(Text)  # personal data: minimum, private
    author_url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))
    raw_metadata: Mapped[dict] = jsonb()
    permissions_metadata: Mapped[dict] = jsonb()
    processing_status: Mapped[str] = mapped_column(String(20), server_default="new")
    retention_expires_at = ts(default_now=False)
    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_source_events_source_event"),
        Index(
            "uq_source_events_canonical_url", "canonical_url", unique=True,
            postgresql_where=text("canonical_url is not null"),
        ),
        Index("ix_source_events_content_hash", "content_hash"),
        Index("ix_source_events_detected_at", "detected_at"),
        Index("ix_source_events_retention", "retention_expires_at"),
    )


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = pk()
    action: Mapped[str] = mapped_column(String(20))
    source_event_id: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    funding_round_id: Mapped[uuid.UUID | None] = uuid_fk("funding_rounds.id", nullable=True, ondelete="SET NULL")
    job_posting_id: Mapped[uuid.UUID | None] = uuid_fk("job_postings.id", nullable=True, ondelete="SET NULL")
    note: Mapped[str | None] = mapped_column(Text)
    created_at = ts()
    __table_args__ = (
        CheckConstraint(
            "action in ('useful','irrelevant','duplicate','incorrect','watch','mute','save','applied')",
            name="action",
        ),
    )

