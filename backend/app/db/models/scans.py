import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import Timestamps, jsonb, pk, ts


class SavedSearch(Base, Timestamps):
    """J6: an Explore search the worker re-runs on an interval and reports new matches for (decision 110)."""

    __tablename__ = "saved_searches"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(120))
    filters: Mapped[dict] = jsonb()  # the Explore parameters: mode, q, exclude, location, source, days, sort, status
    interval_value: Mapped[int] = mapped_column(Integer)
    interval_unit: Mapped[str] = mapped_column(String(10))  # minutes | hours | days
    start_at: Mapped[datetime] = ts()  # the schedule's anchor: runs fall on start_at + k * interval
    timezone: Mapped[str] = mapped_column(String(60), server_default="Asia/Kolkata")  # display only; the schedule is UTC arithmetic
    status: Mapped[str] = mapped_column(String(10), server_default="active")  # active | paused
    watermark: Mapped[datetime] = ts()  # only jobs detected after this count as new; moves to each run's start
    last_run_at = ts(default_now=False)
    next_run_at = ts()
    lease_expires_at = ts(default_now=False)  # set while a worker holds the search, so two workers never run it twice
    __table_args__ = (
        CheckConstraint("interval_unit in ('minutes','hours','days')", name="interval_unit"),
        CheckConstraint("status in ('active','paused')", name="status"),
        CheckConstraint("interval_value > 0", name="interval_value"),
        Index("ix_saved_searches_due", "status", "next_run_at"),
    )


class SavedSearchRun(Base):
    __tablename__ = "saved_search_runs"
    id: Mapped[uuid.UUID] = pk()
    search_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("saved_searches.id", ondelete="CASCADE"))
    run_no: Mapped[int] = mapped_column(Integer)  # also the digest's dedup key part: a re-run after a crash reuses it
    started_at = ts()
    finished_at = ts(default_now=False)
    state: Mapped[str] = mapped_column(String(10))  # ok | error
    total_matches: Mapped[int] = mapped_column(Integer, server_default="0")
    new_matches: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("search_id", "run_no", name="uq_saved_search_runs_no"),
        CheckConstraint("state in ('ok','error')", name="state"),
        Index("ix_saved_search_runs_started", "started_at"),
    )
