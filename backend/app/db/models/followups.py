import uuid
from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import pk, ts


class FollowupLog(Base):
    """J8: a follow-up the owner made (they did it elsewhere; nothing here sends). `status` is the job's status when it was logged, so each
    status counts its own follow-ups. Pinned next date and the stop flag live on tracked_jobs."""

    __tablename__ = "followup_log"
    id: Mapped[uuid.UUID] = pk()
    tracked_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracked_jobs.id", ondelete="CASCADE"))
    logged_on: Mapped[date] = mapped_column(Date)  # the day the follow-up happened (Asia/Kolkata calendar day), chosen by the owner
    status: Mapped[str] = mapped_column(String(12))
    channel: Mapped[str] = mapped_column(String(40))
    contact: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts()
    __table_args__ = (Index("ix_followup_log_job", "tracked_job_id", "logged_on"),)
