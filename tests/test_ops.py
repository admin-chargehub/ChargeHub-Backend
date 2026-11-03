"""Operator console: access control, the outlet guards, and the audit trail.

These are the tests worth having here. The aggregate endpoints mostly restate
SQL and would be tested by reimplementing the query in the assertion; the
guards encode judgement that a future edit could plausibly undo, and the RBAC
boundary is the one place where a mistake exposes customer identities.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models import (
    AuditEvent,
    Booking,
    BookingStatus,
    ChargingSession,
    Outlet,
    OutletStatus,
    Plan,
    SessionStatus,
    Station,
    StationTelemetry,
    User,
    UserRole,
)
from app.models.base import utcnow
from app.services import impact


async def _user(db: AsyncSession, email: str, role: UserRole) -> User:
    user = User(
        email=email,
        full_name=f"{role.value.title()} User",
        hashed_password=hash_password("pw"),
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _station(db: AsyncSession, *, outlets: int = 2) -> Station:
    station = Station(name="UI Hub", slug="ui-hub")
    db.add(station)
    await db.flush()
    for i in range(1, outlets + 1):
        db.add(Outlet(station_id=station.id, index=i, label=f"A{i}"))
    db.add(StationTelemetry(station_id=station.id))
    await db.flush()
    return station


# --------------------------------------------------------------- access ---


async def test_anonymous_is_rejected(client: AsyncClient):
    assert (await client.get("/api/v1/ops/dashboard")).status_code == 401


async def test_customer_is_forbidden(client: AsyncClient, db: AsyncSession):
    """The console exposes customer names, emails and site revenue. A signed-in
    customer reaching it would be a data breach, not a UI bug."""
    customer = await _user(db, "c@example.com", UserRole.CUSTOMER)
    await db.commit()

    res = await client.get("/api/v1/ops/dashboard", headers=_auth(customer))
    assert res.status_code == 403


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.ADMIN])
async def test_operator_and_admin_are_allowed(
    client: AsyncClient, db: AsyncSession, role: UserRole
):
    user = await _user(db, f"{role.value}@example.com", role)
    await _station(db)
    await db.commit()

    res = await client.get("/api/v1/ops/dashboard", headers=_auth(user))
    assert res.status_code == 200
    assert res.json()["overview"]["stations_total"] == 1


async def test_technician_is_forbidden(client: AsyncClient, db: AsyncSession):
    """Technicians get their own flow later. Until it exists they must not
    inherit the operator's view of revenue by default."""
    tech = await _user(db, "t@example.com", UserRole.TECHNICIAN)
    await db.commit()

    assert (
        await client.get("/api/v1/ops/dashboard", headers=_auth(tech))
    ).status_code == 403


# -------------------------------------------------------- outlet guards ---


async def test_lifecycle_statuses_cannot_be_set_by_hand(
    client: AsyncClient, db: AsyncSession
):
    op = await _user(db, "op1@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await db.commit()

    res = await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}",
        json={"status": "in_use"},
        headers=_auth(op),
    )
    assert res.status_code == 422
    assert "booking lifecycle" in res.json()["detail"]


async def test_taking_out_of_service_requires_a_reason(
    client: AsyncClient, db: AsyncSession
):
    op = await _user(db, "op2@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await db.commit()

    res = await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}", json={"status": "fault"}, headers=_auth(op)
    )
    assert res.status_code == 422


async def test_returning_to_available_clears_the_reason(
    client: AsyncClient, db: AsyncSession
):
    """A stale fault note under a working socket sends the next technician to
    chase a problem that was already fixed."""
    op = await _user(db, "op3@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await db.commit()

    await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}",
        json={"status": "fault", "reason": "Burnt pin"},
        headers=_auth(op),
    )
    res = await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}", json={"status": "available"}, headers=_auth(op)
    )
    assert res.status_code == 200
    assert res.json()["fault_reason"] is None


