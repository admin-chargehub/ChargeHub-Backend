"""Operations: station power telemetry and the audit trail.

Neither of these appears in the design. They are here because an operator
console without them cannot answer the two questions an operator actually has.

**Power.** ChargeHub is a solar-first, multi-source station — solar, grid and
generator behind an auto-switching controller. The designed dashboard reports
outlets and revenue and says nothing about which source is carrying the load,
how full the battery is, or whether the generator is running. On a site in
Ibadan that is the difference between a profitable day and one that burned
diesel to charge phones. `StationTelemetry` is the shape the HGC will report
over MQTT; until that firmware exists it is written by the seed and the
simulator, and `last_seen_at` is what makes a stale row visibly stale rather
than quietly wrong.

**Audit.** Operators switch mains power to sockets and end sessions people have
paid for. Every one of those actions needs an actor and a timestamp, or the
first dispute has no answer.
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin, utcnow


class PowerSource(StrEnum):
    """Which supply is carrying the load right now.

    Mirrors the states the existing HPTS controller already switches between,
    plus BATTERY for running off storage with no live source.
    """

    SOLAR = "solar"
    BATTERY = "battery"
    GRID = "grid"
    GENERATOR = "generator"
    NONE = "none"


#: A station that has not reported within this window is treated as offline.
#: Generous on purpose: the pilot is on consumer LTE, and flapping a site to
#: "offline" on one missed heartbeat trains operators to ignore the signal.
OFFLINE_AFTER = timedelta(minutes=10)


class StationTelemetry(UUIDMixin, TimestampMixin, Base):
    """Latest known power state for one station.

    One row per station, overwritten in place. History belongs in a time-series
    store (the design doc names Timestream); this is the current-state cache
    that every operator screen reads, and keeping it to one row keeps the
    dashboard a single cheap query rather than a scan.
    """

    __tablename__ = "station_telemetry"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    source: Mapped[PowerSource] = mapped_column(
        SAEnum(PowerSource, name="power_source"),
        default=PowerSource.GRID,
        nullable=False,
    )

    #: Battery state of charge, 0-100. Null when the site has no storage.
    battery_soc_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Instantaneous PV yield, watts.
    solar_w: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Total draw across all outlets plus station overhead, watts.
    load_w: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    grid_available: Mapped[bool] = mapped_column(default=True, nullable=False)
    generator_running: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Cumulative generator runtime, minutes. The number that turns into a
    #: fuel bill, so it is tracked rather than inferred from source history.
    generator_minutes_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Last contact from the station. Drives online/offline everywhere.
    last_seen_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    station: Mapped["object"] = relationship("Station", lazy="raise")

    @property
    def is_online(self) -> bool:
        return utcnow() - self.last_seen_at < OFFLINE_AFTER

    def __repr__(self) -> str:
        return f"<StationTelemetry {self.station_id} {self.source.value}>"


class AuditAction(StrEnum):
    OUTLET_STATUS_CHANGED = "outlet_status_changed"
    SESSION_ENDED_BY_OPERATOR = "session_ended_by_operator"
    SESSION_EXTENDED = "session_extended"
    STATION_ACTIVATED = "station_activated"
    STATION_DEACTIVATED = "station_deactivated"


class AuditEvent(UUIDMixin, Base):
    """One operator action, append-only.

    Deliberately not a TimestampMixin: an audit row has exactly one time, the
    moment it happened, and an `updated_at` on an append-only table invites
    somebody to update it.
    """

    __tablename__ = "audit_events"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: Denormalised so the trail survives the account being deleted. An audit
    #: row that reads "deleted user" answers nothing.
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)

    action: Mapped[AuditAction] = mapped_column(
        SAEnum(AuditAction, name="audit_action"), index=True, nullable=False
    )

    #: What was acted on — "outlet", "session", "station" — and its id.
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: Human-readable identifier captured at the time ("UI Hub · A3").
    target_label: Mapped[str | None] = mapped_column(String(160), nullable=True)

    #: Rendered sentence, written once and never recomputed. Re-deriving an
    #: audit line from current data defeats the point of keeping one.
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<AuditEvent {self.action.value} {self.at.isoformat()}>"
