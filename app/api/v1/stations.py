import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import Outlet, OutletStatus, Station
from app.models.base import utcnow
from app.schemas.station import OutletOut, StationDetailOut, StationOut
from app.services.availability import outlet_status_snapshot

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("", response_model=list[StationOut])
async def list_stations(db: DbSession, active_only: bool = True) -> list[Station]:
    stmt = select(Station).order_by(Station.name)
    if active_only:
        stmt = stmt.where(Station.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


@router.get("/{station_id}", response_model=StationDetailOut)
async def get_station(station_id: uuid.UUID, db: DbSession) -> StationDetailOut:
    station = (
        await db.execute(select(Station).where(Station.id == station_id))
    ).scalar_one_or_none()
    if station is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")

    occupied = await outlet_status_snapshot(db, station_id, utcnow())

    outlets = list(station.outlets)
    # "Free now" is stricter than "bookable". BOOKABLE_STATUSES includes IN_USE
    # and RESERVED, because an occupied outlet can still be booked for a later
    # window — but it is plainly not free this minute, and reporting it as such
    # made a station with two outlets in use read "8 of 8 available".
    free_now = sum(
        1
        for o in outlets
        if o.status is OutletStatus.AVAILABLE and o.id not in occupied
    )

    return StationDetailOut(
        **StationOut.model_validate(station).model_dump(),
        outlets=[OutletOut.model_validate(o) for o in outlets],
        outlets_total=len(outlets),
        outlets_free_now=free_now,
    )


@router.get("/{station_id}/outlets", response_model=list[OutletOut])
async def list_outlets(station_id: uuid.UUID, db: DbSession) -> list[Outlet]:
    return list(
        (
            await db.execute(
                select(Outlet).where(Outlet.station_id == station_id).order_by(Outlet.index)
            )
        )
        .scalars()
        .all()
    )
