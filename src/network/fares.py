"""Fare resolution for trip legs — pluggable and carrier-agnostic.

A *fare provider* answers "what does it cost to ride this leg?" for a given
carrier (TGV, OUIGO, Intercités, TER, and — later — regional networks like
Zou).  Providers are tried in order and the first hit wins:

  1. :class:`ExactTariffProvider` — real published OD fares for TGV INOUI /
     OUIGO and Intercités (non-dynamic), keyed by UIC8, loaded from
     ``fares.json`` (built by ``script/build_graph.py``).
  2. :class:`PerKmProvider` — a per-kilometre estimate by carrier, the
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

    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        uo, ud = stn.uic(origin), stn.uic(destination)
        if not uo or not ud:
            return None
        row = self._table().get(f"{uo}|{ud}") or self._table().get(f"{ud}|{uo}")
        if not row:
            return None
        lo, hi, basis = row
        return Fare(min_cents=int(lo), max_cents=int(hi), exact=True, basis=basis)


class PerKmProvider:
    """Distance-based estimate, the universal fallback (incl. TER, regional)."""

    def fare(self, origin: str, destination: str, carrier: str) -> Optional[Fare]:
        km = stn.distance_km(origin, destination)
        if km is None:
            return None
        lo_rate, hi_rate = PER_KM_EUR.get(carrier.upper(), PER_KM_EUR["DEFAULT"])
        lo = BASE_FARE_EUR + km * lo_rate
        hi = BASE_FARE_EUR + km * hi_rate
        return Fare(min_cents=int(lo * 100), max_cents=int(hi * 100),
                    exact=False, basis="per-km")


# Provider chain — first hit wins.  Insert regional providers before PerKm.
REGISTRY: List[FareProvider] = [ExactTariffProvider(), PerKmProvider()]


def estimate_fare(origin: str, destination: str, carrier: str = "TGV") -> Fare:
    """Best available fare for a leg: exact tariff if known, else per-km."""
    for provider in REGISTRY:
        f = provider.fare(origin, destination, carrier)
        if f is not None:
            return f
    return Fare(min_cents=None, max_cents=None, exact=False, basis="unknown")
