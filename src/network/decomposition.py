"""Trip decomposition via graph traversal.

Uses the real TGV network graph (from SNCF NETEX data) to find
multi-hop paths between stations.  Query the API in parallel for
each candidate path to check MAX availability.

Replaces the old hardcoded intermediate-stations dict with
real topological data.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Set, Tuple
import logging

from models import Trip, Station
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from network.graph import resolve, neighbors, find_paths, graph_to_api

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
    """Finds multi-leg TGV Max alternatives via graph traversal.

    Uses the real TGV network graph (from SNCF NETEX data) for BFS
    pathfinding, then queries the API in parallel for each candidate.

    Usage:
        d = TripDecomposer()
        combos = d.find_max_only_combos("paris", "lyon", date(2025, 6, 15))
    """

    MIN_CONNECTION_TIME = 15
    MAX_CONNECTION_TIME = 120
    MAX_HOPS = 3

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

        o_node = resolve(origin_full) or origin_full
        d_node = resolve(dest_full) or dest_full

        # 1. Direct trips — use API names (origin_full/dest_full), not graph names
        direct = self._fetch(origin_full, dest_full, trip_date, only_max=not include_paid)
        for t in direct:
            if departure_after and t.departure_time < departure_after:
                continue
            if arrival_before and t.arrival_time > arrival_before:
                continue
            alternatives.append(CompositeTrip(legs=[TripLeg(trip=t, is_max=t.is_free)]))

        # 2. Multi-hop via graph BFS — convert graph nodes to API names
        paths = find_paths(o_node, d_node, self.MAX_HOPS)
        multi = [p for p in paths if len(p) >= 3]  # at least 2 legs

        if multi:
            _dep = departure_after
            _arr = arrival_before
            _paid = include_paid
            _max_price = max_price_cents

            def _eval(path: List[str]) -> List[CompositeTrip]:
                local: List[CompositeTrip] = []
                # Convert graph node names to API station names
                api_path = [graph_to_api(node) for node in path]
                legs_data = [
                    self._fetch(api_path[i], api_path[i+1], trip_date, only_max=not _paid)
                    for i in range(len(api_path) - 1)
                ]
                if not all(legs_data):
                    return local
                if len(legs_data) == 2:
                    for t1 in legs_data[0]:
                        if _dep and t1.departure_time < _dep:
                            continue
                        for t2 in legs_data[1]:
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
                futures = {ex.submit(_eval, p): p for p in multi}
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

        from network.finder import _get_scan_stations
        stations = [s for s in _get_scan_stations() if s != origin_full]

        # fetch all free trips from origin (parallel)
        free_dict = self._client.search_all_to_destinations(
            origin=origin_full, destinations=stations[:60],
            trip_date=trip_date, only_available=True, workers=self._workers)

        results: List[CompositeTrip] = []
        seen: Set[str] = set()

        for _dest, trips in free_dict.items():
            for trip in trips:
                if not trip.is_free or trip.trip_key in seen:
                    continue
                seen.add(trip.trip_key)

                # query API for ALL entries with same train_no on same date
                response = self._client.get_trips_raw(
                    trip_date=trip_date,
                    only_available=False,
                    limit=100,
                    train_no=trip.train_number,
                )
                stops: Set[str] = set()
                for record in response.get("results", []):
                    o = record.get("origine", "")
                    d = record.get("destination", "")
                    if o:
                        stops.add(o)
                    if d:
                        stops.add(d)

                # does the target station appear in the stop list?
                if any(target_full.upper() in s.upper() or s.upper() in target_full.upper()
                       for s in stops):
                    # skip if trip already goes directly to target (shown in DIRECT)
                    if str(trip.destination).upper() == target_full.upper():
                        continue
                    results.append(CompositeTrip(legs=[TripLeg(trip=trip, is_max=True)]))

        results.sort(key=lambda c: c.score)
        return results


def find_trip_with_decomposition(origin: str, destination: str, trip_date: date,
                                  config: Optional[SNCFConfig] = None) -> List[CompositeTrip]:
    return TripDecomposer(config=config).find_alternatives(
        origin, destination, trip_date, include_paid=True)
