"""Operator console payloads.

Separate from the customer schemas on purpose. These expose things the
customer API must never leak — who is on an outlet, what the site earned, why
a socket is dead — and keeping them in their own module means that difference
is visible rather than a flag on a shared model.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import OutletStatus, PaymentMethod, PowerSource


class OverviewOut(BaseModel):
    stations_total: int
    stations_online: int
    outlets_total: int
    outlets_available: int
    outlets_in_use: int
    outlets_faulty: int
    sessions_active: int
    revenue_today_kobo: int
    energy_today_wh: int
    generator_minutes_today: int
    server_now: datetime


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: PowerSource
    battery_soc_pct: int | None
    solar_w: int
    load_w: int
    grid_available: bool
    generator_running: bool
    generator_minutes_today: int
    last_seen_at: datetime
    is_online: bool


class OpsOutletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    label: str | None
    status: OutletStatus
    max_power_w: int
    fault_reason: str | None
    status_changed_at: datetime | None


class OpsStationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    address: str | None
    is_active: bool
    outlets_total: int
    outlets_available: int
    outlets_in_use: int
    outlets_faulty: int
    telemetry: TelemetryOut | None


class OpsStationDetailOut(OpsStationOut):
    outlets: list[OpsOutletOut]


class OutletStatusIn(BaseModel):
    status: OutletStatus
    #: Required when taking an outlet out of service. An unexplained dead
    #: socket is the thing the next operator cannot act on.
    reason: str | None = Field(default=None, max_length=200)
    #: Set to end a live session on this outlet instead of being refused.
    force: bool = False


class OpsSessionOut(BaseModel):
    id: uuid.UUID
    station_id: uuid.UUID
    station_name: str
    outlet_label: str
    customer_name: str
    customer_email: str
    customer_phone: str | None
    status: str
    started_at: datetime
    ends_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    energy_wh: int
    power_w: int | None
    cost_kobo: int
    server_now: datetime


class UpcomingBookingOut(BaseModel):
    id: uuid.UUID
    station_name: str
    outlet_label: str
    customer_name: str
    starts_at: datetime
    ends_at: datetime
    amount_kobo: int


class AttentionOut(BaseModel):
    station_id: uuid.UUID
    station_name: str
    outlet_id: uuid.UUID
    outlet_label: str
    status: OutletStatus
    reason: str | None
    since: datetime | None


class DashboardOut(BaseModel):
    overview: OverviewOut
    stations: list[OpsStationOut]
    attention: list[AttentionOut]
    upcoming: list[UpcomingBookingOut]


class DayPointOut(BaseModel):
    day: date
    sessions: int
    energy_wh: int
    revenue_kobo: int


class StationTotalsOut(BaseModel):
    station_id: uuid.UUID
    name: str
    sessions: int
    energy_wh: int
    revenue_kobo: int


class ImpactOut(BaseModel):
    energy_kwh: float
    litres_displaced: float
    kg_co2_avoided: float
    naira_fuel_avoided: int
    #: Carried in the payload so the figures cannot be quoted without them.
    assumptions: str


class ReportOut(BaseModel):
    days: int
    sessions: int
    energy_wh: int
    revenue_kobo: int
    series: list[DayPointOut]
    by_station: list[StationTotalsOut]
    impact: ImpactOut
    timezone_note: str


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_email: str
    action: str
    target_type: str
    target_label: str | None
    summary: str
    at: datetime


class WalkInIn(BaseModel):
    """A booking taken at the counter for someone who is standing there."""

    outlet_id: uuid.UUID
    plan_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    #: How the same person is recognised on their second visit. Optional,
    #: because somebody may not want to give it and should still be served.
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    payment_method: PaymentMethod = PaymentMethod.CASH


class WalkInOut(BaseModel):
    booking_id: uuid.UUID
    #: Plaintext, returned once. The operator writes it on a slip.
    access_code: str
    customer_name: str
    customer_phone: str | None
    #: True when this created a new customer record, so the UI can say so
    #: rather than leaving the operator unsure whether they made a duplicate.
    new_customer: bool
    station_name: str
    outlet_label: str
    starts_at: datetime
    ends_at: datetime
    amount_kobo: int
    payment_method: str


class ExtendIn(BaseModel):
    minutes: int = Field(ge=1, le=24 * 60)
    reason: str | None = Field(default=None, max_length=200)


class RefundIn(BaseModel):
    kobo: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=200)


class StationActiveIn(BaseModel):
    is_active: bool
    reason: str | None = Field(default=None, max_length=200)


class LookupOut(BaseModel):
    booking_id: uuid.UUID
    station_name: str
    outlet_label: str
    customer_name: str
    customer_phone: str | None
    status: str
    #: "valid" / "already used" / "expired" / "not valid yet" — resolved the
    #: same way the keypad resolves it.
    code_state: str
    starts_at: datetime
    ends_at: datetime
    amount_kobo: int
    refund_kobo: int
    session_id: uuid.UUID | None
    session_status: str | None
