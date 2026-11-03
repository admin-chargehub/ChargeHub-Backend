"""Stand-in for the HGC's MQTT heartbeat.

The real Hub Gateway Controller will publish station power state to AWS IoT
Core and the backend will write it to `station_telemetry`. That firmware does
not exist, so without this every hub goes offline ten minutes after seeding and
the operator console shows a board of dead sites — which is correct behaviour
reported truthfully, and useless to develop or demo against.

This is a script, never an app import. It writes exactly what the gateway will
write, so the console is already reading the real shape.

    python -m scripts.simulate_telemetry          # one tick and exit
    python -m scripts.simulate_telemetry --loop   # tick every 30s

One hub is deliberately left stale so the offline path stays visible: a
simulator that makes everything green hides the failure mode it exists to
exercise.
"""

import argparse
import asyncio
import math
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import PowerSource, Station, StationTelemetry, utcnow

#: Left offline on purpose. Matches a hub whose LTE modem has dropped.
ALWAYS_STALE = {"kuti-hub"}

TICK_SECONDS = 30


def solar_curve(now: datetime, peak_w: int) -> int:
    """Rough bell over daylight hours, zero at night.

    WAT (UTC+1), so solar noon is about 11:00 UTC. Not an irradiance model —
    just enough shape that the dashboard moves through a day instead of
    holding one number, and that a night-time demo correctly shows zero PV
    with the site on battery.
    """
    hour = now.hour + now.minute / 60 + 1  # UTC -> WAT
    if hour < 6.5 or hour > 18.5:
        return 0
    fraction = math.sin(math.pi * (hour - 6.5) / 12)
    return max(0, int(peak_w * fraction * random.uniform(0.85, 1.0)))


def choose_source(solar_w: int, load_w: int, soc: int, grid: bool) -> PowerSource:
    """The switching logic the HPTS controller implements in hardware.

    Order matters and mirrors the real priority: free energy first, then
    stored, then the grid, and diesel only when nothing else can carry it.
    """
    if solar_w >= load_w:
        return PowerSource.SOLAR
    if soc > 35:
        return PowerSource.BATTERY
    if grid:
        return PowerSource.GRID
    return PowerSource.GENERATOR


async def tick() -> int:
    now = utcnow()
    updated = 0

    async with SessionLocal() as db:
        stations = (await db.execute(select(Station))).scalars().all()

        for station in stations:
            row = (
                await db.execute(
                    select(StationTelemetry).where(
                        StationTelemetry.station_id == station.id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = StationTelemetry(station_id=station.id)
                db.add(row)

            if station.slug in ALWAYS_STALE:
                row.last_seen_at = now - timedelta(minutes=85)
                continue

            peak = 600 + (hash(station.slug) % 5) * 450
            row.solar_w = solar_curve(now, peak)
            row.load_w = max(0, int(random.gauss(420, 180)))
            row.grid_available = random.random() > 0.2

            soc = row.battery_soc_pct if row.battery_soc_pct is not None else 70
            # Charge on surplus, discharge on deficit, clamped to a real range.
            drift = 2 if row.solar_w > row.load_w else -2
            row.battery_soc_pct = max(8, min(100, soc + drift + random.randint(-1, 1)))

            row.source = choose_source(
                row.solar_w, row.load_w, row.battery_soc_pct, row.grid_available
            )
            row.generator_running = row.source is PowerSource.GENERATOR
            if row.generator_running:
                row.generator_minutes_today += max(1, TICK_SECONDS // 60)

            row.last_seen_at = now
            updated += 1

        await db.commit()
    return updated


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep ticking")
    args = parser.parse_args()

    while True:
        count = await tick()
        print(f"telemetry: {count} stations reporting at {utcnow():%H:%M:%S}")
        if not args.loop:
            return
        await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
