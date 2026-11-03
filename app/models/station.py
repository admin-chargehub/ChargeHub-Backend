"""Stations and outlets.

The legacy schema used one table per socket (`socket1`..`socket4`), which is why
the prototype physically could not scale past four outlets without a schema
change and a firmware change. Outlets are rows here, not tables: a station can
have 8, 12 or 50 of them with no structural change.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


class OutletStatus(StrEnum):
    AVAILABLE = "available"
    IN_USE = "in_use"
    RESERVED = "reserved"
    FAULT = "fault"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"


class Station(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "stations"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)

    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    outlets: Mapped[list["Outlet"]] = relationship(
        back_populates="station",
        cascade="all, delete-orphan",
        order_by="Outlet.index",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Station {self.slug}>"


class Outlet(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "outlets"
    __table_args__ = (
        UniqueConstraint("station_id", "index", name="uq_outlet_station_index"),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 1-based position on the physical station. Not capped at 9 — the legacy
    # single-digit PIN field is exactly what limited this before.
    index: Mapped[int] = mapped_column(Integer, nullable=False)

    label: Mapped[str | None] = mapped_column(String(60), nullable=True)

    status: Mapped[OutletStatus] = mapped_column(
        SAEnum(OutletStatus, name="outlet_status"),
        default=OutletStatus.AVAILABLE,
        nullable=False,
    )

    # Hard electrical ceiling for this socket, in watts. Real units, unlike the
    # dimensionless 0.06/0.08/0.20/0.32 accumulator thresholds in the prototype.
    max_power_w: Mapped[int] = mapped_column(Integer, default=500, nullable=False)

    #: Why this outlet is not available, set when an operator takes it out of
    #: service. Cleared on the way back to AVAILABLE so a stale reason cannot
    #: sit under a working socket.
    fault_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: When the status last changed by hand. Null for outlets that have only
    #: ever moved through the booking lifecycle.
    status_changed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    station: Mapped[Station] = relationship(back_populates="outlets")

    #: Statuses an operator may set directly. IN_USE and RESERVED are owned by
    #: the booking lifecycle — letting a human assign them by hand would put
    #: the outlet out of step with the booking that actually holds it.
    OPERATOR_SETTABLE = (
        OutletStatus.AVAILABLE,
        OutletStatus.FAULT,
        OutletStatus.OFFLINE,
        OutletStatus.MAINTENANCE,
    )

    def __repr__(self) -> str:
        return f"<Outlet {self.index} @ {self.station_id}>"
