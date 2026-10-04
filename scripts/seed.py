"""Seed demo data: University of Ibadan hubs, tariff plans, and a demo customer.

    python -m scripts.seed

Idempotent — safe to re-run. Existing stations are updated in place rather than
duplicated, so changing a name or address here and re-running is enough.
"""

import asyncio
from typing import NamedTuple

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Outlet, OutletStatus, Plan, Station, User, UserRole

# Duration-tiered plans, matching the Figma customer flow: Quick ₦100/hr,
# Standard ₦1000/2hr, Day ₦2000/24hr. Prices are in kobo (₦1 = 100 kobo).
#
# ⚠ TODO(P1): power_cap_w and energy_cap_wh are placeholders. The design shows
# neither, but they are what the firmware actually enforces per session, so they
# must be derived from the load study before the pilot. Do not ship these values.
#
# ⚠ TODO(P4): the price curve looks wrong — Quick is ₦100/hr, Standard works out
# at ₦500/hr, Day at ₦83/hr. Standard being the most expensive per hour is
# probably not intended. Confirm the tariff before taking real money.
#
#        name       slug       W    Wh    mins  kobo    recommended devices
PLANS = [
    ("Quick",    "quick",     80,    80,    60,  10000, "Phone top-up"),
    ("Standard", "standard", 200,   400,   120, 100000, "Phone, tablet, laptop"),
    ("Day",      "day",      200,  4800,  1440, 200000, "Laptop + peripherals"),
]

# Hubs around the University of Ibadan campus. UI Hub is the real pilot site;
# the rest exist so the hub list, distance sorting and the various outlet states
# have something to show. Coordinates are approximate campus locations — close
# enough for distance ordering, not survey data.
#
# `busy` and `faulty` set a couple of outlets to non-available states so the
# demo shows the full range rather than a wall of green.
#
class Hub(NamedTuple):
    slug: str
    name: str
    address: str
    lat: float
    lng: float
    outlets: int
    busy: int
    faulty: int
    active: bool = True


STATIONS = [
    Hub("ui-pilot", "UI Hub", "Faculty of Technology, Appleton Road",
        7.4433, 3.8964, outlets=8, busy=2, faulty=0),
    Hub("zik-hub", "Zik Hub", "Nnamdi Azikiwe Hall, University of Ibadan",
        7.4469, 3.8991, outlets=6, busy=4, faulty=1),
    Hub("tedder-hub", "Tedder Hub", "Tedder Hall, University of Ibadan",
        7.4412, 3.9015, outlets=6, busy=1, faulty=0),
    Hub("mellanby", "Mellanby Hub", "Mellanby Hall, University of Ibadan",
        7.4455, 3.8932, outlets=4, busy=0, faulty=0),
    Hub("kuti-hub", "Kuti Hub", "Kuti Hall, University of Ibadan",
        7.4398, 3.8948, outlets=4, busy=1, faulty=0),
]

DEMO_EMAIL = "demo@chargehub.ng"
DEMO_PASSWORD = "chargehub123"


async def main() -> None:
    async with SessionLocal() as db:
        for hub in STATIONS:
            station = (
                await db.execute(select(Station).where(Station.slug == hub.slug))
            ).scalar_one_or_none()

            if station is None:
                station = Station(slug=hub.slug)
                db.add(station)

            # Update in place so re-running picks up edits above.
            station.name = hub.name
            station.address = hub.address
            station.latitude = hub.lat
            station.longitude = hub.lng
            station.is_active = hub.active
            await db.flush()

            existing = {
                o.index: o
                for o in (
                    await db.execute(select(Outlet).where(Outlet.station_id == station.id))
                )
                .scalars()
                .all()
            }

            for i in range(1, hub.outlets + 1):
                # Bank-and-position labels as the design shows them (A1…An).
                # `index` stays the integer the firmware addresses; `label` is
                # presentation only.
                if i in existing:
                    outlet = existing[i]
                else:
                    outlet = Outlet(station_id=station.id, index=i)
                    db.add(outlet)
                outlet.label = f"A{i}"
                outlet.max_power_w = 500
                if i <= hub.busy:
                    outlet.status = OutletStatus.IN_USE
                elif i <= hub.busy + hub.faulty:
                    outlet.status = OutletStatus.FAULT
                else:
                    outlet.status = OutletStatus.AVAILABLE

            free = hub.outlets - hub.busy - hub.faulty
            print(
                f"  {hub.name:<13} {hub.outlets} outlets "
                f"({free} free, {hub.busy} in use, {hub.faulty} fault)"
            )

        for plan_name, slug, power_w, energy_wh, minutes, kobo, devices in PLANS:
            plan = (await db.execute(select(Plan).where(Plan.slug == slug))).scalar_one_or_none()
            if plan is None:
                plan = Plan(slug=slug)
                db.add(plan)
            plan.name = plan_name
            plan.power_cap_w = power_w
            plan.energy_cap_wh = energy_wh
            plan.max_duration_minutes = minutes
            plan.price_kobo = kobo
            plan.recommended_devices = devices
            plan.is_active = True
        print(f"  plans: {', '.join(p[0] for p in PLANS)}")

        demo = (
            await db.execute(select(User).where(User.email == DEMO_EMAIL))
        ).scalar_one_or_none()
        if demo is None:
            db.add(
                User(
                    email=DEMO_EMAIL,
                    full_name="Demo Customer",
                    hashed_password=hash_password(DEMO_PASSWORD),
                    role=UserRole.CUSTOMER,
                )
            )
            print(f"  demo user: {DEMO_EMAIL} / {DEMO_PASSWORD}")

        await db.commit()
        print("seed complete")


if __name__ == "__main__":
    asyncio.run(main())
