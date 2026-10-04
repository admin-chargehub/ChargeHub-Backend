import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import Booking, Outlet, Plan, Station
from app.schemas.booking import (
    BookingCreate,
    BookingOut,
    BookingOutletOut,
    BookingStationOut,
)
from app.schemas.plan import PlanOut
from app.services import access_codes
from app.services import bookings as svc
from app.vendor import chargehub_codes as codes

router = APIRouter(prefix="/bookings", tags=["bookings"])


async def _to_out(db, booking: Booking, *, include_code: str | None = None) -> BookingOut:
    """Assemble the response.

    Every related row is fetched with an explicit query rather than read off a
    relationship. `selectin` loading only populates relationships for objects
    that came back from a SELECT — a booking we just inserted has none, so
    touching `booking.outlet` there triggers a lazy load mid-request and raises
    MissingGreenlet. Querying explicitly behaves the same either way.
    """
    outlet, station = (
        await db.execute(
            select(Outlet, Station)
            .join(Station, Station.id == Outlet.station_id)
            .where(Outlet.id == booking.outlet_id)
        )
    ).one()

    plan = (await db.execute(select(Plan).where(Plan.id == booking.plan_id))).scalar_one()

    code = include_code
    if code is None:
        record = await access_codes.for_booking(db, booking.id)
        if record is not None:
            code = access_codes.reveal(record)

    return BookingOut(
        id=booking.id,
        status=booking.status,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        amount_kobo=booking.amount_kobo,
        pending_expires_at=booking.pending_expires_at,
        created_at=booking.created_at,
        outlet=BookingOutletOut.model_validate(outlet),
        station=BookingStationOut.model_validate(station),
        plan=PlanOut.model_validate(plan),
        access_code=codes.format_for_display(code) if code else None,
    )


def _http(exc: svc.BookingError) -> HTTPException:
    if isinstance(exc, svc.SlotTaken):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, svc.NotPayable):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def create_booking(
    payload: BookingCreate,
    db: DbSession,
    user: CurrentUser,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BookingOut:
    """Hold an outlet. The booking is PENDING and expires if it isn't paid for."""
    try:
        booking = await svc.create_booking(
            db,
            user=user,
            outlet_id=payload.outlet_id,
            plan_id=payload.plan_id,
            idempotency_key=idempotency_key,
        )
    except svc.BookingError as exc:
        raise _http(exc) from exc

    return await _to_out(db, booking)


@router.get("", response_model=list[BookingOut])
async def list_bookings(db: DbSession, user: CurrentUser) -> list[BookingOut]:
    rows = await svc.list_for_user(db, user)
    return [await _to_out(db, b) for b in rows]


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    booking = await svc.get_for_user(db, user, booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return await _to_out(db, booking)


@router.post("/{booking_id}/pay", response_model=BookingOut)
async def pay_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    """Pay for a pending booking and receive the access code.

    The code is returned in full here — this is the one response that carries
    it. Afterwards it is only readable through the booking detail route, and
    only until it expires.
    """
    booking = await svc.get_for_user(db, user, booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

    try:
        booking, plaintext = await svc.pay_booking(db, booking, user)
    except svc.BookingError as exc:
        raise _http(exc) from exc

    return await _to_out(db, booking, include_code=plaintext)
