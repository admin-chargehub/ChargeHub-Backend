"""Read models for the operator console.

Everything here is a query, not a mutation. The write paths live with the
domain they belong to (`bookings`, `sessions`) so the console cannot grow its
own parallel rules for ending a session.

Aggregates are computed in SQL rather than by loading rows and summing in
Python. With five hubs either works; the difference shows up at fifty, and the
shape of the query is the thing that is hard to change later.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Booking,
    BookingStatus,
    ChargingSession,
    Outlet,
    OutletStatus,
    SessionStatus,
    Station,
    StationTelemetry,
    User,
    utcnow,
)


def _day_bounds(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC window covering the last `days` days, today inclusive.

    The pilot is in one timezone (WAT, UTC+1) and reports are read locally, so
    "today" here is a UTC day boundary and will be off by an hour at the edges.
    That is a deliberate simplification with a single fix — a station-local
    timezone column — rather than a hidden assumption: see REPORTING_TZ_NOTE.
    """
    now = now or utcnow()
    end = now
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, end


REPORTING_TZ_NOTE = (
    "Day boundaries are UTC. The pilot runs in WAT (UTC+1), so a session "
    "between 00:00 and 01:00 local is reported on the previous day."
)


@dataclass
class Overview:
    stations_total: int
    stations_online: int
    outlets_total: int
    outlets_available: int
    outlets_in_use: int
    outlets_faulty: int
    sessions_active: int
    revenue_today_kobo: int
    energy_today_wh: int
    generator_minutes_today: int


async def overview(db: AsyncSession) -> Overview:
    start, end = _day_bounds(1)

    stations_total = (
        await db.execute(select(func.count()).select_from(Station))
    ).scalar_one()

    # Online is derived from last_seen_at rather than stored, so a station that
    # stops reporting goes offline on its own instead of waiting for something
    # to notice and write a flag.
    cutoff = utcnow() - timedelta(minutes=10)
    stations_online = (
        await db.execute(
            select(func.count())
            .select_from(StationTelemetry)
            .where(StationTelemetry.last_seen_at >= cutoff)
        )
    ).scalar_one()

    status_counts = dict(
        (
            await db.execute(select(Outlet.status, func.count()).group_by(Outlet.status))
        ).all()
    )
    outlets_total = sum(status_counts.values())

    sessions_active = (
        await db.execute(
            select(func.count())
            .select_from(ChargingSession)
            .where(ChargingSession.status == SessionStatus.ACTIVE)
        )
    ).scalar_one()

    # Revenue is counted from the booking that was paid for, not the session:
    # a confirmed booking is money taken even if the customer never turns up,
    # and a report that only counts redeemed codes understates the day.
    revenue_today = (
        await db.execute(
            # Net of refunds. A takings figure that ignores money given back
            # is not revenue, it is turnover, and it will not reconcile with
            # the till at the end of a shift.
            select(
                func.coalesce(func.sum(Booking.amount_kobo - Booking.refund_kobo), 0)
            ).where(
                Booking.paid_at.is_not(None),
                Booking.paid_at >= start,
                Booking.paid_at <= end,
            )
        )
    ).scalar_one()

    energy_today = (
        await db.execute(
            select(func.coalesce(func.sum(ChargingSession.energy_wh), 0)).where(
                ChargingSession.started_at >= start,
                ChargingSession.started_at <= end,
            )
        )
    ).scalar_one()

    generator_minutes = (
        await db.execute(
            select(func.coalesce(func.sum(StationTelemetry.generator_minutes_today), 0))
        )
    ).scalar_one()

    return Overview(
        stations_total=stations_total,
        stations_online=stations_online,
        outlets_total=outlets_total,
        outlets_available=status_counts.get(OutletStatus.AVAILABLE, 0),
        outlets_in_use=status_counts.get(OutletStatus.IN_USE, 0),
        outlets_faulty=status_counts.get(OutletStatus.FAULT, 0)
        + status_counts.get(OutletStatus.OFFLINE, 0),
        sessions_active=sessions_active,
        revenue_today_kobo=int(revenue_today),
        energy_today_wh=int(energy_today),
        generator_minutes_today=int(generator_minutes),
    )


async def telemetry_by_station(db: AsyncSession) -> dict[uuid.UUID, StationTelemetry]:
    rows = (await db.execute(select(StationTelemetry))).scalars().all()
    return {row.station_id: row for row in rows}


async def outlet_counts_by_station(
    db: AsyncSession,
) -> dict[uuid.UUID, dict[OutletStatus, int]]:
    rows = (
        await db.execute(
            select(Outlet.station_id, Outlet.status, func.count()).group_by(
                Outlet.station_id, Outlet.status
            )
        )
    ).all()
    out: dict[uuid.UUID, dict[OutletStatus, int]] = {}
    for station_id, status, count in rows:
        out.setdefault(station_id, {})[status] = count
    return out


