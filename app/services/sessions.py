"""Redeeming an access code and running the session it opens.

The redemption flow follows ChargeHub-Contracts `docs/ACCESS_CODE_FORMAT.md` §4,
including which failures cost the caller an attempt and which do not. Today the
caller is the API; eventually it is the station's keypad, and the ordering is
the same either way.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    AccessCode,
    Booking,
    BookingStatus,
    ChargingSession,
    Outlet,
    OutletStatus,
    Plan,
    SessionEndReason,
    SessionStatus,
    Station,
)
from app.models.base import utcnow
from app.services import access_codes
from app.vendor import chargehub_codes as codes


class RedemptionError(Exception):
    """A redemption that failed for a reason the keypad should explain."""

    #: Whether this failure counts against the brute-force budget. A typo is
    #: not a guess: malformed input must not burn an attempt, or a clumsy
    #: customer locks themselves out.
    costs_attempt = False


class MalformedCode(RedemptionError):
    pass


class RateLimited(RedemptionError):
    pass


class UnknownCode(RedemptionError):
    costs_attempt = True


class CodeNotYetValid(RedemptionError):
    costs_attempt = True


class CodeExpired(RedemptionError):
    costs_attempt = True


class CodeAlreadyUsed(RedemptionError):
    costs_attempt = True


class OutletNotUsable(RedemptionError):
    pass


# --- rate limiting --------------------------------------------------------
#
# Spec §5 puts the real budget on the station gateway, so it still applies when
# the station is offline — which is exactly when an attacker would try. This
# in-process counter is the cloud-side backstop only: it is per-worker, it does
# not survive a restart, and it is NOT the control the spec describes. The HGC
# implementation is the one that counts.

_MAX_FAILURES = 5
_WINDOW_SECONDS = 15 * 60
_LOCKOUT_SECONDS = 60
_failures: dict[str, list[float]] = {}


def _rate_key(station_slug: str) -> str:
    return station_slug


def _check_rate_limit(station_slug: str) -> None:
    key = _rate_key(station_slug)
    now = time.monotonic()
    recent = [t for t in _failures.get(key, []) if now - t < _WINDOW_SECONDS]
    _failures[key] = recent
    if len(recent) >= _MAX_FAILURES and now - recent[-1] < _LOCKOUT_SECONDS:
        raise RateLimited("Too many attempts. Wait a minute and try again.")


def _record_failure(station_slug: str) -> None:
    _failures.setdefault(_rate_key(station_slug), []).append(time.monotonic())


def _clear_failures(station_slug: str) -> None:
    _failures.pop(_rate_key(station_slug), None)


# --- redemption -----------------------------------------------------------


async def redeem(db: AsyncSession, *, station_slug: str, code: str) -> ChargingSession:
    """Validate `code` at `station_slug` and open a session.

    Order matters, and is taken from spec §4: cheap local checks first so a
    mistyped code is rejected instantly without a lookup and without costing an
    attempt; only a well-formed code that fails the allowlist is a real guess.
    """
    station = (
        await db.execute(select(Station).where(Station.slug == station_slug))
    ).scalar_one_or_none()
    if station is None:
        raise UnknownCode("Unknown station.")

    # 1–2. Shape and check digit. Free: these cannot distinguish a guess from
    # a typo, so they must not consume the attempt budget.
    try:
        normalised = codes.normalise(code)
    except codes.InvalidCode as exc:
        raise MalformedCode("That code doesn’t look right. Check and try again.") from exc

    # 3. Rate-limit gate.
    _check_rate_limit(station_slug)

    # 4. Allowlist lookup, by hash — the plaintext is never stored or compared.
    digest = access_codes.hash_code(normalised)
    record = (
        await db.execute(select(AccessCode).where(AccessCode.code_sha256 == digest))
    ).scalar_one_or_none()

    if record is None:
        _record_failure(station_slug)
        raise UnknownCode("That code isn’t recognised at this hub.")

    booking = (
        await db.execute(select(Booking).where(Booking.id == record.booking_id))
    ).scalar_one()

    outlet = (await db.execute(select(Outlet).where(Outlet.id == booking.outlet_id))).scalar_one()
    if outlet.station_id != station.id:
        # Codes are station-scoped (spec §7). A code for hub A must not open
        # anything at hub B, even though it is otherwise perfectly valid.
        _record_failure(station_slug)
        raise UnknownCode("That code isn’t recognised at this hub.")

    now = utcnow()

    if record.revoked_at is not None:
        record.attempts += 1
        _record_failure(station_slug)
        raise CodeAlreadyUsed("That code has been cancelled.")

    if record.single_use and record.redeemed_at is not None:
        record.attempts += 1
        _record_failure(station_slug)
        raise CodeAlreadyUsed("That code has already been used.")

    # 6. Validity window, enforced against the clock — not inferred from the
    # code's presence in a cache. The prototype got this wrong: its cached PIN
    # file was never invalidated, so losing the network made every cached code
    # valid forever.
    if now < record.not_before:
        record.attempts += 1
        _record_failure(station_slug)
        raise CodeNotYetValid("This booking hasn’t started yet.")

    if now >= record.not_after:
        record.attempts += 1
        _record_failure(station_slug)
        raise CodeExpired("This booking has expired.")

    # 7. Outlet health.
    if outlet.status in (OutletStatus.FAULT, OutletStatus.OFFLINE, OutletStatus.MAINTENANCE):
        raise OutletNotUsable("That outlet is out of service. Please see an attendant.")

    existing = (
        await db.execute(
            select(ChargingSession).where(ChargingSession.booking_id == booking.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise CodeAlreadyUsed("A session is already running for this booking.")

    # 8. Energise.
    session = ChargingSession(
        booking_id=booking.id,
        outlet_id=outlet.id,
        started_at=now,
        # The paid window runs from redemption, not from booking time — a
        # customer who arrives twenty minutes late still gets their full hour.
        ends_at=now + (booking.ends_at - booking.starts_at),
        status=SessionStatus.ACTIVE,
        cost_kobo=booking.amount_kobo,
    )
    db.add(session)

    record.redeemed_at = now
    booking.status = BookingStatus.ACTIVE
    outlet.status = OutletStatus.IN_USE

    _clear_failures(station_slug)
    await db.flush()
    return session


# --- lifecycle ------------------------------------------------------------


async def _plan_for(db: AsyncSession, session: ChargingSession) -> Plan:
    return (
        await db.execute(
            select(Plan).join(Booking, Booking.plan_id == Plan.id).where(
                Booking.id == session.booking_id
            )
        )
    ).scalar_one()


async def _simulate_progress(db: AsyncSession, session: ChargingSession) -> None:
    """Invent plausible energy for a running session.

    Development only, gated by SIMULATE_TELEMETRY. Real figures arrive by MQTT
    from the station; until that exists the Active Session screen would show a
    flat zero and nobody could tell a working countdown from a broken one.

    The model is deliberately crude: a constant draw at 60% of the plan's power
    cap, accrued at `simulate_speedup` times real time so the figure visibly
    moves, and clamped to the plan's energy cap so it can never report more
    than the customer bought. It is not a load model and should never be
    mistaken for one.
    """
    if not settings.simulate_telemetry or session.status is not SessionStatus.ACTIVE:
        return

    plan = await _plan_for(db, session)
    elapsed = (min(utcnow(), session.ends_at) - session.started_at).total_seconds()
    if elapsed <= 0:
        return

    draw_w = int(plan.power_cap_w * 0.6)
    accrued = int(draw_w * elapsed * max(1, settings.simulate_speedup) / 3600)

    # Monotonic: cumulative energy must never go down. Without the max() a
    # clock correction — or a real telemetry reading arriving alongside the
    # simulator — could walk the meter backwards mid-session, which would be
    # visible to the customer and wrong on the receipt.
    session.energy_wh = min(max(session.energy_wh, accrued), plan.energy_cap_wh)
    # Draw drops to nothing once the paid energy is used up.
    session.power_w = 0 if session.energy_wh >= plan.energy_cap_wh else draw_w


async def get_live(db: AsyncSession, session: ChargingSession) -> ChargingSession:
    """Refresh a session's derived state, ending it if it is done.

    A session ends on whichever comes first: the paid window closing, or the
    paid energy being used up. The second case is the `energy_exhausted` event
    in the contracts spec — the one the prototype could raise but never did,
    because the handler read a parameter the firmware never sent.
    """
    if session.status is not SessionStatus.ACTIVE:
        return session

    await _simulate_progress(db, session)

    if utcnow() >= session.ends_at:
        await end(db, session, SessionEndReason.TIME_EXPIRED)
        return session

    plan = await _plan_for(db, session)
    if session.energy_wh >= plan.energy_cap_wh:
        await end(db, session, SessionEndReason.ENERGY_CAP)
        return session

    await db.flush()
    return session


async def end(
    db: AsyncSession, session: ChargingSession, reason: SessionEndReason
) -> ChargingSession:
    """Stop a session and release its outlet. Idempotent."""
    if session.status is SessionStatus.COMPLETED:
        return session

    await _simulate_progress(db, session)

    now = utcnow()
    session.status = SessionStatus.COMPLETED
    session.ended_at = min(now, session.ends_at) if reason is SessionEndReason.TIME_EXPIRED else now
    session.end_reason = reason
    session.power_w = 0

    booking = (
        await db.execute(select(Booking).where(Booking.id == session.booking_id))
    ).scalar_one()
    booking.status = BookingStatus.COMPLETED

    outlet = (
        await db.execute(select(Outlet).where(Outlet.id == session.outlet_id))
    ).scalar_one()
    if outlet.status is OutletStatus.IN_USE:
        outlet.status = OutletStatus.AVAILABLE

    await db.flush()
    return session


async def for_booking(db: AsyncSession, booking_id: uuid.UUID) -> ChargingSession | None:
    return (
        await db.execute(
            select(ChargingSession).where(ChargingSession.booking_id == booking_id)
        )
    ).scalar_one_or_none()


async def get_for_user(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> ChargingSession | None:
    """Someone else's session reads as missing, not forbidden."""
    return (
        await db.execute(
            select(ChargingSession)
            .join(Booking, Booking.id == ChargingSession.booking_id)
            .where(ChargingSession.id == session_id)
            .where(Booking.user_id == user_id)
        )
    ).scalar_one_or_none()


async def active_for_user(db: AsyncSession, user_id: uuid.UUID) -> ChargingSession | None:
    return (
        await db.execute(
            select(ChargingSession)
            .join(Booking, Booking.id == ChargingSession.booking_id)
            .where(Booking.user_id == user_id)
            .where(ChargingSession.status == SessionStatus.ACTIVE)
            .order_by(ChargingSession.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def remaining_seconds(session: ChargingSession, at: datetime | None = None) -> int:
    now = at or utcnow()
    end_at = session.ended_at or session.ends_at
    if session.status is not SessionStatus.ACTIVE:
        return 0
    return max(0, int((end_at - now).total_seconds()))


def elapsed_seconds(session: ChargingSession, at: datetime | None = None) -> int:
    now = at or utcnow()
    end_at = session.ended_at or min(now, session.ends_at)
    return max(0, int((end_at - session.started_at).total_seconds()))


def duration(session: ChargingSession) -> timedelta:
    return session.ends_at - session.started_at
