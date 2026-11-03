"""Operator console API.

Every route here requires OPERATOR or ADMIN. `require_roles` has existed in
`api/deps.py` since the first commit and was applied to nothing; this is the
first module that uses it, and it is the reason the console can show customer
names and site revenue at all.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DbSession, require_roles
from app.models import (
    AuditAction,
    AuditEvent,
    Booking,
    ChargingSession,
    Outlet,
    OutletStatus,
    SessionEndReason,
    SessionStatus,
    Station,
    User,
    UserRole,
    utcnow,
)
from app.schemas.ops import (
    AttentionOut,
    AuditEventOut,
    DashboardOut,
    DayPointOut,
    ExtendIn,
    ImpactOut,
    LookupOut,
    OpsOutletOut,
    OpsSessionOut,
    OpsStationDetailOut,
    OpsStationOut,
    OutletStatusIn,
    OverviewOut,
    RefundIn,
    ReportOut,
    StationActiveIn,
    StationTotalsOut,
    TelemetryOut,
    UpcomingBookingOut,
    WalkInIn,
    WalkInOut,
)
from app.services import audit, counter, impact, ops
from app.services import sessions as session_service
from app.services.bookings import OutletUnavailable, PlanUnavailable, SlotTaken

router = APIRouter(prefix="/ops", tags=["ops"])

#: Applied at the router level below, so a route added later cannot forget it.
OperatorUser = Annotated[User, Depends(require_roles(UserRole.OPERATOR, UserRole.ADMIN))]


def _outlet_label(outlet: Outlet) -> str:
    return outlet.label or f"Outlet {outlet.index}"


async def _station_rows(db) -> list[OpsStationOut]:
    stations = (await db.execute(select(Station).order_by(Station.name))).scalars().all()
    telemetry = await ops.telemetry_by_station(db)
    counts = await ops.outlet_counts_by_station(db)

    rows: list[OpsStationOut] = []
    for station in stations:
        by_status = counts.get(station.id, {})
        tel = telemetry.get(station.id)
        rows.append(
            OpsStationOut(
                id=station.id,
                name=station.name,
                slug=station.slug,
                address=station.address,
                is_active=station.is_active,
                outlets_total=sum(by_status.values()),
                outlets_available=by_status.get(OutletStatus.AVAILABLE, 0),
                outlets_in_use=by_status.get(OutletStatus.IN_USE, 0),
                outlets_faulty=by_status.get(OutletStatus.FAULT, 0)
                + by_status.get(OutletStatus.OFFLINE, 0),
                telemetry=(
                    TelemetryOut(
                        source=tel.source,
                        battery_soc_pct=tel.battery_soc_pct,
                        solar_w=tel.solar_w,
                        load_w=tel.load_w,
                        grid_available=tel.grid_available,
                        generator_running=tel.generator_running,
                        generator_minutes_today=tel.generator_minutes_today,
                        last_seen_at=tel.last_seen_at,
                        is_online=tel.is_online,
                    )
                    if tel
                    else None
                ),
            )
        )
    return rows


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(db: DbSession, _: OperatorUser) -> DashboardOut:
    """Everything the landing screen needs, in one round trip.

    One endpoint rather than five because the dashboard is useless partially
    loaded, and five parallel requests means five chances for one to fail and
    leave a panel blank with no explanation.
    """
    summary = await ops.overview(db)

    return DashboardOut(
        overview=OverviewOut(**vars(summary), server_now=utcnow()),
        stations=await _station_rows(db),
        attention=[
            AttentionOut(
                station_id=station.id,
                station_name=station.name,
                outlet_id=outlet.id,
                outlet_label=_outlet_label(outlet),
                status=outlet.status,
                reason=outlet.fault_reason,
                since=outlet.status_changed_at,
            )
            for station, outlet in await ops.pending_attention(db)
        ],
        upcoming=[
            UpcomingBookingOut(
                id=booking.id,
                station_name=station.name,
                outlet_label=_outlet_label(outlet),
                customer_name=user.full_name,
                starts_at=booking.starts_at,
                ends_at=booking.ends_at,
                amount_kobo=booking.amount_kobo,
            )
            for booking, outlet, station, user in await ops.upcoming_bookings(db)
        ],
    )


@router.get("/stations", response_model=list[OpsStationOut])
async def list_stations(db: DbSession, _: OperatorUser) -> list[OpsStationOut]:
    return await _station_rows(db)


@router.get("/stations/{station_id}", response_model=OpsStationDetailOut)
async def get_station(
    station_id: uuid.UUID, db: DbSession, _: OperatorUser
) -> OpsStationDetailOut:
    rows = await _station_rows(db)
    base = next((r for r in rows if r.id == station_id), None)
    if base is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Station not found")

    outlets = (
        (
            await db.execute(
                select(Outlet).where(Outlet.station_id == station_id).order_by(Outlet.index)
            )
        )
        .scalars()
        .all()
    )
    return OpsStationDetailOut(
        **base.model_dump(),
        outlets=[OpsOutletOut.model_validate(o) for o in outlets],
    )


@router.patch("/outlets/{outlet_id}", response_model=OpsOutletOut)
async def set_outlet_status(
    outlet_id: uuid.UUID, payload: OutletStatusIn, db: DbSession, actor: OperatorUser
) -> OpsOutletOut:
    """Take an outlet out of service, or put it back.

    Three guards, in this order, because each one protects against a different
    way to make the board lie:

    1. Only statuses the booking lifecycle does not own may be set by hand.
    2. Taking an outlet out of service requires a reason.
    3. An outlet with a live session on it is refused unless `force` is set,
       and forcing ends that session properly rather than orphaning it — a
       customer mid-charge must not simply have the socket vanish from under
       them with the booking still marked active.
    """
    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == outlet_id))
    ).scalar_one_or_none()
    if outlet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")

    if payload.status not in Outlet.OPERATOR_SETTABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{payload.status.value} is set by the booking lifecycle, not by hand",
        )

    out_of_service = payload.status is not OutletStatus.AVAILABLE
    if out_of_service and not (payload.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A reason is required when taking an outlet out of service",
        )

    live = (
        await db.execute(
            select(ChargingSession).where(
                ChargingSession.outlet_id == outlet_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()

    if live is not None and out_of_service:
        if not payload.force:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A session is running on this outlet. Re-send with force to end it.",
            )
        await session_service.end(db, live, SessionEndReason.FAULT)
        await audit.record(
            db,
            actor=actor,
            action=AuditAction.SESSION_ENDED_BY_OPERATOR,
            target_type="session",
            target_id=live.id,
            target_label=_outlet_label(outlet),
            summary=(
                f"Ended the live session on {_outlet_label(outlet)} to take the "
                f"outlet out of service ({payload.reason})"
            ),
        )

    previous = outlet.status
    outlet.status = payload.status
    # Clearing the reason on the way back to AVAILABLE matters: a stale
    # "burnt socket" note under a working outlet is worse than none.
    outlet.fault_reason = payload.reason.strip() if out_of_service and payload.reason else None
    outlet.status_changed_at = utcnow()

    station = (
        await db.execute(select(Station).where(Station.id == outlet.station_id))
    ).scalar_one()

    await audit.record(
        db,
        actor=actor,
        action=AuditAction.OUTLET_STATUS_CHANGED,
        target_type="outlet",
        target_id=outlet.id,
        target_label=f"{station.name} · {_outlet_label(outlet)}",
        summary=(
            f"{_outlet_label(outlet)} at {station.name}: "
            f"{previous.value} → {payload.status.value}"
            + (f" ({outlet.fault_reason})" if outlet.fault_reason else "")
        ),
    )

    await db.commit()
    await db.refresh(outlet)
    return OpsOutletOut.model_validate(outlet)


def _session_row(session, outlet, station, user) -> OpsSessionOut:
    return OpsSessionOut(
        id=session.id,
        station_id=station.id,
        station_name=station.name,
        outlet_label=_outlet_label(outlet),
        customer_name=user.full_name,
        customer_email=user.email,
        customer_phone=user.phone,
        status=session.status.value,
        started_at=session.started_at,
        ends_at=session.ends_at,
        ended_at=session.ended_at,
        end_reason=session.end_reason.value if session.end_reason else None,
        energy_wh=session.energy_wh,
        power_w=session.power_w,
        cost_kobo=session.cost_kobo,
        server_now=utcnow(),
    )


@router.get("/sessions", response_model=list[OpsSessionOut])
async def list_sessions(
    db: DbSession,
    _: OperatorUser,
    station_id: uuid.UUID | None = None,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[OpsSessionOut]:
    rows = (
        await db.execute(
            ops.sessions_query(station_id=station_id, active_only=active_only).limit(limit)
        )
    ).all()
    return [_session_row(*row) for row in rows]


@router.post("/sessions/{session_id}/end", response_model=OpsSessionOut)
async def end_session(
    session_id: uuid.UUID, db: DbSession, actor: OperatorUser
) -> OpsSessionOut:
    """End someone else's session.

    Goes through the same service the customer's own "End session" uses, so
    the outlet is released and the booking closed by one code path. An
    operator shortcut that wrote the status directly is how outlets end up
    stuck IN_USE with no session behind them.
    """
    row = (
        await db.execute(ops.sessions_query().where(ChargingSession.id == session_id))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    session, outlet, station, user = row
    if session.status is SessionStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "That session has already ended")

    await session_service.end(db, session, SessionEndReason.OPERATOR_ENDED)
    await audit.record(
        db,
        actor=actor,
        action=AuditAction.SESSION_ENDED_BY_OPERATOR,
        target_type="session",
        target_id=session.id,
        target_label=f"{station.name} · {_outlet_label(outlet)}",
        summary=(
            f"Ended {user.full_name}'s session on {_outlet_label(outlet)} "
            f"at {station.name}"
        ),
    )
    await db.commit()
    await db.refresh(session)
    return _session_row(session, outlet, station, user)


@router.get("/reports", response_model=ReportOut)
async def reports(
    db: DbSession,
    _: OperatorUser,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> ReportOut:
    series = await ops.daily_series(db, days)
    by_station = await ops.station_totals(db, days)

    energy_wh = sum(row.energy_wh for row in series)
    est = impact.estimate(energy_wh)

    return ReportOut(
        days=days,
        sessions=sum(row.sessions for row in series),
        energy_wh=energy_wh,
        revenue_kobo=sum(row.revenue_kobo for row in series),
        series=[DayPointOut(**vars(row)) for row in series],
        by_station=[StationTotalsOut(**vars(row)) for row in by_station],
        impact=ImpactOut(**vars(est), assumptions=impact.ASSUMPTIONS),
        timezone_note=ops.REPORTING_TZ_NOTE,
    )


@router.get("/audit", response_model=list[AuditEventOut])
async def audit_trail(
    db: DbSession,
    _: OperatorUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventOut]:
    rows = (
        (await db.execute(select(AuditEvent).order_by(AuditEvent.at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return [AuditEventOut.model_validate(r) for r in rows]


# ------------------------------------------------------------- counter ---
# Writes the customer API deliberately does not expose. Everything below
# moves money or power, so everything below is audited.


@router.post("/bookings", response_model=WalkInOut, status_code=status.HTTP_201_CREATED)
async def book_walk_in(
    payload: WalkInIn, db: DbSession, actor: OperatorUser
) -> WalkInOut:
    """Book and take payment for someone standing at the counter.

    The pilot is a kiosk on a campus. People arrive with cash and a phone
    that has no data, and before this the platform had no way to serve them
    at all — which would have pushed those takings off the books rather than
    into a report.
    """
    try:
        result = await counter.book_walk_in(
            db,
            operator=actor,
            outlet_id=payload.outlet_id,
            plan_id=payload.plan_id,
            name=payload.name,
            phone=payload.phone,
            email=payload.email,
            method=payload.payment_method,
        )
    except (OutletUnavailable, PlanUnavailable, SlotTaken) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == payload.outlet_id))
    ).scalar_one()
    station = (
        await db.execute(select(Station).where(Station.id == outlet.station_id))
    ).scalar_one()

    await audit.record(
        db,
        actor=actor,
        action=AuditAction.WALK_IN_BOOKED,
        target_type="booking",
        target_id=result.booking.id,
        target_label=f"{station.name} \u00b7 {_outlet_label(outlet)}",
        summary=(
            f"Booked {_outlet_label(outlet)} at {station.name} for "
            f"{result.user.full_name} "
            f"({payload.payment_method.value}, \u20a6{result.booking.amount_kobo / 100:,.0f})"
        ),
    )
    await db.commit()

    return WalkInOut(
        booking_id=result.booking.id,
        access_code=result.access_code,
        customer_name=result.user.full_name,
        customer_phone=result.user.phone,
        new_customer=result.created_user,
        station_name=station.name,
        outlet_label=_outlet_label(outlet),
        starts_at=result.booking.starts_at,
        ends_at=result.booking.ends_at,
        amount_kobo=result.booking.amount_kobo,
        payment_method=payload.payment_method.value,
    )


@router.post("/sessions/{session_id}/extend", response_model=OpsSessionOut)
async def extend_session(
    session_id: uuid.UUID, payload: ExtendIn, db: DbSession, actor: OperatorUser
) -> OpsSessionOut:
    """Give a running session more time.

    Refused when the extra window collides with somebody else's booking —
    otherwise this quietly hands the next customer's slot to whoever is
    already standing there.
    """
    row = (
        await db.execute(ops.sessions_query().where(ChargingSession.id == session_id))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    session, outlet, station, user = row
    try:
        await counter.extend_session(db, session, payload.minutes)
    except counter.CounterError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await audit.record(
        db,
        actor=actor,
        action=AuditAction.SESSION_EXTENDED,
        target_type="session",
        target_id=session.id,
        target_label=f"{station.name} \u00b7 {_outlet_label(outlet)}",
        summary=(
            f"Extended {user.full_name}'s session on {_outlet_label(outlet)} "
            f"at {station.name} by {payload.minutes} minutes"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
    )
    await db.commit()
    await db.refresh(session)
    return _session_row(session, outlet, station, user)


@router.post("/bookings/{booking_id}/refund", response_model=dict)
async def refund_booking(
    booking_id: uuid.UUID, payload: RefundIn, db: DbSession, actor: OperatorUser
) -> dict:
    """Record money given back.

    Records it; it does not move it. The operator hands over cash, or raises
    the refund with the gateway. Claiming the platform had moved the money
    would be worse than recording nothing.
    """
    booking = (
        await db.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one_or_none()
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    try:
        await counter.refund(db, booking, kobo=payload.kobo, reason=payload.reason)
    except counter.CounterError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    customer = (
        await db.execute(select(User).where(User.id == booking.user_id))
    ).scalar_one()

    await audit.record(
        db,
        actor=actor,
        action=AuditAction.BOOKING_REFUNDED,
        target_type="booking",
        target_id=booking.id,
        target_label=customer.full_name,
        summary=(
            f"Refunded \u20a6{payload.kobo / 100:,.0f} to {customer.full_name} "
            f"({payload.reason})"
        ),
    )
    await db.commit()
    return {
        "booking_id": str(booking.id),
        "refund_kobo": booking.refund_kobo,
        "amount_kobo": booking.amount_kobo,
    }


@router.patch("/stations/{station_id}", response_model=OpsStationOut)
async def set_station_active(
    station_id: uuid.UUID, payload: StationActiveIn, db: DbSession, actor: OperatorUser
) -> OpsStationOut:
    """Open or close a whole hub.

    Closing hides it from the customer app immediately. It does not touch
    sessions already running — people mid-charge keep what they paid for, and
    the hub empties out on its own.
    """
    station = (
        await db.execute(select(Station).where(Station.id == station_id))
    ).scalar_one_or_none()
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Station not found")
    if payload.is_active is False and not (payload.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Say why the hub is closing"
        )

    station.is_active = payload.is_active
    await audit.record(
        db,
        actor=actor,
        action=(
            AuditAction.STATION_ACTIVATED
            if payload.is_active
            else AuditAction.STATION_DEACTIVATED
        ),
        target_type="station",
        target_id=station.id,
        target_label=station.name,
        summary=(
            f"{station.name} {'opened' if payload.is_active else 'closed'}"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
    )
    await db.commit()

    rows = await _station_rows(db)
    return next(r for r in rows if r.id == station_id)


@router.get("/lookup", response_model=LookupOut | None)
async def lookup_code(
    code: Annotated[str, Query(min_length=1, max_length=32)],
    db: DbSession,
    _: OperatorUser,
) -> LookupOut | None:
    """Answer "what is this PIN?" for somebody at the counter.

    Resolved by hash exactly the way redemption resolves it, so a code this
    screen calls valid is a code the keypad will accept.
    """
    found = await counter.find_by_code(db, code)
    if found is None:
        return None
    return LookupOut(
        booking_id=found.booking.id,
        station_name=found.station.name,
        outlet_label=_outlet_label(found.outlet),
        customer_name=found.user.full_name,
        customer_phone=found.user.phone,
        status=found.booking.status.value,
        code_state=found.code_state,
        starts_at=found.booking.starts_at,
        ends_at=found.booking.ends_at,
        amount_kobo=found.booking.amount_kobo,
        refund_kobo=found.booking.refund_kobo,
        session_id=found.session.id if found.session else None,
        session_status=found.session.status.value if found.session else None,
    )
