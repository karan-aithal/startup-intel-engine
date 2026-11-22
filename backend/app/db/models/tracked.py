import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import Timestamps, pk, ts, uuid_fk

STATES = ("saved", "applied", "responded", "assessment", "interview", "offer", "hired", "rejected", "withdrawn", "skipped")


class TrackedJob(Base, Timestamps):
    """J7: a job the owner saved, skipped or is pursuing (decision 111). It keeps its own copy of title, company, link and location, so it still
    reads after retention scrubs the event (decision 75). The key (source_id, external_job_id) is the job's identity, the same pair as job_postings."""

    __tablename__ = "tracked_jobs"
    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID | None] = uuid_fk("source_registry.id", nullable=True, ondelete="SET NULL")
    external_job_id: Mapped[str] = mapped_column(String(200))
    job_posting_id: Mapped[uuid.UUID | None] = uuid_fk("job_postings.id", nullable=True, ondelete="SET NULL")  # the J1 record, when there is one
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    company_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12))
    notes: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(300))
    salary_text: Mapped[str | None] = mapped_column(String(300))
    next_step: Mapped[str | None] = mapped_column(String(300))
    applied_at = ts(default_now=False)  # set when the job first becomes "applied"; drives J8
    status_changed_at = ts()
    next_pin_on: Mapped[date | None] = mapped_column(Date)  # J8: a pinned next follow-up date; it overrides the cadence until a follow-up is logged
    reminders_stopped_at = ts(default_now=False)  # J8: set = no reminders for this job; the next logged follow-up clears it
    __table_args__ = (
        UniqueConstraint("source_id", "external_job_id", name="uq_tracked_jobs_job"),
        CheckConstraint("status in (%s)" % ",".join(f"'{s}'" for s in STATES), name="status"),
        Index("ix_tracked_jobs_status", "status", "status_changed_at"),
    )


class TrackedJobEvent(Base):
    """Every status change, newest last. from_status is NULL for the first (the job was saved or skipped from Explore, or imported)."""

    __tablename__ = "tracked_job_events"
    id: Mapped[uuid.UUID] = pk()
    tracked_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracked_jobs.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(12))
    to_status: Mapped[str] = mapped_column(String(12))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = ts()
    __table_args__ = (Index("ix_tracked_job_events_job", "tracked_job_id", "at"),)