def sessions_query(
    *,
    station_id: uuid.UUID | None = None,
    active_only: bool = False,
) -> Select:
    """Sessions joined to the customer and outlet behind them.

    The operator needs to know *who* is on an outlet to help them, which the
    customer-facing session endpoints deliberately never expose.
    """
    q = (
        select(ChargingSession, Outlet, Station, User)
        .join(Outlet, ChargingSession.outlet_id == Outlet.id)
        .join(Station, Outlet.station_id == Station.id)
        .join(Booking, ChargingSession.booking_id == Booking.id)
        .join(User, Booking.user_id == User.id)
        .order_by(ChargingSession.started_at.desc())
    )
    if station_id is not None:
        q = q.where(Station.id == station_id)
    if active_only:
        q = q.where(ChargingSession.status == SessionStatus.ACTIVE)
    return q


@dataclass
class DayRow:
    day: date
    sessions: int
    energy_wh: int
    revenue_kobo: int


async def daily_series(db: AsyncSession, days: int) -> list[DayRow]:
    """Per-day totals, with empty days present as zeroes.

    Missing days matter: a chart that silently skips a day with no sessions
    draws a continuous line through an outage.
    """
    start, end = _day_bounds(days)

    session_rows = (
        await db.execute(
            select(
                func.date(ChargingSession.started_at).label("day"),
                func.count(),
                func.coalesce(func.sum(ChargingSession.energy_wh), 0),
            )
            .where(ChargingSession.started_at >= start, ChargingSession.started_at <= end)
            .group_by("day")
        )
    ).all()

    revenue_rows = (
        await db.execute(
            select(
                func.date(Booking.paid_at).label("day"),
                func.coalesce(
                    func.sum(Booking.amount_kobo - Booking.refund_kobo), 0
                ),
            )
            .where(
                Booking.paid_at.is_not(None),
                Booking.paid_at >= start,
                Booking.paid_at <= end,
            )
            .group_by("day")
        )
    ).all()

    def as_date(value: object) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str):  # SQLite returns text from func.date()
            return date.fromisoformat(value)
        return None

    sessions = {as_date(d): (c, int(e)) for d, c, e in session_rows}
    revenue = {as_date(d): int(r) for d, r in revenue_rows}

    out: list[DayRow] = []
    cursor = start.date()
    last = end.date()
    while cursor <= last:
        count, energy = sessions.get(cursor, (0, 0))
        out.append(
            DayRow(
                day=cursor,
                sessions=count,
                energy_wh=energy,
                revenue_kobo=revenue.get(cursor, 0),
            )
        )
        cursor += timedelta(days=1)
    return out


@dataclass
class StationTotals:
    station_id: uuid.UUID
    name: str
    sessions: int
    energy_wh: int
    revenue_kobo: int


async def station_totals(db: AsyncSession, days: int) -> list[StationTotals]:
    start, end = _day_bounds(days)

    rows = (
        await db.execute(
            select(
                Station.id,
                Station.name,
                func.count(ChargingSession.id),
                func.coalesce(func.sum(ChargingSession.energy_wh), 0),
                func.coalesce(func.sum(ChargingSession.cost_kobo), 0),
            )
            .select_from(Station)
            # Outer joins so a station with no sessions still appears at zero.
            # A "top stations" table that silently drops the quiet sites hides
            # exactly the ones worth asking about.
            .outerjoin(Outlet, Outlet.station_id == Station.id)
            .outerjoin(
                ChargingSession,
                (ChargingSession.outlet_id == Outlet.id)
                & (ChargingSession.started_at >= start)
                & (ChargingSession.started_at <= end),
            )
            .group_by(Station.id, Station.name)
        )
    ).all()

    return sorted(
        (
            StationTotals(
                station_id=sid,
                name=name,
                sessions=count,
                energy_wh=int(energy),
                revenue_kobo=int(revenue),
            )
            for sid, name, count, energy, revenue in rows
        ),
        key=lambda s: (-s.revenue_kobo, s.name),
    )


async def pending_attention(db: AsyncSession) -> list[tuple[Station, Outlet]]:
    """Outlets an operator should look at: faulted, offline or in maintenance.

    Surfaced on the dashboard because the designed console reports "2 Faulty"
    as a number with no way to find out which two.
    """
    rows = (
        await db.execute(
            select(Station, Outlet)
            .join(Outlet, Outlet.station_id == Station.id)
            .where(
                Outlet.status.in_(
                    (OutletStatus.FAULT, OutletStatus.OFFLINE, OutletStatus.MAINTENANCE)
                )
            )
            .order_by(Station.name, Outlet.index)
        )
    ).all()
    return [(station, outlet) for station, outlet in rows]


async def upcoming_bookings(
    db: AsyncSession, limit: int = 20
) -> list[tuple[Booking, Outlet, Station, User]]:
    """Paid bookings not yet redeemed, soonest first.

    Lets an operator see who is about to arrive — the designed console has no
    forward view at all, only what is already running.
    """
    rows = (
        await db.execute(
            select(Booking, Outlet, Station, User)
            .join(Outlet, Booking.outlet_id == Outlet.id)
            .join(Station, Outlet.station_id == Station.id)
            .join(User, Booking.user_id == User.id)
            .where(Booking.status == BookingStatus.CONFIRMED)
            .order_by(Booking.starts_at)
            .limit(limit)
        )
    ).all()
    return [(b, o, s, u) for b, o, s, u in rows]
