"""Access-code redemption and the session it opens.

The redemption ordering here is the one in ChargeHub-Contracts
`docs/ACCESS_CODE_FORMAT.md` §4, and the tests assert the part that is easy to
get wrong: which failures cost an attempt and which are free.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessCode,
    BookingStatus,
    Outlet,
    OutletStatus,
    Plan,
    SessionEndReason,
    SessionStatus,
    Station,
    User,
)
from app.models.base import utcnow
from app.services import bookings as booking_svc
from app.services import sessions as svc
from app.vendor import chargehub_codes as codes


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is process-global, so one test's failures would lock out the
    next. Reset around every test."""
    svc._failures.clear()
    yield
    svc._failures.clear()


async def _paid_booking(db: AsyncSession, *, outlet_index: int = 1):
    user = User(email=f"r{outlet_index}@example.com", full_name="Rider", hashed_password="x")
    station = Station(name="UI Hub", slug="ui-hub")
    plan = Plan(
        name="Quick",
        slug="quick",
        power_cap_w=80,
        energy_cap_wh=80,
        max_duration_minutes=60,
        price_kobo=10000,
    )
    db.add_all([user, station, plan])
    await db.flush()
    for i in range(1, 4):
        db.add(Outlet(station_id=station.id, index=i, label=f"A{i}"))
    await db.flush()

    outlet = (
        await db.execute(
            select(Outlet)
            .where(Outlet.station_id == station.id)
            .where(Outlet.index == outlet_index)
        )
    ).scalar_one()

    booking = await booking_svc.create_booking(
        db, user=user, outlet_id=outlet.id, plan_id=plan.id
    )
    booking, code = await booking_svc.pay_booking(db, booking, user)
    return user, station, plan, outlet, booking, code


class TestRedeem:
    async def test_a_valid_code_opens_a_session(self, db):
        _, station, _, outlet, booking, code = await _paid_booking(db)

        session = await svc.redeem(db, station_slug=station.slug, code=code)

        assert session.status is SessionStatus.ACTIVE
        assert session.outlet_id == outlet.id
        # This is the only thing in the system that sets ACTIVE.
        assert booking.status is BookingStatus.ACTIVE
        assert outlet.status is OutletStatus.IN_USE

    async def test_the_display_form_is_accepted(self, db):
        _, station, _, _, _, code = await _paid_booking(db)

        session = await svc.redeem(
            db, station_slug=station.slug, code=codes.format_for_display(code)
        )
        assert session.status is SessionStatus.ACTIVE

    async def test_the_window_runs_from_redemption_not_from_booking(self, db):
        """Arriving late must not cost the customer part of their hour."""
        _, station, plan, _, booking, code = await _paid_booking(db)

        session = await svc.redeem(db, station_slug=station.slug, code=code)

        granted = session.ends_at - session.started_at
        assert granted == timedelta(minutes=plan.max_duration_minutes)

    async def test_a_malformed_code_costs_no_attempt(self, db):
        """A typo is not a guess. Burning the budget on mistyping would lock
        out honest customers."""
        _, station, _, _, _, _ = await _paid_booking(db)

        with pytest.raises(svc.MalformedCode) as exc:
            await svc.redeem(db, station_slug=station.slug, code="12345")

        assert exc.value.costs_attempt is False
        assert svc._failures.get(station.slug, []) == []

    async def test_a_well_formed_unknown_code_does_cost_an_attempt(self, db):
        _, station, _, _, _, _ = await _paid_booking(db)
        decoy = codes.generate()

        with pytest.raises(svc.UnknownCode):
            await svc.redeem(db, station_slug=station.slug, code=decoy)

        assert len(svc._failures.get(station.slug, [])) == 1

    async def test_repeated_failures_trip_the_rate_limit(self, db):
        _, station, _, _, _, _ = await _paid_booking(db)

        for _ in range(5):
            with pytest.raises(svc.UnknownCode):
                await svc.redeem(db, station_slug=station.slug, code=codes.generate())

        with pytest.raises(svc.RateLimited):
            await svc.redeem(db, station_slug=station.slug, code=codes.generate())

    async def test_a_code_cannot_be_used_twice(self, db):
        _, station, _, _, _, code = await _paid_booking(db)
        await svc.redeem(db, station_slug=station.slug, code=code)

        with pytest.raises(svc.CodeAlreadyUsed):
            await svc.redeem(db, station_slug=station.slug, code=code)

    async def test_an_expired_code_is_refused(self, db):
        _, station, _, _, booking, code = await _paid_booking(db)
        record = (
            await db.execute(
                select(AccessCode).where(AccessCode.booking_id == booking.id)
            )
        ).scalar_one()
        record.not_after = utcnow() - timedelta(seconds=1)
        await db.flush()

        with pytest.raises(svc.CodeExpired):
            await svc.redeem(db, station_slug=station.slug, code=code)

    async def test_a_code_from_another_hub_is_not_recognised(self, db):
        """Codes are station-scoped, per spec §7."""
        _, _, _, _, _, code = await _paid_booking(db)
        other = Station(name="Zik Hub", slug="zik-hub")
        db.add(other)
        await db.flush()

        with pytest.raises(svc.UnknownCode):
            await svc.redeem(db, station_slug=other.slug, code=code)

    async def test_a_faulty_outlet_refuses_without_costing_an_attempt(self, db):
        _, station, _, outlet, _, code = await _paid_booking(db)
        outlet.status = OutletStatus.FAULT
        await db.flush()

        with pytest.raises(svc.OutletNotUsable) as exc:
            await svc.redeem(db, station_slug=station.slug, code=code)
        assert exc.value.costs_attempt is False


