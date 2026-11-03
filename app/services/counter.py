"""Things an operator does that a customer cannot.

The first version of the console could only watch. That was the wrong shape:
the customer app already handles the happy path, so a console that mirrors it
adds nothing. What an operator is actually for is the cases self-service
cannot reach —

* somebody turns up with cash and no smartphone, or a phone with no data;
* a charge needs another twenty minutes and the customer is not going to
  rebook from scratch over it;
* the outlet died mid-session and they are owed their money back;
* someone is at the counter holding a PIN and no idea what it belongs to.

Each of these is a write the customer API deliberately does not expose, and
each one moves money or power, so each is audited.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessCode,
    Booking,
    BookingStatus,
    ChargingSession,
    Outlet,
    PaymentMethod,
    Plan,
    SessionStatus,
    Station,
    User,
    UserRole,
    utcnow,
)
from app.services import access_codes
from app.services.availability import is_outlet_free
from app.services.bookings import OutletUnavailable, PlanUnavailable, SlotTaken

#: A password hash that no password can produce, so the row cannot be logged
#: into. Walk-ins are real customer records — bookings, sessions and reporting
#: all work on them unchanged — they just have no credentials until the person
#: chooses to set some. Storing a random password instead would be a
#: credential nobody knows and everybody has to reset.
UNUSABLE_PASSWORD = "!"


class CounterError(Exception):
    """Something the operator needs to be told, not a 500."""


@dataclass
class WalkIn:
    booking: Booking
    user: User
    access_code: str
    created_user: bool


async def _find_or_create_customer(
    db: AsyncSession, *, name: str, phone: str | None, email: str | None
) -> tuple[User, bool]:
    """Match an existing customer, or mint a credential-less one.

    Matching on phone first: at a counter in Ibadan a phone number is what
    somebody actually has and will repeat correctly, and it is how the same
    person is recognised on their second visit rather than accumulating a new
    record each time.
    """
    clauses = []
    if email:
        clauses.append(User.email == email.lower().strip())
    if phone:
        clauses.append(User.phone == phone.strip())

    if clauses:
        existing = (
            await db.execute(select(User).where(or_(*clauses)))
        ).scalars().first()
        if existing is not None:
            # Fill in a detail we did not have before, but never overwrite one
            # the customer set themselves.
            if phone and not existing.phone:
                existing.phone = phone.strip()
            return existing, False

    if not email:
        # Synthetic, unique, and obviously not a real mailbox. It must not
        # collide with a address somebody might later register with, or the
        # walk-in record would block their signup.
        email = f"walkin-{uuid.uuid4().hex[:12]}@walkin.chargehub.invalid"

    user = User(
        email=email.lower().strip(),
        full_name=name.strip() or "Walk-in customer",
        phone=phone.strip() if phone else None,
        hashed_password=UNUSABLE_PASSWORD,
        role=UserRole.CUSTOMER,
    )
    db.add(user)
    await db.flush()
    return user, True


async def book_walk_in(
    db: AsyncSession,
    *,
    operator: User,
    outlet_id: uuid.UUID,
    plan_id: uuid.UUID,
    name: str,
    phone: str | None = None,
    email: str | None = None,
    method: PaymentMethod = PaymentMethod.CASH,
) -> WalkIn:
    """Book and pay for an outlet on behalf of someone at the counter.

    Goes straight to CONFIRMED with no gateway. That is the honest model for
    cash: the money is already in the operator's hand, so there is nothing to
    authorise and nothing that can decline. The booking records who took it
    so the till can be reconciled against a person at the end of a shift.
    """
    plan = (
        await db.execute(
            select(Plan).where(Plan.id == plan_id, Plan.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if plan is None:
        raise PlanUnavailable("That plan is no longer offered.")

    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == outlet_id).with_for_update())
    ).scalar_one_or_none()
    if outlet is None:
        raise OutletUnavailable("No such outlet.")
    # Operators may book an outlet the customer app would hide, but not one
    # that is physically out of service — handing somebody a PIN for a socket
    # marked faulty is how people get hurt.
    if outlet.status.value in ("fault", "offline", "maintenance"):
        raise OutletUnavailable(
            f"That outlet is {outlet.status.value}. Put it back in service first."
        )

    starts_at = utcnow()
    ends_at = starts_at + timedelta(minutes=plan.max_duration_minutes)

    if not await is_outlet_free(db, outlet_id, starts_at, ends_at):
        raise SlotTaken("That outlet is already booked for this window.")

    user, created = await _find_or_create_customer(
        db, name=name, phone=phone, email=email
    )

    booking = Booking(
        user_id=user.id,
        outlet_id=outlet_id,
        plan_id=plan.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=BookingStatus.CONFIRMED,
        amount_kobo=plan.price_kobo,
        payment_method=method,
        created_by_id=operator.id,
        paid_at=starts_at,
        pending_expires_at=None,
    )
    db.add(booking)
    await db.flush()

    _record, plaintext = await access_codes.issue(db, booking)
    await db.flush()

    return WalkIn(booking=booking, user=user, access_code=plaintext, created_user=created)


async def extend_session(
    db: AsyncSession, session: ChargingSession, minutes: int
) -> ChargingSession:
    """Add time to a running session.

    The design has an "Extend Session" button and no definition of what it
    does. The part that matters is that extending is a *booking* change, not
    just a longer countdown: the outlet has to actually be free for the extra
    window, or this hands the next customer's slot away to the person already
    standing there.
    """
    if session.status is SessionStatus.COMPLETED:
        raise CounterError("That session has already ended.")
    if minutes <= 0 or minutes > 24 * 60:
        raise CounterError("Extend by between 1 minute and 24 hours.")

    booking = (
        await db.execute(select(Booking).where(Booking.id == session.booking_id))
    ).scalar_one()

    new_end = session.ends_at + timedelta(minutes=minutes)
    free = await is_outlet_free(
        db,
        session.outlet_id,
        session.ends_at,
        new_end,
        exclude_booking_id=booking.id,
    )
    if not free:
        raise CounterError(
            "Someone else has this outlet booked for that window. "
            "Move them, or end this session on time."
        )

    session.ends_at = new_end
    booking.ends_at = new_end
    await db.flush()
    return session


async def refund(
    db: AsyncSession, booking: Booking, *, kobo: int, reason: str
) -> Booking:
    """Record money given back.

    Records it; it does not move it. With cash the operator hands over notes,
    and with a gateway the refund is raised there — either way this is the
    ledger entry that makes the takings true, and pretending the system had
    pushed the money would be worse than recording nothing.
    """
    if kobo <= 0:
        raise CounterError("A refund has to be more than zero.")
    already = booking.refund_kobo or 0
    if already + kobo > booking.amount_kobo:
        remaining = booking.amount_kobo - already
        raise CounterError(
            f"That is more than is left to refund (₦{remaining / 100:,.0f})."
        )
    if not reason.strip():
        raise CounterError("Record why this was refunded.")

    booking.refund_kobo = already + kobo
    booking.refunded_at = utcnow()
    booking.refund_reason = reason.strip()
    await db.flush()
    return booking


@dataclass
class Lookup:
    booking: Booking
    user: User
    outlet: Outlet
    station: Station
    session: ChargingSession | None
    code_state: str


async def find_by_code(db: AsyncSession, raw: str) -> Lookup | None:
    """Answer "what is this PIN?" for someone standing at the counter.

    Looks up by hash, the same way redemption does, so a code that would be
    rejected at the keypad is reported as rejected here rather than looking
    fine to the operator and failing in front of the customer.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None

    row = (
        await db.execute(
            select(AccessCode, Booking, Outlet, Station, User)
            .join(Booking, AccessCode.booking_id == Booking.id)
            .join(Outlet, Booking.outlet_id == Outlet.id)
            .join(Station, Outlet.station_id == Station.id)
            .join(User, Booking.user_id == User.id)
            .where(AccessCode.code_sha256 == access_codes.hash_code(digits))
        )
    ).first()
    if row is None:
        return None

    record, booking, outlet, station, user = row
    now = utcnow()
    if record.redeemed_at is not None:
        state = "already used"
    elif record.not_after and record.not_after < now:
        state = "expired"
    elif record.not_before and record.not_before > now:
        state = "not valid yet"
    else:
        state = "valid"

    session = (
        await db.execute(
            select(ChargingSession).where(ChargingSession.booking_id == booking.id)
        )
    ).scalar_one_or_none()

    return Lookup(
        booking=booking,
        user=user,
        outlet=outlet,
        station=station,
        session=session,
        code_state=state,
    )
