"""Operator writes the customer API deliberately does not expose.

These are the paths where an operator moves money or power on somebody else's
behalf, so the tests are about the guards rather than the happy path: who can
be billed twice, whose slot can be given away, and which socket must never
have a PIN issued against it.
"""

import uuid as uuidlib
from datetime import timedelta

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
    PaymentMethod,
    Plan,
    SessionStatus,
    Station,
    User,
    UserRole,
)
from app.models.base import utcnow
from app.services import counter


async def _operator(db: AsyncSession, email: str = "op@example.com") -> User:
    user = User(
        email=email,
        full_name="Site Operator",
        hashed_password=hash_password("pw"),
        role=UserRole.OPERATOR,
    )
    db.add(user)
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _hub(db: AsyncSession, *, outlets: int = 2) -> tuple[Station, list[Outlet], Plan]:
    station = Station(name="UI Hub", slug="ui-hub")
    db.add(station)
    await db.flush()

    rows = []
    for i in range(1, outlets + 1):
        outlet = Outlet(station_id=station.id, index=i, label=f"A{i}")
        db.add(outlet)
        rows.append(outlet)

    plan = Plan(
        name="Quick",
        slug="quick",
        price_kobo=30000,
        max_duration_minutes=60,
        power_cap_w=500,
        energy_cap_wh=500,
    )
    db.add(plan)
    await db.flush()
    return station, rows, plan


# ------------------------------------------------------------- walk-ins ---


async def test_walk_in_is_confirmed_and_issues_a_code(
    client: AsyncClient, db: AsyncSession
):
    """Cash is already in the operator's hand. There is nothing to authorise,
    so the booking is CONFIRMED and the PIN exists immediately — a pending
    booking would hold the outlet against money that is already paid."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    await db.commit()

    res = await client.post(
        "/api/v1/ops/bookings",
        json={
            "outlet_id": str(outlets[0].id),
            "plan_id": str(plan.id),
            "name": "Adunni Bello",
            "phone": "08031234567",
            "payment_method": "cash",
        },
        headers=_auth(op),
    )
    assert res.status_code == 201
    body = res.json()
    assert body["new_customer"] is True
    assert len(body["access_code"]) == 10

    booking = (
        await db.execute(
            select(Booking).where(Booking.id == uuidlib.UUID(body["booking_id"]))
        )
    ).scalar_one()
    assert booking.status is BookingStatus.CONFIRMED
    assert booking.payment_method is PaymentMethod.CASH
    assert booking.paid_at is not None
    # Who took the cash — the whole point of being able to reconcile a till.
    assert booking.created_by_id == op.id


async def test_second_visit_reuses_the_customer_by_phone(
    client: AsyncClient, db: AsyncSession
):
    """Otherwise a regular accumulates a new record per visit and no history
    attaches to anybody."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    await db.commit()

    payload = {
        "plan_id": str(plan.id),
        "name": "Adunni Bello",
        "phone": "08031234567",
        "payment_method": "cash",
    }
    first = await client.post(
        "/api/v1/ops/bookings",
        json={**payload, "outlet_id": str(outlets[0].id)},
        headers=_auth(op),
    )
    second = await client.post(
        "/api/v1/ops/bookings",
        json={**payload, "outlet_id": str(outlets[1].id)},
        headers=_auth(op),
    )

    assert first.json()["new_customer"] is True
    assert second.json()["new_customer"] is False

    customers = (
        (await db.execute(select(User).where(User.phone == "08031234567")))
        .scalars()
        .all()
    )
    assert len(customers) == 1


