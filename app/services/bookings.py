"""Creating and paying for bookings.

This is the only place a `Booking` row is written. The concurrency handling is
the substance of it — everything else is bookkeeping.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessCode, Booking, BookingStatus, Outlet, Plan, User
from app.models.base import utcnow
from app.services import access_codes
from app.services.availability import BOOKABLE_STATUSES, is_outlet_free
from app.services.payments import get_provider


class BookingError(Exception):
    """Base for failures the API should report rather than 500 on."""


class OutletUnavailable(BookingError):
    pass


class PlanUnavailable(BookingError):
    pass


class SlotTaken(BookingError):
    pass


class NotPayable(BookingError):
    pass


async def create_booking(
    db: AsyncSession,
    *,
    user: User,
    outlet_id: uuid.UUID,
    plan_id: uuid.UUID,
    idempotency_key: str | None = None,
) -> Booking:
    """Reserve `outlet_id` for the duration `plan_id` grants, starting now.

    Concurrency, in layers:

    1. `SELECT … FOR UPDATE` on the outlet row serialises competing requests for
       the same outlet, so the overlap check below cannot race.
    2. `is_outlet_free` is the application check. It produces the useful error
       message. On its own it is advisory — its own docstring says so.
    3. A PostgreSQL exclusion constraint (migration-only) is the actual
       guarantee. Anything that slips past 1 and 2 surfaces here as an
       `IntegrityError`, which we translate into the same `SlotTaken`.

    Layer 3 is what makes this correct; layers 1 and 2 are what make it polite.
    """
    if idempotency_key:
        existing = (
            await db.execute(
                select(Booking).where(Booking.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # A retried request, not a second booking.
            return existing

    plan = (
        await db.execute(select(Plan).where(Plan.id == plan_id).where(Plan.is_active.is_(True)))
    ).scalar_one_or_none()
    if plan is None:
        raise PlanUnavailable("That plan is no longer offered.")

    # FOR UPDATE: held until this transaction commits. Note SQLAlchemy silently
    # drops this on SQLite, so the test that proves it works has to run against
    # PostgreSQL — see tests/integration/.
    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == outlet_id).with_for_update())
    ).scalar_one_or_none()
    if outlet is None or outlet.status not in BOOKABLE_STATUSES:
        raise OutletUnavailable("That outlet can’t be booked right now.")

    starts_at = utcnow()
    # Derived from the plan, never accepted from the client — otherwise someone
    # buys the one-hour plan and books a twenty-four hour window.
    ends_at = starts_at + timedelta(minutes=plan.max_duration_minutes)

    if not await is_outlet_free(db, outlet_id, starts_at, ends_at):
        raise SlotTaken("Someone just took that outlet. Try another.")

    booking = Booking(
        user_id=user.id,
        outlet_id=outlet_id,
        plan_id=plan.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=BookingStatus.PENDING,
        amount_kobo=plan.price_kobo,
        pending_expires_at=starts_at + timedelta(minutes=Booking.PENDING_TTL_MINUTES),
        idempotency_key=idempotency_key,
    )
    db.add(booking)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise SlotTaken("Someone just took that outlet. Try another.") from exc

    return booking


async def pay_booking(db: AsyncSession, booking: Booking, user: User) -> tuple[Booking, str]:
    """Take payment and issue the access code. Returns the booking and the plaintext code.

    Payment and code issue happen in one transaction: a confirmed booking with
    no code would be a customer who paid and cannot open anything.
    """
    if booking.status == BookingStatus.CONFIRMED:
        # Idempotent: paying twice returns the existing code rather than
        # charging again or minting a second code.
        record = await access_codes.for_booking(db, booking.id)
        if record is not None:
            plaintext = access_codes.reveal(record)
            if plaintext is not None:
                return booking, plaintext
        raise NotPayable("This booking is already paid but its code has expired.")

    if booking.status != BookingStatus.PENDING:
        raise NotPayable(f"A {booking.status.value} booking can’t be paid for.")

    if booking.pending_expires_at and booking.pending_expires_at <= utcnow():
        booking.status = BookingStatus.CANCELLED
        await db.flush()
        raise NotPayable("This booking expired before payment. Please book again.")

    provider = get_provider()
    intent = provider.initialize(
        amount_kobo=booking.amount_kobo, email=user.email, booking_id=booking.id
    )
    result = provider.verify(intent.reference)
    if not result.paid:
        raise NotPayable(result.message or "Payment was declined.")

    booking.status = BookingStatus.CONFIRMED
    booking.pending_expires_at = None

    _record, plaintext = await access_codes.issue(db, booking)
    await db.flush()
    return booking, plaintext


async def sweep_pending(db: AsyncSession) -> int:
    """Cancel pending bookings that were never paid for.

    The query-level TTL in `availability.blocking_clause` already stops these
    from hiding an outlet, but the PostgreSQL exclusion constraint cannot apply
    a time cutoff, so a *write* keeps conflicting until the row actually moves
    out of a blocking status. Run this on a minute-ish cadence.
    """
    now = utcnow()
    stale = (
        (
            await db.execute(
                select(Booking)
                .where(Booking.status == BookingStatus.PENDING)
                .where(Booking.pending_expires_at.is_not(None))
                .where(Booking.pending_expires_at <= now)
            )
        )
        .scalars()
        .all()
    )
    for booking in stale:
        booking.status = BookingStatus.CANCELLED
    await db.flush()
    return len(stale)


async def list_for_user(db: AsyncSession, user: User, limit: int = 50) -> list[Booking]:
    return list(
        (
            await db.execute(
                select(Booking)
                .where(Booking.user_id == user.id)
                .order_by(Booking.starts_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def get_for_user(db: AsyncSession, user: User, booking_id: uuid.UUID) -> Booking | None:
    """Someone else's booking is reported as missing, not forbidden — a 403
    would confirm the id exists."""
    return (
        await db.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .where(Booking.user_id == user.id)
        )
    ).scalar_one_or_none()


__all__ = [
    "AccessCode",
    "BookingError",
    "NotPayable",
    "OutletUnavailable",
    "PlanUnavailable",
    "SlotTaken",
    "create_booking",
    "get_for_user",
    "list_for_user",
    "pay_booking",
    "sweep_pending",
]
