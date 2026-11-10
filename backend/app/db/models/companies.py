import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import Timestamps, jsonb, pk, ts, uuid_fk

VERIFICATION_STATES = (
    "DISCOVERED", "UNVERIFIED", "PARTIALLY_VERIFIED", "VERIFIED", "CONTRADICTED", "EXPIRED",
)


class Company(Base, Timestamps):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text)  # lower-cased, punctuation stripped
    website_url: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))
    hq_city: Mapped[str | None] = mapped_column(String(120))
    is_india_linked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    hot_until = ts(default_now=False)  # newly funded: polled more often for 90 days
    watched: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    muted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    meta: Mapped[dict] = jsonb()
    __table_args__ = (
        Index("ix_companies_canonical_name_trgm", "canonical_name",
              postgresql_using="gin", postgresql_ops={"canonical_name": "gin_trgm_ops"}),
    )


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    alias: Mapped[str] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(SmallInteger, server_default=text("100"))
    created_at = ts()
    __table_args__ = (UniqueConstraint("company_id", "alias", name="uq_company_aliases_company_alias"),)


class CompanyDomain(Base):
    __tablename__ = "company_domains"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    evidence_id: Mapped[uuid.UUID | None] = uuid_fk("evidence.id", nullable=True, ondelete="SET NULL")
    created_at = ts()


class Founder(Base, Timestamps):
    __tablename__ = "founders"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(String(80))
    profile_url: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[uuid.UUID | None] = uuid_fk("evidence.id", nullable=True, ondelete="SET NULL")
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_founders_company_name"),)


class Investor(Base, Timestamps):
    __tablename__ = "investors"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str | None] = mapped_column(String(40))  # vc / angel_network / accelerator / angel
    website_url: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))


class FundingRound(Base, Timestamps):
    __tablename__ = "funding_rounds"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    stage: Mapped[str | None] = mapped_column(String(40))
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    announced_at = ts(default_now=False)
    occurred_at = ts(default_now=False)
    first_detected_at = ts()
    verified_at = ts(default_now=False)
    verification_status: Mapped[str] = mapped_column(String(24), server_default="DISCOVERED")
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    is_rumour: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    risk_flags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Set only when evidence is sufficient to say two reports describe the same round.
    identity_key: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        Index("uq_funding_rounds_identity_key", "identity_key", unique=True,
              postgresql_where=text("identity_key is not null")),
        CheckConstraint(
            "verification_status in (%s)" % ",".join(f"'{s}'" for s in VERIFICATION_STATES),
            name="verification_status",
        ),
        CheckConstraint("confidence is null or confidence between 0 and 100", name="confidence_range"),
    )


class CompanyInvestor(Base):
    __tablename__ = "company_investors"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    investor_id: Mapped[uuid.UUID] = uuid_fk("investors.id")
    funding_round_id: Mapped[uuid.UUID | None] = uuid_fk("funding_rounds.id", nullable=True)
    role: Mapped[str | None] = mapped_column(String(20))  # lead / participant / existing
    evidence_id: Mapped[uuid.UUID | None] = uuid_fk("evidence.id", nullable=True, ondelete="SET NULL")
    __table_args__ = (
        UniqueConstraint("company_id", "investor_id", "funding_round_id",
                         name="uq_company_investors_triple", postgresql_nulls_not_distinct=True),
    )


class AtsBoard(Base, Timestamps):
    __tablename__ = "ats_boards"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    provider: Mapped[str] = mapped_column(String(40))  # greenhouse / lever / ashby / ...
    board_token: Mapped[str] = mapped_column(String(200))
    board_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    discovered_via: Mapped[str | None] = mapped_column(String(60))
    last_polled_at = ts(default_now=False)
    last_success_at = ts(default_now=False)
    __table_args__ = (UniqueConstraint("provider", "board_token", name="uq_ats_boards_provider_token"),)


class JobPosting(Base, Timestamps):
    __tablename__ = "job_postings"
    id: Mapped[uuid.UUID] = pk()
    ats_board_id: Mapped[uuid.UUID | None] = uuid_fk("ats_boards.id", nullable=True, ondelete="SET NULL")
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    source_event_id: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    external_job_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))
    is_remote: Mapped[bool | None] = mapped_column(Boolean)
    department: Mapped[str | None] = mapped_column(Text)
    role_category: Mapped[str | None] = mapped_column(String(60))
    seniority: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), server_default="open")  # open / closed
    posted_at = ts(default_now=False)
    first_seen_at = ts()
    last_seen_at = ts()
    closed_at = ts(default_now=False)
    __table_args__ = (
        UniqueConstraint("ats_board_id", "external_job_id", name="uq_job_postings_board_job"),
        Index("ix_job_postings_company", "company_id"),
    )


class CompanySignal(Base):
    __tablename__ = "company_signals"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    signal_type: Mapped[str] = mapped_column(String(60))  # new_office, launch, layoffs, ...
    source_event_id: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    occurred_at = ts()
    data: Mapped[dict] = jsonb()
    __table_args__ = (Index("ix_company_signals_company_time", "company_id", "occurred_at"),)


class Evidence(Base):
    """Evidence lives in its own table; one funding event can have many evidence rows."""

    __tablename__ = "evidence"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True)
    funding_round_id: Mapped[uuid.UUID | None] = uuid_fk("funding_rounds.id", nullable=True)
    job_posting_id: Mapped[uuid.UUID | None] = uuid_fk("job_postings.id", nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    evidence_type: Mapped[str] = mapped_column(String(40))
    weight: Mapped[int] = mapped_column(SmallInteger)  # ranking weight, not a probability
    url: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    syndication_group: Mapped[str | None] = mapped_column(String(64))  # copies share a group
    supports: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))  # false = contradicts
    created_at = ts()
    __table_args__ = (
        CheckConstraint("weight between 0 and 100", name="weight_range"),
        CheckConstraint(
            "company_id is not null or funding_round_id is not null or job_posting_id is not null",
            name="has_target",
        ),
    )


class CompanySource(Base, Timestamps):
    """Feeds/profiles discovered for a company, with the evidence tying them to it."""

    __tablename__ = "company_sources"
    id: Mapped[uuid.UUID] = pk()
    company_id: Mapped[uuid.UUID] = uuid_fk("companies.id")
    source_id: Mapped[uuid.UUID | None] = uuid_fk("source_registry.id", nullable=True, ondelete="SET NULL")
    kind: Mapped[str] = mapped_column(String(40))  # blog_feed / youtube / github / mastodon / careers
    url: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = jsonb()
    status: Mapped[str] = mapped_column(String(20), server_default="candidate")
    __table_args__ = (UniqueConstraint("company_id", "url", name="uq_company_sources_company_url"),)


class CompanyDiscoveryCandidate(Base, Timestamps):
    __tablename__ = "company_discovery_candidates"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255))
    source_event_id: Mapped[uuid.UUID | None] = uuid_fk("source_events.id", nullable=True, ondelete="SET NULL")
    matched_company_id: Mapped[uuid.UUID | None] = uuid_fk("companies.id", nullable=True, ondelete="SET NULL")
    match_confidence: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")  # pending/accepted/rejected/unresolved
    evidence: Mapped[dict] = jsonb()