async def test_walk_in_customer_cannot_log_in(client: AsyncClient, db: AsyncSession):
    """A counter record must not become an account somebody can get into. The
    stored hash is unusable, so no password verifies against it."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    await db.commit()

    await client.post(
        "/api/v1/ops/bookings",
        json={
            "outlet_id": str(outlets[0].id),
            "plan_id": str(plan.id),
            "name": "Adunni Bello",
            "phone": "08031234567",
        },
        headers=_auth(op),
    )

    created = (
        (await db.execute(select(User).where(User.phone == "08031234567")))
        .scalars()
        .one()
    )
    assert created.hashed_password == counter.UNUSABLE_PASSWORD

    # The synthetic address sits on .invalid, reserved by RFC 2606 and
    # therefore never deliverable or registrable. Login rejects it at
    # validation, before credentials are even considered — which is a
    # stronger guarantee than a password check, not a weaker one.
    assert created.email.endswith(".invalid")
    res = await client.post(
        "/api/v1/auth/login",
        json={"email": created.email, "password": counter.UNUSABLE_PASSWORD},
    )
    assert res.status_code >= 400


async def test_no_pin_is_issued_for_an_outlet_out_of_service(
    client: AsyncClient, db: AsyncSession
):
    """The safety one. Handing somebody a code for a socket marked faulty
    sends them to plug a laptop into known-broken mains."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    outlets[0].status = OutletStatus.FAULT
    outlets[0].fault_reason = "Burnt pin"
    await db.commit()

    res = await client.post(
        "/api/v1/ops/bookings",
        json={
            "outlet_id": str(outlets[0].id),
            "plan_id": str(plan.id),
            "name": "Adunni Bello",
        },
        headers=_auth(op),
    )
    assert res.status_code == 409
    assert "fault" in res.json()["detail"]


# -------------------------------------------------------------- extend ---


async def _running_session(
    db: AsyncSession, outlet: Outlet, plan: Plan, *, email: str = "rider@example.com"
) -> ChargingSession:
    customer = User(email=email, full_name="Rider", hashed_password="x")
    db.add(customer)
    await db.flush()

    now = utcnow()
    booking = Booking(
        user_id=customer.id,
        outlet_id=outlet.id,
        plan_id=plan.id,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        status=BookingStatus.ACTIVE,
        amount_kobo=plan.price_kobo,
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
        cost_kobo=plan.price_kobo,
    )
    db.add(session)
    outlet.status = OutletStatus.IN_USE
    await db.flush()
    return session


async def test_extend_moves_the_booking_too(client: AsyncClient, db: AsyncSession):
    """Extending only the session would leave the booking — which is what
    availability is computed from — still ending at the old time, so the
    outlet would look free while somebody is still charging on it."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    session = await _running_session(db, outlets[0], plan)
    original = session.ends_at
    await db.commit()

    res = await client.post(
        f"/api/v1/ops/sessions/{session.id}/extend",
        json={"minutes": 30},
        headers=_auth(op),
    )
    assert res.status_code == 200

    await db.refresh(session)
    booking = (
        await db.execute(select(Booking).where(Booking.id == session.booking_id))
    ).scalar_one()
    assert session.ends_at == original + timedelta(minutes=30)
    assert booking.ends_at == session.ends_at


async def test_extend_is_refused_when_it_would_take_someone_elses_slot(
    client: AsyncClient, db: AsyncSession
):
    """The guard that makes extend safe to offer at all."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    session = await _running_session(db, outlets[0], plan)

    nxt = User(email="next@example.com", full_name="Next", hashed_password="x")
    db.add(nxt)
    await db.flush()
    db.add(
        Booking(
            user_id=nxt.id,
            outlet_id=outlets[0].id,
            plan_id=plan.id,
            starts_at=session.ends_at,
            ends_at=session.ends_at + timedelta(hours=1),
            status=BookingStatus.CONFIRMED,
            amount_kobo=plan.price_kobo,
            paid_at=utcnow(),
        )
    )
    await db.commit()

    res = await client.post(
        f"/api/v1/ops/sessions/{session.id}/extend",
        json={"minutes": 30},
        headers=_auth(op),
    )
    assert res.status_code == 409
    assert "booked" in res.json()["detail"]


# -------------------------------------------------------------- refund ---


async def test_refund_cannot_exceed_what_was_paid(client: AsyncClient, db: AsyncSession):
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    session = await _running_session(db, outlets[0], plan)
    await db.commit()

    res = await client.post(
        f"/api/v1/ops/bookings/{session.booking_id}/refund",
        json={"kobo": 50000, "reason": "Outlet died"},
        headers=_auth(op),
    )
    assert res.status_code == 409


