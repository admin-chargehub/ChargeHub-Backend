import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.models import Booking, ChargingSession, Outlet, Plan, SessionEndReason, Station, User
from app.models.base import utcnow
from app.schemas.booking import BookingOutletOut, BookingStationOut
from app.schemas.session import RedeemRequest, SessionOut
from app.services import sessions as svc

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _to_out(db, session: ChargingSession) -> SessionOut:
    outlet, station = (
        await db.execute(
            select(Outlet, Station)
            .join(Station, Station.id == Outlet.station_id)
            .where(Outlet.id == session.outlet_id)
        )
    ).one()

    plan, customer = (
        await db.execute(
            select(Plan, User)
            .join(Booking, Booking.plan_id == Plan.id)
            .join(User, User.id == Booking.user_id)
            .where(Booking.id == session.booking_id)
        )
    ).one()

    return SessionOut(
        id=session.id,
        booking_id=session.booking_id,
        status=session.status,
        end_reason=session.end_reason,
        started_at=session.started_at,
        ends_at=session.ends_at,
        ended_at=session.ended_at,
        server_now=utcnow(),
        remaining_seconds=svc.remaining_seconds(session),
        elapsed_seconds=svc.elapsed_seconds(session),
        energy_wh=session.energy_wh,
        power_w=session.power_w,
        cost_kobo=session.cost_kobo,
        simulated=settings.simulate_telemetry,
        outlet=BookingOutletOut.model_validate(outlet),
        station=BookingStationOut.model_validate(station),
        plan_name=plan.name,
        customer_name=customer.full_name,
    )


_REDEMPTION_STATUS = {
    svc.MalformedCode: status.HTTP_422_UNPROCESSABLE_ENTITY,
    svc.RateLimited: status.HTTP_429_TOO_MANY_REQUESTS,
    svc.OutletNotUsable: status.HTTP_409_CONFLICT,
}


@router.post("/redeem", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def redeem(payload: RedeemRequest, db: DbSession) -> SessionOut:
    """Open a session by presenting an access code — the station's entry point.

    Deliberately unauthenticated: the code *is* the credential, exactly as it
    is on a keypad. When the HGC exists this moves behind mutual-TLS device
    identity and the station, not the customer's phone, will be the caller.
    """
    try:
        session = await svc.redeem(db, station_slug=payload.station_slug, code=payload.code)
    except svc.RedemptionError as exc:
        raise HTTPException(
            status_code=_REDEMPTION_STATUS.get(type(exc), status.HTTP_404_NOT_FOUND),
            detail=str(exc),
        ) from exc

    return await _to_out(db, session)


@router.get("/active", response_model=SessionOut | None)
async def active_session(db: DbSession, user: CurrentUser) -> SessionOut | None:
    """The caller's running session, if any — drives the Home screen banner."""
    session = await svc.active_for_user(db, user.id)
    if session is None:
        return None
    await svc.get_live(db, session)
    return await _to_out(db, session)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, db: DbSession, user: CurrentUser) -> SessionOut:
    session = await svc.get_for_user(db, user.id, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await svc.get_live(db, session)
    return await _to_out(db, session)


@router.post("/{session_id}/end", response_model=SessionOut)
async def end_session(session_id: uuid.UUID, db: DbSession, user: CurrentUser) -> SessionOut:
    """Stop early. The customer keeps paying the flat plan price — see the
    receipt note on the billing model."""
    session = await svc.get_for_user(db, user.id, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    await svc.end(db, session, SessionEndReason.USER_ENDED)
    return await _to_out(db, session)
