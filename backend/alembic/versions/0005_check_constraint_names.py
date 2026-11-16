"""Fix doubled check-constraint names created by 0004

0004 passed fully prefixed names (ck_<table>_kind) while the metadata naming convention adds ck_<table>_ itself, so a
database built from 0004 as pushed has ck_<table>_ck_<table>_kind. The ORM models use the short names. Idempotent: a database
that already has the correct names (0004 was applied from a corrected copy) is left alone.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RENAMES = (
    ("event_relationships", "ck_event_relationships_ck_event_relationships_kind", "ck_event_relationships_kind"),
    ("funding_round_claims", "ck_funding_round_claims_ck_funding_round_claims_status", "ck_funding_round_claims_status"),
)


def upgrade() -> None:
    for table, old, new in RENAMES:
        op.execute(f"""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{old}' AND conrelid = '{table}'::regclass) THEN
                ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
            END IF; END $$""")


def downgrade() -> None:
    """Names are cosmetic; restoring the doubled ones would only reintroduce the defect."""