async def _live_session(db: AsyncSession, station: Station, outlet: Outlet):
    customer = await _user(db, f"cust{outlet.index}@example.com", UserRole.CUSTOMER)
    plan = Plan(
        name="Quick",
        slug=f"quick{outlet.index}",
        price_kobo=30000,
        max_duration_minutes=60,
        power_cap_w=500,
        energy_cap_wh=500,
    )
    db.add(plan)
    await db.flush()

    now = utcnow()
    booking = Booking(
        user_id=customer.id,
        outlet_id=outlet.id,
        plan_id=plan.id,
        starts_at=now,
        ends_at=now + __import__("datetime").timedelta(hours=1),
        status=BookingStatus.ACTIVE,
        amount_kobo=30000,
        paid_at=now,
    )
    db.add(booking)
    await db.flush()

    session = ChargingSession(
        booking_id=booking.id,
        outlet_id=outlet.id,
        started_at=now,
        ends_at=booking.ends_at,
        status=SessionStatus.ACTIVE,
        cost_kobo=30000,
    )
    outlet.status = OutletStatus.IN_USE
    db.add(session)
    await db.flush()
    return session


async def test_busy_outlet_is_refused_without_force(
    client: AsyncClient, db: AsyncSession
):
    op = await _user(db, "op4@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await _live_session(db, station, outlet)
    await db.commit()

    res = await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}",
        json={"status": "fault", "reason": "Sparking"},
        headers=_auth(op),
    )
    assert res.status_code == 409


async def test_force_ends_the_session_rather_than_orphaning_it(
    client: AsyncClient, db: AsyncSession
):
    """The important one. Flipping the outlet without closing the session
    would leave a booking marked ACTIVE against a socket nobody can use, and
    the customer's app would keep counting down against dead copper."""
    op = await _user(db, "op5@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    session = await _live_session(db, station, outlet)
    await db.commit()

    res = await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}",
        json={"status": "fault", "reason": "Sparking", "force": True},
        headers=_auth(op),
    )
    assert res.status_code == 200

    await db.refresh(session)
    assert session.status is SessionStatus.COMPLETED
    assert session.ended_at is not None

    booking = (
        await db.execute(select(Booking).where(Booking.id == session.booking_id))
    ).scalar_one()
    assert booking.status is BookingStatus.COMPLETED


# ----------------------------------------------------------------- audit ---


async def test_outlet_change_is_audited_with_actor_and_reason(
    client: AsyncClient, db: AsyncSession
):
    op = await _user(db, "op6@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await db.commit()

    await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}",
        json={"status": "fault", "reason": "Burnt pin"},
        headers=_auth(op),
    )

    events = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].actor_email == "op6@example.com"
    assert "Burnt pin" in events[0].summary
    assert "available" in events[0].summary and "fault" in events[0].summary


async def test_a_rejected_change_writes_no_audit_row(
    client: AsyncClient, db: AsyncSession
):
    """An audit trail that records attempts which did not happen is worse than
    none, because it is believed."""
    op = await _user(db, "op7@example.com", UserRole.OPERATOR)
    station = await _station(db)
    outlet = (
        await db.execute(select(Outlet).where(Outlet.station_id == station.id))
    ).scalars().first()
    await db.commit()

    await client.patch(
        f"/api/v1/ops/outlets/{outlet.id}", json={"status": "fault"}, headers=_auth(op)
    )

    assert (await db.execute(select(AuditEvent))).scalars().all() == []


# ---------------------------------------------------------------- impact ---


def test_impact_is_zero_for_zero_energy():
    est = impact.estimate(0)
    assert est.litres_displaced == 0
    assert est.kg_co2_avoided == 0


def test_impact_scales_from_published_constants():
    """Pinned so a change to the constants is a deliberate edit to this test,
    not a silent shift in a number that goes into funding applications."""
    est = impact.estimate(3_500)  # 3.5 kWh = exactly one litre
    assert est.litres_displaced == pytest.approx(1.0)
    assert est.kg_co2_avoided == pytest.approx(impact.KG_CO2_PER_LITRE)


def test_impact_assumptions_are_published_with_the_numbers():
    assert "Not a measurement" in impact.ASSUMPTIONS
