import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.session import SessionEndReason, SessionStatus
from app.schemas.booking import BookingOutletOut, BookingStationOut


class RedeemRequest(BaseModel):
    """What the station keypad sends. Eventually this comes over MQTT from the
    HGC; for now it is an HTTP call so the flow can be exercised without
    hardware."""

    station_slug: str = Field(max_length=64)
    code: str = Field(max_length=32, description="Accepts the XXXXX-XXXXX display form.")


class SessionOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    status: SessionStatus
    end_reason: SessionEndReason | None

    started_at: datetime
    ends_at: datetime
    ended_at: datetime | None

    server_now: datetime = Field(
        description=(
            "The server's clock at the moment of this response. Clients should "
            "offset their countdown by (server_now - client_now) so a wrong "
            "device clock doesn't show the wrong time remaining."
        )
    )
    remaining_seconds: int
    elapsed_seconds: int

    energy_wh: int = Field(
        description="Cumulative energy this session, in watt-hours — not kilowatt-hours."
    )
    power_w: int | None
    cost_kobo: int
    simulated: bool = Field(
        description=(
            "True when the energy figures are fabricated because no station "
            "telemetry exists yet. Never true in production."
        )
    )

    outlet: BookingOutletOut
    station: BookingStationOut
    plan_name: str
    customer_name: str
