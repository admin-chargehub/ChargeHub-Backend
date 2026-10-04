import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"


class SessionEndReason(StrEnum):
    TIME_EXPIRED = "time_expired"
    USER_ENDED = "user_ended"
    ENERGY_CAP = "energy_cap"
    POWER_CAP = "power_cap"
    IDLE_TIMEOUT = "idle_timeout"
    FAULT = "fault"
    OPERATOR_ENDED = "operator_ended"


class ChargingSession(UUIDMixin, TimestampMixin, Base):
    """A live or finished charging session at one outlet.

    Created when a customer redeems their access code at the station — which is
    the only thing that sets `BookingStatus.ACTIVE`. Until the firmware exists
    the redemption comes from the API rather than a keypad, but the state
    transition and the data are the real ones.

    `energy_wh` is watt-hours, an integer, and so is `cost_kobo`. The design
    mockup shows "125kWh" for a two-hour session on a 500 W outlet, which is
    about a thousand times too much — a 500 W outlet can deliver at most 1 kWh
    in two hours. Storing watt-hours makes that kind of slip visible rather
    than plausible.
    """

    __tablename__ = "charging_sessions"

    #: One session per booking. A second redemption of the same code must not
    #: open a second session.
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    outlet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlets.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: When the session is scheduled to end — the paid-for window.
    ends_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: When it actually ended. Null while running.
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    status: Mapped[SessionStatus] = mapped_column(
        SAEnum(SessionStatus, name="session_status"),
        default=SessionStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    end_reason: Mapped[SessionEndReason | None] = mapped_column(
        SAEnum(SessionEndReason, name="session_end_reason"), nullable=True
    )

    #: Cumulative energy delivered this session, in watt-hours.
    energy_wh: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Most recent instantaneous power reading, in watts. Null before telemetry.
    power_w: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: What the customer paid, copied from the booking. Flat prepaid: the
    #: receipt reports this regardless of energy used. If P4 decides on metered
    #: billing with refunds, this is where that would diverge from the booking.
    cost_kobo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Free-text note for faults, mostly for the technician flow later.
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def __repr__(self) -> str:
        return f"<ChargingSession {self.id} {self.status.value}>"
