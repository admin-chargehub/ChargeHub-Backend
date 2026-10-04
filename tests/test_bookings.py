"""Booking creation, payment and expiry.

These run on SQLite, so they cover *logic* only. Two of the three concurrency
mechanisms do not exist here: SQLAlchemy silently drops `FOR UPDATE` on SQLite,
and the PostgreSQL exclusion constraint is declared only in the migration. A
green run here does not prove two simultaneous requests can't double-book —
that needs an integration test against real PostgreSQL.
"""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessCode, Booking, BookingStatus, Outlet, Plan, Station, User
from app.models.base import utcnow
from app.services import access_codes
from app.services import bookings as svc
from app.services.availability import find_earliest_slot, is_outlet_free
from app.vendor import chargehub_codes as codes


async def _fixtures(db: AsyncSession, *, outlets: int = 3) -> tuple[User, Station, Plan]:
    user = User(email="rider@example.com", full_name="Rider", hashed_password="x")
    station = Station(name="Pilot", slug="pilot")
    plan = Plan(
        name="Standard",
        slug="standard",
        power_cap_w=200,
        energy_cap_wh=400,
        max_duration_minutes=120,
        price_kobo=100000,
    )
    db.add_all([user, station, plan])
    await db.flush()
    for i in range(1, outlets + 1):
        db.add(Outlet(station_id=station.id, index=i, label=f"A{i}"))
    await db.flush()
    return user, station, plan


async def _outlet(db: AsyncSession, station: Station, index: int) -> Outlet:
    from sqlalchemy import select

    return (
        await db.execute(
            select(Outlet).where(Outlet.station_id == station.id).where(Outlet.index == index)
        )
    ).scalar_one()


class TestCreate:
    async def test_creates_a_pending_booking(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)

        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        assert booking.status is BookingStatus.PENDING
        assert booking.amount_kobo == plan.price_kobo
        assert booking.pending_expires_at is not None

    async def test_window_comes_from_the_plan_not_the_client(self, db):
        """The client cannot choose how long it books for."""
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)

        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        assert booking.ends_at - booking.starts_at == timedelta(
            minutes=plan.max_duration_minutes
        )

    async def test_second_booking_on_the_same_outlet_is_refused(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)

        await svc.create_booking(db, user=user, outlet_id=outlet.id, plan_id=plan.id)

        with pytest.raises(svc.SlotTaken):
            await svc.create_booking(db, user=user, outlet_id=outlet.id, plan_id=plan.id)

    async def test_a_different_outlet_is_still_free(self, db):
        user, station, plan = await _fixtures(db)
        first = await _outlet(db, station, 1)
        second = await _outlet(db, station, 2)

        await svc.create_booking(db, user=user, outlet_id=first.id, plan_id=plan.id)
        other = await svc.create_booking(db, user=user, outlet_id=second.id, plan_id=plan.id)

        assert other.outlet_id == second.id

    async def test_repeating_an_idempotency_key_returns_the_same_booking(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)

        first = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id, idempotency_key="k1"
        )
        again = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id, idempotency_key="k1"
        )

        assert again.id == first.id

    async def test_unknown_plan_is_rejected(self, db):
        import uuid

        user, station, _ = await _fixtures(db)
        outlet = await _outlet(db, station, 1)

        with pytest.raises(svc.PlanUnavailable):
            await svc.create_booking(
                db, user=user, outlet_id=outlet.id, plan_id=uuid.uuid4()
            )


class TestPay:
    async def test_payment_confirms_and_issues_a_contract_valid_code(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        booking, code = await svc.pay_booking(db, booking, user)

        assert booking.status is BookingStatus.CONFIRMED
        assert booking.pending_expires_at is None
        assert codes.is_valid(code), f"issued a code the firmware would reject: {code}"

    async def test_the_plaintext_code_is_never_stored(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )
        _, code = await svc.pay_booking(db, booking, user)

        record = await access_codes.for_booking(db, booking.id)
        assert record is not None
        assert code not in record.code_encrypted
        assert code not in record.code_sha256
        # …but the platform can still read it back for the "Send PIN" flow.
        assert access_codes.reveal(record) == code

    async def test_paying_twice_returns_the_same_code(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        _, first = await svc.pay_booking(db, booking, user)
        _, again = await svc.pay_booking(db, booking, user)

        assert first == again

    async def test_an_expired_hold_cannot_be_paid_for(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )
        booking.pending_expires_at = utcnow() - timedelta(seconds=1)
        await db.flush()

        with pytest.raises(svc.NotPayable):
            await svc.pay_booking(db, booking, user)
        assert booking.status is BookingStatus.CANCELLED


class TestPendingExpiry:
    async def test_an_expired_hold_stops_blocking_its_outlet(self, db):
        """The whole point of the TTL: an abandoned checkout must not hold an
        outlet forever."""
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        assert not await is_outlet_free(db, outlet.id, booking.starts_at, booking.ends_at)

        booking.pending_expires_at = utcnow() - timedelta(seconds=1)
        await db.flush()

        assert await is_outlet_free(db, outlet.id, booking.starts_at, booking.ends_at)

    async def test_a_paid_booking_keeps_blocking(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )
        await svc.pay_booking(db, booking, user)

        assert not await is_outlet_free(db, outlet.id, booking.starts_at, booking.ends_at)

    async def test_availability_skips_an_outlet_held_by_a_live_pending(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        await svc.create_booking(db, user=user, outlet_id=outlet.id, plan_id=plan.id)

        slot = await find_earliest_slot(db, station.id, 60, utcnow())
        assert slot is not None
        assert slot.outlet_index == 2

    async def test_sweep_cancels_expired_holds(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )
        booking.pending_expires_at = utcnow() - timedelta(seconds=1)
        await db.flush()

        assert await svc.sweep_pending(db) == 1
        assert booking.status is BookingStatus.CANCELLED
        # Second run finds nothing left to do.
        assert await svc.sweep_pending(db) == 0


class TestOwnership:
    async def test_another_users_booking_is_not_visible(self, db):
        user, station, plan = await _fixtures(db)
        outlet = await _outlet(db, station, 1)
        booking = await svc.create_booking(
            db, user=user, outlet_id=outlet.id, plan_id=plan.id
        )

        intruder = User(email="nosy@example.com", full_name="Nosy", hashed_password="x")
        db.add(intruder)
        await db.flush()

        assert await svc.get_for_user(db, intruder, booking.id) is None
        assert await svc.get_for_user(db, user, booking.id) is not None


async def test_access_code_table_is_cleaned_up_with_the_booking(db):
    """Sanity check on the FK cascade — orphaned codes would be redeemable."""
    from sqlalchemy import select

    user, station, plan = await _fixtures(db)
    outlet = await _outlet(db, station, 1)
    booking = await svc.create_booking(db, user=user, outlet_id=outlet.id, plan_id=plan.id)
    await svc.pay_booking(db, booking, user)

    assert (await db.execute(select(AccessCode))).scalars().all()
    assert (await db.execute(select(Booking))).scalars().all()
