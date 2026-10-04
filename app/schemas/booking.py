import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.booking import BookingStatus
from app.schemas.plan import PlanOut


class BookingCreate(BaseModel):
    outlet_id: uuid.UUID
    plan_id: uuid.UUID
    # `starts_at` is intentionally absent. The window is derived server-side
    # from the plan's duration; accepting it from the client would let someone
    # buy the one-hour plan and book twenty-four hours.


class BookingOutletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    label: str | None


class BookingStationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    address: str | None


class BookingOut(BaseModel):
    id: uuid.UUID
    status: BookingStatus
    starts_at: datetime
    ends_at: datetime
    amount_kobo: int
    pending_expires_at: datetime | None
    created_at: datetime

    outlet: BookingOutletOut
    station: BookingStationOut
    plan: PlanOut

    access_code: str | None = Field(
        default=None,
        description=(
            "The 10-digit code, formatted for display. Null until the booking is "
            "paid for, and null again once it expires."
        ),
    )
