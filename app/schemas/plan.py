import uuid

from pydantic import BaseModel, ConfigDict


class PlanOut(BaseModel):
    """A tariff plan as the booking UI needs it.

    Money is in kobo throughout — integers, never floats. The client formats;
    the server never sends a pre-formatted "₦1,000".
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    power_cap_w: int
    energy_cap_wh: int
    max_duration_minutes: int
    price_kobo: int
    recommended_devices: str | None