async def test_partial_refunds_accumulate_and_then_stop(
    client: AsyncClient, db: AsyncSession
):
    """Two half refunds are allowed; a third is not. Without the running
    total, repeated partials would quietly pay out more than was taken."""
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    session = await _running_session(db, outlets[0], plan)
    await db.commit()

    url = f"/api/v1/ops/bookings/{session.booking_id}/refund"
    body = {"kobo": 15000, "reason": "Half the session was lost"}

    assert (await client.post(url, json=body, headers=_auth(op))).status_code == 200
    second = await client.post(url, json=body, headers=_auth(op))
    assert second.status_code == 200
    assert second.json()["refund_kobo"] == 30000
    assert (await client.post(url, json=body, headers=_auth(op))).status_code == 409


async def test_refund_requires_a_reason(client: AsyncClient, db: AsyncSession):
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    session = await _running_session(db, outlets[0], plan)
    await db.commit()

    res = await client.post(
        f"/api/v1/ops/bookings/{session.booking_id}/refund",
        json={"kobo": 1000, "reason": ""},
        headers=_auth(op),
    )
    assert res.status_code == 422


# -------------------------------------------------------------- lookup ---


async def test_lookup_resolves_a_pin_to_its_owner(client: AsyncClient, db: AsyncSession):
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    await db.commit()

    created = await client.post(
        "/api/v1/ops/bookings",
        json={
            "outlet_id": str(outlets[0].id),
            "plan_id": str(plan.id),
            "name": "Adunni Bello",
            "phone": "08031234567",
        },
        headers=_auth(op),
    )
    code = created.json()["access_code"]

    res = await client.get(f"/api/v1/ops/lookup?code={code}", headers=_auth(op))
    assert res.status_code == 200
    body = res.json()
    assert body["customer_name"] == "Adunni Bello"
    assert body["code_state"] == "valid"


async def test_lookup_of_an_unknown_pin_returns_nothing(
    client: AsyncClient, db: AsyncSession
):
    op = await _operator(db)
    await db.commit()
    res = await client.get("/api/v1/ops/lookup?code=0000000000", headers=_auth(op))
    assert res.status_code == 200
    assert res.json() is None


# ------------------------------------------------------------- customer ---


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("post", "/api/v1/ops/bookings", {}),
        ("get", "/api/v1/ops/lookup?code=1234567890", None),
    ],
)
async def test_customers_cannot_reach_counter_writes(
    client: AsyncClient, db: AsyncSession, method: str, url: str, body: dict | None
):
    """A customer reaching /ops/lookup could resolve any PIN to the name and
    phone number behind it."""
    customer = User(
        email="c@example.com",
        full_name="Customer",
        hashed_password=hash_password("pw"),
        role=UserRole.CUSTOMER,
    )
    db.add(customer)
    await db.commit()

    call = getattr(client, method)
    res = await (call(url, json=body, headers=_auth(customer)) if body is not None
                 else call(url, headers=_auth(customer)))
    assert res.status_code == 403


async def test_every_counter_write_is_audited(client: AsyncClient, db: AsyncSession):
    op = await _operator(db)
    _station, outlets, plan = await _hub(db)
    await db.commit()

    created = await client.post(
        "/api/v1/ops/bookings",
        json={
            "outlet_id": str(outlets[0].id),
            "plan_id": str(plan.id),
            "name": "Adunni Bello",
        },
        headers=_auth(op),
    )
    await client.post(
        f"/api/v1/ops/bookings/{created.json()['booking_id']}/refund",
        json={"kobo": 10000, "reason": "Goodwill"},
        headers=_auth(op),
    )

    events = (await db.execute(select(AuditEvent))).scalars().all()
    actions = {e.action.value for e in events}
    assert "walk_in_booked" in actions
    assert "booking_refunded" in actions
    assert all(e.actor_email == op.email for e in events)
