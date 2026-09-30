"""Fare resolution for trip legs — pluggable and carrier-agnostic.

A *fare provider* answers "what does it cost to ride this leg?" for a given
carrier (TGV, OUIGO, Intercités, TER, and — later — regional networks like
Zou).  Providers are tried in order and the first hit wins:

  1. :class:`ExactTariffProvider` — real published OD fares for TGV INOUI /
     OUIGO and Intercités (non-dynamic), keyed by UIC8, loaded from
     ``fares.json`` (built by ``script/build_graph.py``).
  2. :class:`TerKilometricProvider` — the published kilometric TER tariff
     (P = a + b*d), with rail distance estimated from stop coordinates.
  3. :class:`PerKmProvider` — a per-kilometre estimate by carrier, the
     fallback when no exact tariff is known (notably TER, whose fares aren't
     in national open data, and regional operators).

To add a regional network later, register a provider in :data:`REGISTRY`
(e.g. one backed by a Zou fare table) ahead of the per-km fallback — nothing
else has to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Protocol
import json

from network import stations as stn

_HERE = Path(__file__).resolve().parent

# Per-kilometre 2nd-class fare bands (euros/km), rough and clearly estimates.
# TER/regional are cheaper per km than TGV — the whole point of routing onto
# them.  (min, max) so the UI can show a range.
PER_KM_EUR: Dict[str, tuple] = {
    "TER": (0.11, 0.19),
    "INTERCITES": (0.09, 0.17),
    "OUIGO": (0.06, 0.14),
    "TGV": (0.08, 0.20),     # regressive per-km; long trips are cheaper/km
    "BUS": (0.04, 0.09),     # regional coaches (e.g. Zou) — future
    "DEFAULT": (0.10, 0.20),
}
# Flat floor added to every estimated fare (booking/handling), euros.
BASE_FARE_EUR = 1.0


@dataclass
class Fare:
    min_cents: Optional[int]
    max_cents: Optional[int]
    exact: bool          # True = published tariff, False = per-km estimate
    basis: str           # provider that produced it

    @property
    def display(self) -> str:
        if self.min_cents is None:
            return "price unknown"
        lo = self.min_cents / 100
        hi = (self.max_cents or self.min_cents) / 100
        prefix = "" if self.exact else "~"
        if abs(hi - lo) < 0.5:
            return f"{prefix}{lo:.0f}EUR"
        return f"{prefix}{lo:.0f}-{hi:.0f}EUR"


class FareProvider(Protocol):
    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        ...


class ExactTariffProvider:
    """Published, non-dynamic OD fares for TGV INOUI/OUIGO and Intercités."""

    @staticmethod
    @lru_cache(maxsize=1)
    def _table() -> Dict[str, list]:
        path = _HERE / "fares.json"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _uic(name: str) -> Optional[str]:
        uo = stn.uic(name)
        if uo:
            return uo
        # stations.json carries no UIC map; the TER cache joins 266 MAX
        # stations to UIC via GTFS names + coordinates.
        try:
            from network import ter as _ter
            return _ter._name_to_uic().get(name)
        except Exception:
            return None

    @staticmethod
    def _candidate_uics(name: str) -> List[str]:
        """UICs usable for a station: its own + same-metro stops.

        Tariff tables key on specific stations (Paris Gare de Lyon =
        87686006) while queries use aggregates (PARIS (intramuros)).
        Same-metro stops (TER-cache stops within 8 km of the query
        coordinates) close that gap.
        """
        import math
        cands = []
        u = ExactTariffProvider._uic(name)
        if u:
            cands.append(u)
        try:
            from network import ter as _ter
            c = stn.coords(name)
            if c:
                (lat1, lon1) = c
                for su, meta in _ter._cache().get("stops", {}).items():
                    if meta[1] is None or su in cands:
                        continue
                    p1, p2 = math.radians(lat1), math.radians(meta[1])
                    dp = math.radians(meta[1] - lat1)
                    dl = math.radians(meta[2] - lon1)
                    h = (math.sin(dp / 2) ** 2
                         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
                    if 2 * 6371.0 * math.asin(math.sqrt(h)) <= 8.0:
                        cands.append(su)
        except Exception:
            pass
        return cands

    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        # The tables only cover long-distance carriers; TER and coaches
        # have their own providers (kilometric grid / per-km band).
        want = carrier.upper()
        if want not in ("TGV", "OUIGO", "INTERCITES"):
            return None
        table = self._table()
        if not table:
            return None
        rows = []
        for uo in self._candidate_uics(origin):
            for ud in self._candidate_uics(destination):
                for key in (f"{uo}|{ud}", f"{ud}|{uo}"):
                    cell = table.get(key)
                    if cell and want in cell:
                        rows.append(cell[want])
        if not rows:
            return None
        lo = min(r[0] for r in rows)
        hi = max(r[1] for r in rows)
        return Fare(min_cents=lo, max_cents=hi, exact=True,
                    basis="SNCF open tariff grid")


class TerKilometricProvider:
    """Published kilometric TER tariff: P = a + b*d (2nd class).

    This is the actual counter-price mechanism, printed in every
    regional CGV (P = constante + prix-kilometrique x distance
    tarifaire, rounded UP to the 10 cents, EUR1.20 minimum; 1st class
    is x1.5). Parameters below average the Hauts-de-France (03/2023)
    and Sud/PACA (01/2025) grids as a national reference - individual
    regions differ by roughly +/-15%.

    Distance is rail-estimated: great-circle between the stops x 1.25
    circuity (real rail distance is not in open data). exact=True: the
    tariff mechanism is the genuine published one, not a per-km guess -
    only the distance (and the regional parameter choice) is estimated.
    """

    CARRIERS = ("TER",)
    #: (lo_km, hi_km, a_eur, b_eur_per_km), 2nd class.
    GRID = [
        (1, 16, 1.5765, 0.20655),
        (17, 32, 1.0155, 0.23000),
        (33, 64, 2.9495, 0.16970),
        (65, 109, 3.8190, 0.15815),
        (110, 149, 5.0905, 0.15140),
        (150, 199, 9.3410, 0.12675),
        (200, 300, 8.9910, 0.12845),
        (301, 499, 15.2515, 0.10940),
        (500, 799, 20.3440, 0.09785),
        (800, 9999, 37.7180, 0.08490),
    ]
    CIRCUITY = 1.25      # rail km per crow-flies km (national average)
    MIN_CENTS = 120      # minimum de perception, 2nd class

    @staticmethod
    def _crow_km(origin: str, destination: str) -> Optional[float]:
        km = _coords_km(origin, destination)
        return km

    @classmethod
    def price_cents(cls, rail_km: float) -> int:
        import math
        d = max(1, int(round(rail_km)))
        a, b = cls.GRID[-1][2], cls.GRID[-1][3]
        for lo, hi, ga, gb in cls.GRID:
            if lo <= d <= hi:
                a, b = ga, gb
                break
        return max(cls.MIN_CENTS, int(math.ceil((a + b * d) * 10) * 10))

    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        if carrier.upper() != "TER":
            return None
        km = self._crow_km(origin, destination)
        if km is None:
            return None
        cents = self.price_cents(km * self.CIRCUITY)
        return Fare(min_cents=cents, max_cents=cents, exact=True,
                    basis="ter kilometric grid")


class PerKmProvider:
    """Distance-based estimate, the universal fallback."""

    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        km = _coords_km(origin, destination)
        if km is None:
            return None
        lo_rate, hi_rate = PER_KM_EUR.get(carrier.upper(), PER_KM_EUR["DEFAULT"])
        lo = BASE_FARE_EUR + km * lo_rate
        hi = BASE_FARE_EUR + km * hi_rate
        return Fare(min_cents=int(lo * 100), max_cents=int(hi * 100),
                    exact=False, basis="per-km")


# Provider chain — first hit wins.  Insert regional providers before PerKm.
REGISTRY: List[FareProvider] = [
    ExactTariffProvider(),
    TerKilometricProvider(),
    PerKmProvider(),
]


def _coords_any(name: str):
    """Coordinates for a MAX station, TER halt or coach stop name."""
    c = stn.coords(name)
    if c:
        return c
    # GTFS-labelled TER halts are not in the MAX station list.
    try:
        from network import ter as _ter
        for _uic, meta in _ter._cache().get("stops", {}).items():
            if meta[0] == name and meta[1] is not None:
                return (meta[1], meta[2])
    except Exception:
        pass
    # Coach stops live in the bus cache under their own names.
    try:
        from network import bus as _bus
        c = _bus.stop_coords(name)
        if c:
            return c
    except Exception:
        pass
    return None


def _coords_km(origin: str, destination: str) -> Optional[float]:
    """Great-circle distance (km) across all three stop namespaces."""
    import math
    ca, cb = _coords_any(origin), _coords_any(destination)
    if not ca or not cb:
        return None
    (lat1, lon1), (lat2, lon2) = ca, cb
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimate_fare(origin: str, destination: str, carrier: str = "TGV") -> Fare:
    """Best available fare for a leg: exact tariff if known, else per-km."""
    for provider in REGISTRY:
        f = provider.fare(origin, destination, carrier)
        if f is not None:
            return f
    return Fare(min_cents=None, max_cents=None, exact=False, basis="unknown")
