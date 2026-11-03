import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import Station
from app.models.base import utcnow
from app.schemas.station import AvailabilityQuery, AvailabilityResponse
from app.services.availability import find_earliest_slot

router = APIRouter(prefix="/stations", tags=["availability"])


@router.post("/{station_id}/availability", response_model=AvailabilityResponse)
async def check_availability(
    station_id: uuid.UUID, payload: AvailabilityQuery, db: DbSession
) -> AvailabilityResponse:
    """Earliest outlet at this station that can take `duration_minutes`.

    This is the production replacement for the prototype's "find earliest free
    socket" search — see `app/services/availability.py` for what changed.
    """
    exists = (
        await db.execute(select(Station.id).where(Station.id == station_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")

    not_before = payload.not_before or utcnow()
    if not_before.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="not_before must include a timezone offset",
        )

    slot = await find_earliest_slot(
        db,
        station_id=station_id,
        duration_minutes=payload.duration_minutes,
        not_before=not_before,
    )

    if slot is None:
        return AvailabilityResponse(station_id=station_id, available=False)

    return AvailabilityResponse(
        station_id=station_id,
        available=True,
        outlet_id=slot.outlet_id,
        outlet_index=slot.outlet_index,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        wait_minutes=int((slot.starts_at - not_before).total_seconds() // 60),
    )
