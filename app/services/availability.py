"""Availability engine — "find the earliest free outlet".

This is the one piece of genuinely valuable business logic in the 2016
prototype, and the only part the blueprint is right to call reuse. The original
(described in `website_structure.md`) read the *last* `timeExpire` from each
`socketN` table, took the earliest, and appended the requested hours.

That has two defects this version fixes:

1. It could only ever append to the end of a queue. It never found a *gap*
   between two existing bookings, so an outlet free 09:00–13:00 between two
   bookings was invisible to it.
2. "Last recorded" meant highest id, not latest time — so an out-of-order
   insert silently produced a double-booking.

Windows are half-open ``[starts_at, ends_at)``: a booking ending at 14:00 and
one starting at 14:00 do not conflict.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models import Booking, BookingStatus, Outlet, OutletStatus
from app.models.base import utcnow

#: Outlet states that can accept a new booking.
BOOKABLE_STATUSES = (OutletStatus.AVAILABLE, OutletStatus.IN_USE, OutletStatus.RESERVED)


def blocking_clause(now: datetime | None = None) -> ColumnElement[bool]:
    """Bookings that actually occupy an outlet at `now`.

    `Booking.BLOCKING` alone is not enough. It includes PENDING, so without the
    TTL below an abandoned checkout would hold an outlet forever — the customer
    closes the tab at the payment step and nobody can book that socket again.

    A PENDING booking past `pending_expires_at` stops blocking immediately here,
    which is what keeps availability honest between sweeper runs. Note the
    PostgreSQL exclusion constraint cannot apply the same cutoff (its predicate
    may not call `now()`), so a write can still conflict with an expired pending
    row until the sweeper cancels it.

    `now` is "is this hold still live", which is always the present moment —
    not the window being tested. It is a parameter only so tests can pin it.
    """
    now = now or utcnow()
    return and_(
        Booking.status.in_(Booking.BLOCKING),
        or_(
            Booking.status != BookingStatus.PENDING,
            Booking.pending_expires_at.is_(None),
            Booking.pending_expires_at > now,
        ),
    )


@dataclass(frozen=True, slots=True)
class Slot:
    outlet_id: uuid.UUID
    outlet_index: int
    starts_at: datetime
    ends_at: datetime


def _earliest_gap(
    busy: list[tuple[datetime, datetime]],
    not_before: datetime,
    duration: timedelta,
) -> datetime | None:
    """First instant >= not_before with `duration` clear of every busy window.

    `busy` need not be sorted; it is sorted here.
    """
    cursor = not_before
    for start, end in sorted(busy):
        if end <= cursor:
            continue  # already behind us
        if start - cursor >= duration:
            return cursor  # gap before this booking is big enough
        cursor = max(cursor, end)
    return cursor  # nothing left to collide with


async def find_earliest_slot(
    db: AsyncSession,
    station_id: uuid.UUID,
    duration_minutes: int,
    not_before: datetime,
) -> Slot | None:
    """Earliest bookable slot at `station_id`, or None if the station has no usable outlets.

    Ties break toward the lowest outlet index, so allocation is deterministic
    and the pilot fills predictably from socket 1 up.
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")

    duration = timedelta(minutes=duration_minutes)

    outlets = (
        (
            await db.execute(
                select(Outlet)
                .where(Outlet.station_id == station_id)
                .where(Outlet.status.in_(BOOKABLE_STATUSES))
                .order_by(Outlet.index)
            )
        )
        .scalars()
        .all()
    )
    if not outlets:
        return None

    outlet_ids = [o.id for o in outlets]

    rows = (
        (
            await db.execute(
                select(Booking.outlet_id, Booking.starts_at, Booking.ends_at)
                .where(Booking.outlet_id.in_(outlet_ids))
                .where(blocking_clause())
                .where(Booking.ends_at > not_before)
            )
        )
        .all()
    )

    busy_by_outlet: dict[uuid.UUID, list[tuple[datetime, datetime]]] = {}
    for outlet_id, starts_at, ends_at in rows:
        busy_by_outlet.setdefault(outlet_id, []).append((starts_at, ends_at))

    best: Slot | None = None
    for outlet in outlets:
        start = _earliest_gap(busy_by_outlet.get(outlet.id, []), not_before, duration)
        if start is None:
            continue
        if best is None or start < best.starts_at:
            best = Slot(
                outlet_id=outlet.id,
                outlet_index=outlet.index,
                starts_at=start,
                ends_at=start + duration,
            )
            if start == not_before:
                break  # cannot do better than immediate
    return best


async def is_outlet_free(
    db: AsyncSession,
    outlet_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    exclude_booking_id: uuid.UUID | None = None,
) -> bool:
    """Whether `outlet_id` has no blocking booking overlapping the window.

    Call this inside the same transaction that writes the booking. On its own it
    is advisory only — under concurrency a unique/exclusion constraint or a
    ``SELECT ... FOR UPDATE`` on the outlet row is what actually prevents a
    double-book. See the note in the README.
    """
    stmt = (
        select(Booking.id)
        .where(Booking.outlet_id == outlet_id)
        .where(blocking_clause())
        .where(Booking.starts_at < ends_at)
        .where(Booking.ends_at > starts_at)
        .limit(1)
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)

    return (await db.execute(stmt)).scalar_one_or_none() is None


async def outlet_status_snapshot(
    db: AsyncSession, station_id: uuid.UUID, at: datetime
) -> dict[uuid.UUID, BookingStatus | None]:
    """Which outlets are occupied at instant `at`, for the live station view."""
    rows = (
        (
            await db.execute(
                select(Booking.outlet_id, Booking.status)
                .join(Outlet, Outlet.id == Booking.outlet_id)
                .where(Outlet.station_id == station_id)
                .where(blocking_clause())
                .where(Booking.starts_at <= at)
                .where(Booking.ends_at > at)
            )
        )
        .all()
    )
    return {outlet_id: status for outlet_id, status in rows}
