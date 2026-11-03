"""Generator-displacement estimates.

Limitless Power Solution Concepts sells a solar-first station, so "how much
generator running did this displace" is a number the business is asked for in
every pitch, grant application and impact report. It belongs in one place with
its assumptions written down, not inlined in a reporting endpoint where nobody
can audit it.

**This is an estimate and the API labels it as one.** Two things keep it
honest:

1. It is counterfactual. It answers "if this energy had come from a small
   petrol generator instead, what would that have cost and emitted" — not
   "this much diesel was definitely saved". Until per-session source
   attribution exists (which needs the HGC reporting which supply carried each
   session), no stronger claim is available, and the endpoint must not imply
   one.
2. The constants below are published averages, not site measurements. When the
   pilot has real fuel logs, replace them with measured values rather than
   leaving plausible-looking defaults in a report that goes to a funder.
"""

from dataclasses import dataclass

#: Energy a small petrol generator yields per litre of fuel, kWh/L. Typical
#: 2-3 kVA sets used for this duty in Nigeria run 3-4 kWh/L at reasonable load
#: and worse when lightly loaded; 3.5 is a mid estimate that does not flatter
#: the result.
KWH_PER_LITRE = 3.5

#: Combustion CO2 from petrol, kg per litre. Tank-to-wheel only — it excludes
#: the emissions of refining and delivering the fuel, so the real avoided total
#: is somewhat higher than this reports. Understating is the right direction
#: for a number that will be quoted publicly.
KG_CO2_PER_LITRE = 2.31

#: Pump price, naira per litre, as a planning figure. Nigerian petrol prices
#: move sharply, so this is configuration rather than a constant of nature and
#: should be revisited per reporting period.
NAIRA_PER_LITRE = 1_000


@dataclass(frozen=True)
class Impact:
    energy_kwh: float
    litres_displaced: float
    kg_co2_avoided: float
    naira_fuel_avoided: int


def estimate(energy_wh: int) -> Impact:
    """Counterfactual generator cost of delivering `energy_wh`.

    Returns zeroes for zero energy rather than a tiny non-zero figure, so an
    empty reporting period reads as empty.
    """
    kwh = max(0, energy_wh) / 1000
    litres = kwh / KWH_PER_LITRE
    return Impact(
        energy_kwh=round(kwh, 3),
        litres_displaced=round(litres, 3),
        kg_co2_avoided=round(litres * KG_CO2_PER_LITRE, 3),
        naira_fuel_avoided=round(litres * NAIRA_PER_LITRE),
    )


#: Shown next to the figures so a reader can check the arithmetic without
#: reading this module.
ASSUMPTIONS = (
    f"Counterfactual estimate: assumes {KWH_PER_LITRE} kWh per litre of petrol, "
    f"{KG_CO2_PER_LITRE} kg CO₂ per litre (combustion only), "
    f"₦{NAIRA_PER_LITRE:,} per litre. Not a measurement."
)
