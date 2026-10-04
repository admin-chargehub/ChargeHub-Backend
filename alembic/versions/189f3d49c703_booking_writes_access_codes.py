"""booking writes, access codes

Revision ID: 189f3d49c703
Revises: c996dcdedf2a
Create Date: 2026-10-04 12:23:18.304842

Adds what is needed to actually write a booking: the amount charged, a pending
TTL, an idempotency key, the access-code table — and the exclusion constraint
that makes double-booking impossible.

Two things worth reading before changing any of this:

1. The exclusion constraint is declared HERE AND ONLY HERE, never in
   `Booking.__table_args__`. SQLite cannot express it, and the test suite builds
   its schema with `Base.metadata.create_all`; declaring it on the model would
   break every test. The model and the production database differ on purpose.

2. The constraint's predicate CANNOT reference `now()`. Index predicates must be
   immutable, and `now()` is not. So the predicate covers all three blocking
   statuses unconditionally, which means **an expired PENDING booking still
   occupies its range here until the sweeper moves it to CANCELLED.**
   `availability.blocking_clause()` applies the time cutoff at query time, so
   availability *displays* correctly straight away; it is only a *write* that
   will 409 against a stale pending row. Run `sweep_pending` about once a
   minute against the 10-minute TTL and the window is invisible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "189f3d49c703"
down_revision: str | None = "c996dcdedf2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Must match Booking.BLOCKING. These are the enum *labels* PostgreSQL stores,
#: which SQLAlchemy derives from the Python enum member names, not their values.
BLOCKING = ("PENDING", "CONFIRMED", "ACTIVE")


def upgrade() -> None:
    op.create_table(
        "access_codes",
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("code_encrypted", sa.String(length=255), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("single_use", sa.Boolean(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_access_codes_booking_id"), "access_codes", ["booking_id"])
    op.create_index(
        op.f("ix_access_codes_code_sha256"), "access_codes", ["code_sha256"], unique=True
    )

    # server_default so the NOT NULL add succeeds against existing rows, then
    # dropped so the application is the only thing that sets a price.
    op.add_column(
        "bookings",
        sa.Column("amount_kobo", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("bookings", "amount_kobo", server_default=None)

    op.add_column(
        "bookings", sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("bookings", sa.Column("idempotency_key", sa.String(length=64), nullable=True))
    op.create_index(
        op.f("ix_bookings_idempotency_key"), "bookings", ["idempotency_key"], unique=True
    )

    # The real double-booking guarantee. Application-level checks produce the
    # friendly error; this produces the truth.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        f"""
        ALTER TABLE bookings ADD CONSTRAINT ex_booking_no_overlap
        EXCLUDE USING gist (
            outlet_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        WHERE (status IN {BLOCKING})
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ex_booking_no_overlap")
    # btree_gist is left installed; other things may depend on it.

    op.drop_index(op.f("ix_bookings_idempotency_key"), table_name="bookings")
    op.drop_column("bookings", "idempotency_key")
    op.drop_column("bookings", "pending_expires_at")
    op.drop_column("bookings", "amount_kobo")

    op.drop_index(op.f("ix_access_codes_code_sha256"), table_name="access_codes")
    op.drop_index(op.f("ix_access_codes_booking_id"), table_name="access_codes")
    op.drop_table("access_codes")
