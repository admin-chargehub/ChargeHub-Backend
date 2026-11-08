"""Sessions must end when their time runs out, with nobody watching.

The bug these cover: `get_live` is what actually ends an expired session, and
it was reachable only from the two customer endpoints. A customer who closed
their tab left the session ACTIVE indefinitely — the operator console showed
it running, the booking never completed, and `end()` never released the
outlet. One session in the pilot database had been "running" for seven days.
"""

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models import (
    Booking,
    BookingStatus,
    ChargingSession,
    Outlet,
    OutletStatus,
    Plan,
    SessionEndReason,
    SessionStatus,
    Station,
    User,
    UserRole,
)
from app.models.base import utcnow
from app.services import sessions as svc


async def _expired_session(
    db: AsyncSession, *, index: int = 1, station: Station | None = None
) -> ChargingSession:
    """A session whose paid window closed an hour ago and that nobody ended."""
    if station is None:
        station = Station(name="UI Hub", slug="ui-hub")
        db.add(station)
        await db.flush()

    outlet = Outlet(
        station_id=station.id,
        index=index,
        label=f"A{index}",
        status=OutletStatus.IN_USE,
    )
    customer = User(
        email=f"rider{index}@example.com", full_name="Rider", hashed_password="x"
    )
    plan = Plan(
        name="Quick",
        slug=f"quick{index}",
        price_kobo=30000,
        max_duration_minutes=60,
        power_cap_w=500,
        energy_cap_wh=500,
    )
    db.add_all([outlet, customer, plan])
    await db.flush()

    started = utcnow() - timedelta(hours=2)
    ended = started + timedelta(hours=1)
    booking = Booking(
        user_id=customer.id,
        outlet_id=outlet.id,
        plan_id=plan.id,
        starts_at=started,
        ends_at=ended,
        status=BookingStatus.ACTIVE,
        amount_kobo=plan.price_kobo,
        paid_at=started,
    )
    db.add(booking)
    await db.flush()

    session = ChargingSession(
        booking_id=booking.id,
        outlet_id=outlet.id,
        started_at=started,
        ends_at=ended,
        status=SessionStatus.ACTIVE,
        cost_kobo=plan.price_kobo,
    )
    db.add(session)
    await db.flush()
    return session


async def test_sweeper_ends_a_session_whose_window_closed(db: AsyncSession):
    session = await _expired_session(db)
    await db.commit()

    assert await svc.sweep_expired(db) == 1

    await db.refresh(session)
    assert session.status is SessionStatus.COMPLETED
    assert session.end_reason is SessionEndReason.TIME_EXPIRED
    assert session.ended_at is not None


async def test_sweeper_releases_the_outlet_and_closes_the_booking(db: AsyncSession):
    """The part that actually costs money. A session left ACTIVE holds its
    outlet IN_USE, so the socket cannot be rebooked by anybody."""
    session = await _expired_session(db)
    await db.commit()

    await svc.sweep_expired(db)

    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == session.outlet_id))
    ).scalar_one()
    booking = (
        await db.execute(select(Booking).where(Booking.id == session.booking_id))
    ).scalar_one()
    assert outlet.status is OutletStatus.AVAILABLE
    assert booking.status is BookingStatus.COMPLETED


async def test_sweeper_leaves_a_running_session_alone(db: AsyncSession):
    session = await _expired_session(db)
    session.ends_at = utcnow() + timedelta(minutes=30)
    await db.commit()

    assert await svc.sweep_expired(db) == 0
    await db.refresh(session)
    assert session.status is SessionStatus.ACTIVE


async def test_sweeper_is_idempotent(db: AsyncSession):
    """Cron retries and overlapping runs must not double-end anything."""
    await _expired_session(db)
    await db.commit()

    assert await svc.sweep_expired(db) == 1
    assert await svc.sweep_expired(db) == 0


async def test_operator_sessions_list_does_not_report_a_finished_session_as_running(
    client: AsyncClient, db: AsyncSession
):
    """The reported bug, end to end.

    The operator opens Sessions. The customer has not touched their screen,
    so nothing has called `get_live` on their behalf. The list must still
    show the session as finished rather than running.
    """
    operator = User(
        email="op@example.com",
        full_name="Operator",
        hashed_password=hash_password("pw"),
        role=UserRole.OPERATOR,
    )
    db.add(operator)
    session = await _expired_session(db)
    await db.commit()

    res = await client.get(
        "/api/v1/ops/sessions",
        headers={"Authorization": f"Bearer {create_access_token(str(operator.id))}"},
    )
    assert res.status_code == 200
    row = next(r for r in res.json() if r["id"] == str(session.id))
    assert row["status"] == "completed"
    assert row["end_reason"] == "time_expired"


async def test_dashboard_does_not_count_a_finished_session_as_active(
    client: AsyncClient, db: AsyncSession
):
    operator = User(
        email="op2@example.com",
        full_name="Operator",
        hashed_password=hash_password("pw"),
        role=UserRole.OPERATOR,
    )
    db.add(operator)
    await _expired_session(db)
    await db.commit()

    res = await client.get(
        "/api/v1/ops/dashboard",
        headers={"Authorization": f"Bearer {create_access_token(str(operator.id))}"},
    )
    assert res.json()["overview"]["sessions_active"] == 0


async def test_sweep_endpoint_rejects_a_bad_token(client: AsyncClient):
    """It cancels bookings and ends sessions. An open one would be a way for
    anybody to terminate every charge on the network."""
    assert (await client.post("/api/v1/internal/sweep")).status_code in (401, 503)
    assert (
        await client.post(
            "/api/v1/internal/sweep", headers={"X-Internal-Token": "wrong"}
        )
    ).status_code in (401, 503)
