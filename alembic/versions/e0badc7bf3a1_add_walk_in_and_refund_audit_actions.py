"""add walk-in and refund audit actions

Revision ID: e0badc7bf3a1
Revises: 68d8f9970b85
Create Date: 2026-11-03 08:42:15.706151
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e0badc7bf3a1'
down_revision: str | None = '68d8f9970b85'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the new AuditAction members to the PostgreSQL enum type.

    Alembic autogenerate does not detect values added to an existing enum —
    it compares tables and columns, not the members of a type — so adding a
    member to the Python StrEnum and nothing else leaves the application
    writing a value the database will reject at runtime. Every enum extension
    needs a migration like this one.

    IF NOT EXISTS so re-running against a database that already has them is a
    no-op rather than an error.
    """
    for value in ("WALK_IN_BOOKED", "BOOKING_REFUNDED"):
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Not reversible.

    PostgreSQL has no DROP VALUE. Removing an enum member means recreating the
    type and rewriting every column that uses it, which is not worth
    automating for an append-only audit table — and any row already written
    with one of these values would have nowhere to go.
    """
