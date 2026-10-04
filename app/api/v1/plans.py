from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import Plan
from app.schemas.plan import PlanOut

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=list[PlanOut])
async def list_plans(db: DbSession, active_only: bool = True) -> list[Plan]:
    """Tariff plans offered to customers, cheapest first.

    The `plans` table has existed since the initial schema but was unreachable
    over the API until now, which is why the booking UI had nothing to show.
    """
    stmt = select(Plan).order_by(Plan.price_kobo)
    if active_only:
        stmt = stmt.where(Plan.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())
