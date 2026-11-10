import uuid
from datetime import datetime

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column


# Native Postgres UUIDs generated server-side (gen_random_uuid is built in since PG13).
def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def uuid_fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE"):
    from sqlalchemy import ForeignKey

    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable)


def ts(*, default_now: bool = True, nullable: bool | None = None):
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now() if default_now else None,
        nullable=(not default_now) if nullable is None else nullable,
    )


def jsonb():
    return mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)


class Timestamps:
    created_at: Mapped[datetime] = ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
