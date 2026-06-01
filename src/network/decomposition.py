"""Trip decomposition via the real, geography-aware TGV graph.

The connection graph comes straight from the SNCF API (every (origine,
destination) pair in the ``tgvmax`` dataset is a real TGV route — see
``network.stations``).  Detour intermediates are pruned geographically so
candidates stay *on the way*: we never propose backtracking to the other
side of the country.  Each surviving candidate is checked against the API in
parallel for actual MAX availability and a valid connection time.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Set, Tuple
import logging

from models import Trip, Station
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from network import stations as stn

logger = logging.getLogger(__name__)

MIN_CONNECTION = 15
MAX_CONNECTION = 120


@dataclass
class TripLeg:
    trip: Trip
    is_max: bool


@dataclass
class CompositeTrip:
    legs: List[TripLeg]

    @property
    def total_duration(self) -> timedelta:
        if not self.legs:
            return timedelta()
        d = datetime.combine(self.legs[0].trip.departure_date, self.legs[0].trip.departure_time)
        a = datetime.combine(self.legs[-1].trip.departure_date, self.legs[-1].trip.arrival_time)
        if self.legs[-1].trip.arrival_time < self.legs[-1].trip.departure_time:
            a += timedelta(days=1)
        return a - d

    @property
    def connection_time(self) -> timedelta:
        travel = sum((leg.trip.duration for leg in self.legs), timedelta())
        ct = self.total_duration - travel
        return ct if ct.total_seconds() > 0 else timedelta()

    @property
    def max_legs(self) -> int:
        return sum(1 for leg in self.legs if leg.is_max)

    @property
    def paid_legs(self) -> int:
        return len(self.legs) - self.max_legs

    @property
    def is_fully_max(self) -> bool:
        return self.paid_legs == 0

    @property
    def is_fully_free(self) -> bool:
        return self.is_fully_max

    @property
    def total_price_cents(self) -> Optional[int]:
        total = sum(
            leg.trip.price_cents
            for leg in self.legs
            if not leg.is_max and leg.trip.price_cents is not None
        )
        return total if total > 0 else None

    @property
    def price_display(self) -> str:
        if self.is_fully_max:
            return "MAX (0EUR)"
        pc = self.total_price_cents
        return f"{pc/100:.2f}EUR" if pc else "Price unknown"

    @property
    def origin(self) -> str:
        return str(self.legs[0].trip.origin) if self.legs else ""

    @property
    def destination(self) -> str:
        return str(self.legs[-1].trip.destination) if self.legs else ""

    @property
    def departure_date(self) -> date:
        return self.legs[0].trip.departure_date if self.legs else date.today()

    @property
    def departure_time(self) -> time:
        return self.legs[0].trip.departure_time if self.legs else time(0, 0)

    @property
    def arrival_time(self) -> time:
        return self.legs[-1].trip.arrival_time if self.legs else time(0, 0)

    @property
    def score(self) -> float:
        penalty = self.paid_legs * 1000
        duration_min = self.total_duration.total_seconds() / 60
        conn_min = self.connection_time.total_seconds() / 60
        return penalty + duration_min + conn_min * 2 + len(self.legs) * 30

    def __str__(self) -> str:
        parts = [
            f"{leg.trip.origin}({leg.trip.departure_time.strftime('%H:%M')})"
            for leg in self.legs
        ]
        parts.append(f"{self.destination}({self.arrival_time.strftime('%H:%M')})")
        info = f"[{self.max_legs} MAX" + (f" + {self.paid_legs} paid]" if self.paid_legs else "]")
        return " -> ".join(parts) + " " + info


class TripDecomposer:
    """Finds 2-leg TGV Max detours via the real, geography-aware graph.

    Intermediate stations are taken from the API connection graph (both legs
    must be real TGV routes) and pruned geographically so they stay on the
    way between origin and destination.  Each candidate is then checked
    against the API in parallel for MAX availability + a valid connection.

    Usage:
        d = TripDecomposer()
        combos = d.find_max_only_combos("paris", "lyon", date(2025, 6, 15))
    """

    MIN_CONNECTION_TIME = 15
    MAX_CONNECTION_TIME = 120
    # Allow a detour up to this multiple of the direct great-circle distance.
    DETOUR_FACTOR = 1.4
    # Cap on intermediates to probe (already small after geo pruning).
    MAX_INTERMEDIATES = 30

    def __init__(self, config: Optional[SNCFConfig] = None, max_workers: int = 8):
        self.config = config or default_config
        self._client = SNCFMaxClient(config=self.config)
        self._workers = max_workers
        self._cache: Dict[str, List[Trip]] = {}

    def _cache_key(self, origin: str, destination: str, date: date, only_max: bool) -> str:
        return f"{origin}|{destination}|{date}|{only_max}"

    def _fetch(self, origin: str, destination: str, trip_date: date,
               only_max: bool = True) -> List[Trip]:
        key = self._cache_key(origin, destination, trip_date, only_max)
        if key not in self._cache:
            free, paid = self._client.search_all_trips(
                origin=origin, destination=destination, trip_date=trip_date)
            self._cache[key] = free if only_max else free + paid
        return self._cache[key]

    def clear_cache(self) -> None:
        self._cache.clear()

    def _detour_intermediates(self, o_canon: str, d_canon: str) -> List[str]:
        """Stations that make a sensible 2-leg detour from o to d.

        A candidate X must have a real TGV route o->X *and* X->d, and lie
        geographically on the way (origin->X->dest within DETOUR_FACTOR of the
        direct distance).  Ranked by how little they deviate, so the most
        natural detours (e.g. Le Creusot for Paris->Lyon) come first.
        """
        graph = stn.graph()
        out = set(graph.get(o_canon, []))
        into = {x for x in graph if d_canon in graph.get(x, [])}
        candidates = [
            x for x in (out & into)
            if x not in (o_canon, d_canon)
            and stn.on_the_way(o_canon, x, d_canon, self.DETOUR_FACTOR)
        ]

        def _deviation(x: str) -> float:
            l1 = stn.distance_km(o_canon, x)
            l2 = stn.distance_km(x, d_canon)
            return (l1 + l2) if (l1 is not None and l2 is not None) else 1e9

        candidates.sort(key=_deviation)
        return candidates[: self.MAX_INTERMEDIATES]

    def _can_connect(self, arr: Trip, dep: Trip) -> bool:
        if arr.departure_date != dep.departure_date:
            return False
        a = arr.arrival_datetime
        d = dep.departure_datetime
        if a >= d:
            return False
        mins = (d - a).total_seconds() / 60
        return self.MIN_CONNECTION_TIME <= mins <= self.MAX_CONNECTION_TIME

    def find_alternatives(
        self,
        origin: str,
        destination: str,
        trip_date: date,
        include_paid: bool = True,
        departure_after: Optional[time] = None,
        arrival_before: Optional[time] = None,
        max_price_cents: Optional[int] = None,
    ) -> List[CompositeTrip]:
        origin_full = get_station_name(origin)
        dest_full = get_station_name(destination)
        alternatives: List[CompositeTrip] = []

        o_canon = stn.resolve(origin_full) or origin_full
        d_canon = stn.resolve(dest_full) or dest_full

        # 1. Direct trips
        direct = self._fetch(origin_full, dest_full, trip_date, only_max=not include_paid)
        for t in direct:
            if departure_after and t.departure_time < departure_after:
                continue
            if arrival_before and t.arrival_time > arrival_before:
                continue
            alternatives.append(CompositeTrip(legs=[TripLeg(trip=t, is_max=t.is_free)]))

        # 2. Geography-aware 2-leg detours through real TGV intermediates
        intermediates = self._detour_intermediates(o_canon, d_canon)
        if intermediates:
            _dep = departure_after
            _arr = arrival_before
            _paid = include_paid
            _max_price = max_price_cents

            def _eval(via: str) -> List[CompositeTrip]:
                local: List[CompositeTrip] = []
                leg1 = self._fetch(o_canon, via, trip_date, only_max=not _paid)
                if not leg1:
                    return local
                leg2 = self._fetch(via, d_canon, trip_date, only_max=not _paid)
                if not leg2:
                    return local
                for t1 in leg1:
                    if _dep and t1.departure_time < _dep:
                        continue
                    for t2 in leg2:
                        if _arr and t2.arrival_time > _arr:
                            continue
                        if not self._can_connect(t1, t2):
                            continue
                        c = CompositeTrip(legs=[
                            TripLeg(trip=t1, is_max=t1.is_free),
                            TripLeg(trip=t2, is_max=t2.is_free),
                        ])
                        if c.max_legs == 0 and not _paid:
                            continue
                        if _max_price and c.total_price_cents and c.total_price_cents > _max_price:
                            continue
                        local.append(c)
                return local

            with ThreadPoolExecutor(max_workers=self._workers) as ex:
                futures = {ex.submit(_eval, v): v for v in intermediates}
                for future in as_completed(futures):
                    alternatives.extend(future.result())

        # Sort and deduplicate
        alternatives.sort(key=lambda x: x.score)
        seen: Set[Tuple[time, time, int]] = set()
        unique: List[CompositeTrip] = []
        for alt in alternatives:
            key = (alt.departure_time, alt.arrival_time, alt.max_legs)
            if key not in seen:
                seen.add(key)
                unique.append(alt)
        return unique

    def find_max_only_combos(self, origin: str, destination: str, trip_date: date) -> List[CompositeTrip]:
        return self.find_alternatives(origin, destination, trip_date, include_paid=False)

    def find_best_alternative(self, origin: str, destination: str, trip_date: date,
                              prefer_fully_max: bool = True) -> Optional[CompositeTrip]:
        alts = self.find_alternatives(origin, destination, trip_date,
                                       include_paid=not prefer_fully_max)
        if prefer_fully_max:
            max_alts = [a for a in alts if a.is_fully_max]
            if max_alts:
                return max_alts[0]
        return alts[0] if alts else None

    def find_descentres(self, origin: str, target: str, trip_date: date) -> List[CompositeTrip]:
        """Book a longer MAX trip and get off early at an intermediate stop.

        Approach: for each free MAX trip from origin, query the API for
        ALL entries with that same train_no on that date. If the target
        station appears among those entries' origins or destinations, the
        train stops there.
        """
        origin_full = get_station_name(origin)
        target_full = get_station_name(target)
        o_canon = stn.resolve(origin_full) or origin_full
        t_canon = stn.resolve(target_full) or target_full

        # Only worth probing destinations the origin can actually reach that
        # lie *beyond* the target (a longer trip we'd cut short at the target).
        # Restrict to those farther along the origin->target axis; fall back to
        # all reachable destinations when coordinates are unknown.
        reachable = [d for d in stn.neighbors(o_canon) if d != o_canon]
        beyond = [d for d in reachable
                  if d != t_canon and stn.farther_along(o_canon, t_canon, d)]
        destinations = beyond or reachable

        # fetch all free trips from origin (parallel)
        free_dict = self._client.search_all_to_destinations(
            origin=origin_full, destinations=destinations,
            trip_date=trip_date, only_available=True, workers=self._workers)

        # One candidate trip per (train_no), skipping trips already going
        # straight to the target (those show up under DIRECT).
        candidates: Dict[str, Trip] = {}
        for _dest, trips in free_dict.items():
            for trip in trips:
                if not trip.is_free:
                    continue
                if str(trip.destination).upper() == t_canon.upper():
                    continue
                candidates.setdefault(trip.train_number, trip)

        def _stops_for(train_no: str) -> Set[str]:
            response = self._client.get_trips_raw(
                trip_date=trip_date, only_available=False,
                limit=100, train_no=train_no)
            stops: Set[str] = set()
            for record in response.get("results", []):
                for key in ("origine", "destination"):
                    s = record.get(key, "")
                    if s:
                        stops.add(s)
            return stops

        target_up = t_canon.upper()
        results: List[CompositeTrip] = []
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futures = {ex.submit(_stops_for, tn): trip for tn, trip in candidates.items()}
            for future in as_completed(futures):
                trip = futures[future]
                try:
                    stops = future.result()
                except Exception:
                    continue
                # does the train actually stop at the target?
                if any(target_up in s.upper() or s.upper() in target_up for s in stops):
                    results.append(CompositeTrip(legs=[TripLeg(trip=trip, is_max=True)]))

        results.sort(key=lambda c: c.score)
        return results


def find_trip_with_decomposition(origin: str, destination: str, trip_date: date,
                                  config: Optional[SNCFConfig] = None) -> List[CompositeTrip]:
    return TripDecomposer(config=config).find_alternatives(
        origin, destination, trip_date, include_paid=True)
