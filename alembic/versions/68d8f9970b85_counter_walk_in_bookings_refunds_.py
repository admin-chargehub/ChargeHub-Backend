"""counter: walk-in bookings, refunds, payment method

Revision ID: 68d8f9970b85
Revises: d47d04fba008
Create Date: 2026-11-03 08:39:50.811060
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '68d8f9970b85'
down_revision: str | None = 'd47d04fba008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Autogenerate emitted ADD COLUMN for the enum without a CREATE TYPE, and
    # NOT NULL with no default against a table that already has rows. Both
    # fail on PostgreSQL, so this is written by hand.
    payment_method = sa.Enum(
        "CARD", "TRANSFER", "USSD", "WALLET", "CASH", name="payment_method"
    )
    payment_method.create(op.get_bind(), checkfirst=True)

    # Every booking that already exists came through the app, which only has
    # the mock card provider — so CARD is the truthful backfill, not a
    # placeholder. The default is dropped afterwards so the application stays
    # the one deciding, rather than the column quietly supplying a value for
    # a field somebody forgot to set.
    op.add_column(
        "bookings",
        sa.Column("payment_method", payment_method, nullable=False, server_default="CARD"),
    )
    op.alter_column("bookings", "payment_method", server_default=None)

    op.add_column("bookings", sa.Column("created_by_id", sa.Uuid(), nullable=True))
    op.add_column(
        "bookings",
        sa.Column("refund_kobo", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("bookings", "refund_kobo", server_default=None)
    op.add_column(
        "bookings", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "bookings", sa.Column("refund_reason", sa.String(length=200), nullable=True)
    )
    op.create_foreign_key(
        "fk_bookings_created_by_id_users",
        "bookings",
        "users",
        ["created_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_created_by_id_users", "bookings", type_="foreignkey")
    op.drop_column("bookings", "refund_reason")
    op.drop_column("bookings", "refunded_at")
    op.drop_column("bookings", "refund_kobo")
    op.drop_column("bookings", "created_by_id")
    op.drop_column("bookings", "payment_method")
    sa.Enum(name="payment_method").drop(op.get_bind(), checkfirst=True)