class TestLifecycle:
    async def test_ending_releases_the_outlet_and_completes_the_booking(self, db):
        _, station, _, outlet, booking, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)

        await svc.end(db, session, SessionEndReason.USER_ENDED)

        assert session.status is SessionStatus.COMPLETED
        assert session.end_reason is SessionEndReason.USER_ENDED
        assert session.ended_at is not None
        assert booking.status is BookingStatus.COMPLETED
        assert outlet.status is OutletStatus.AVAILABLE

    async def test_ending_twice_is_harmless(self, db):
        _, station, _, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)

        await svc.end(db, session, SessionEndReason.USER_ENDED)
        first_ended_at = session.ended_at
        await svc.end(db, session, SessionEndReason.OPERATOR_ENDED)

        assert session.ended_at == first_ended_at
        assert session.end_reason is SessionEndReason.USER_ENDED

    async def test_a_session_past_its_window_ends_itself(self, db):
        _, station, _, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)
        session.ends_at = utcnow() - timedelta(seconds=1)
        await db.flush()

        await svc.get_live(db, session)

        assert session.status is SessionStatus.COMPLETED
        assert session.end_reason is SessionEndReason.TIME_EXPIRED

    async def test_reaching_the_energy_cap_ends_the_session(self, db):
        """The `energy_exhausted` path the prototype had but never reached."""
        _, station, plan, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)
        session.energy_wh = plan.energy_cap_wh
        await db.flush()

        await svc.get_live(db, session)

        assert session.status is SessionStatus.COMPLETED
        assert session.end_reason is SessionEndReason.ENERGY_CAP

    async def test_simulated_energy_never_exceeds_the_paid_cap(self, db):
        _, station, plan, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)
        # Pretend the session started long ago; the simulator must still clamp.
        session.started_at = utcnow() - timedelta(minutes=59)
        await db.flush()

        await svc._simulate_progress(db, session)

        assert session.energy_wh <= plan.energy_cap_wh

    async def test_remaining_is_zero_once_completed(self, db):
        _, station, _, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)
        await svc.end(db, session, SessionEndReason.USER_ENDED)

        assert svc.remaining_seconds(session) == 0


class TestOwnership:
    async def test_another_users_session_is_not_visible(self, db):
        user, station, _, _, _, code = await _paid_booking(db)
        session = await svc.redeem(db, station_slug=station.slug, code=code)

        intruder = User(email="nosy@example.com", full_name="Nosy", hashed_password="x")
        db.add(intruder)
        await db.flush()

        assert await svc.get_for_user(db, intruder.id, session.id) is None
        assert await svc.get_for_user(db, user.id, session.id) is not None
