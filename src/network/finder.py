"""Free trip finder -- unearth unusual TGV Max availability.

Strategies:
  - Broadcast: scan every destination from a station in parallel
  - Dead-hour gems: early / late trains nobody books
  - Long-distance luck: >3h or >5h free MAX trips
  - Midday gold: peak-hour (10-15h) trains still available
  - Unpopular routes: regional pairs nobody checks
  - Fully-MAX combos: two-leg all-MAX decomposition

All calls are *parallel* across destinations for speed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Dict, Set, Tuple
import logging

from models import Trip
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from network.dayindex import SIDE_FROM, SIDE_TO

logger = logging.getLogger(__name__)

# Destinations are no longer picked in advance: one sweep of the station's
# day returns every free trip from it, so the old "scan the first 150 stations
# in priority order" list (and the cap that silently hid half the country) is
# gone.  Callers may still pass an explicit ``destinations`` list to filter.


@dataclass
class FreeTripBucket:
    label: str
    trips: List[Trip]
    note: str = ""


@dataclass
class FinderReport:
    timestamp: datetime = field(default_factory=datetime.now)
    origin: str = ""
    trip_date: date = field(default_factory=date.today)
    total_free: int = 0
    buckets: List[FreeTripBucket] = field(default_factory=list)

    @property
    def all_free_trips(self) -> List[Trip]:
        seen: Set[str] = set()
        out: List[Trip] = []
        for bucket in self.buckets:
            for t in bucket.trips:
                key = t.trip_key
                if key not in seen:
                    seen.add(key)
                    out.append(t)
        return sorted(out)

    def summary(self) -> str:
        lines = [
            f"Free trips from {self.origin} on {self.trip_date}",
            f"Total free trips found: {self.total_free}",
            "",
        ]
        for b in self.buckets:
            n = len(b.trips)
            if n:
                lines.append(f"  [{b.label}]  ({n} trips)")
                if b.note:
                    lines.append(f"    {b.note}")
        return "\n".join(lines)


class FreeTripFinder:
    """Hunt unusual TGV Max (free) trips, from station-day sweeps."""

    def __init__(self, config: Optional[SNCFConfig] = None, max_workers: int = 8):
        self.config = config or default_config
        self._client = SNCFMaxClient(config=self.config)
        self._workers = max_workers

    # ------------------------------------------------------------------
    # Parallel broadcast
    # ------------------------------------------------------------------

    def find_all_from(
        self,
        origin: str,
        trip_date: Optional[date] = None,
        destinations: Optional[List[str]] = None,
        max_routes: int = 150,
    ) -> FinderReport:
        """Find every free trip leaving *origin* that day.

        One sweep of the station's day answers all of it: no cap on
        destinations, no per-destination request.  ``destinations`` filters
        the result if given.
        """
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        origin_full = get_station_name(origin)

        index = self._client.day(origin_full, SIDE_FROM, trip_date)
        if destinations is None:
            destinations = index.counterparts()

        free_dict, _paid_dict = self._client.search_all_to_destinations_split(
            origin=origin_full,
            destinations=destinations,
            trip_date=trip_date,
            workers=self._workers,
        )

        all_free: List[Trip] = []
        seen: Set[str] = set()
        for dest, trips in free_dict.items():
            for t in trips:
                key = t.trip_key
                if key not in seen:
                    seen.add(key)
                    all_free.append(t)

        all_free.sort()

        return FinderReport(
            origin=origin_full,
            trip_date=trip_date,
            total_free=len(all_free),
            buckets=[
                self._bucket_dead_hours(all_free),
                self._bucket_long_distance(all_free, 3.0),
                self._bucket_long_distance(all_free, 5.0),
                self._bucket_midday_gold(all_free),
                self._bucket_unpopular_routes(all_free),
                self._bucket_weekend(all_free),
            ],
        )

    def find_all_to(
        self,
        destination: str,
        trip_date: Optional[date] = None,
        origins: Optional[List[str]] = None,
        max_routes: int = 150,
    ) -> FinderReport:
        """Find every free trip arriving at *destination* that day.

        The 'who can get here for free today?' query - the mirror of
        ``find_all_from``, from one sweep into the destination.
        """
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        destination_full = get_station_name(destination)

        index = self._client.day(destination_full, SIDE_TO, trip_date)
        if origins is None:
            origins = index.counterparts()

        free_dict, _paid_dict = self._client.search_all_from_origins_split(
            destination=destination_full,
            origins=origins,
            trip_date=trip_date,
            workers=self._workers,
        )

        all_free: List[Trip] = []
        seen: Set[str] = set()
        for orig, trips in free_dict.items():
            for t in trips:
                key = t.trip_key
                if key not in seen:
                    seen.add(key)
                    all_free.append(t)

        all_free.sort()

        return FinderReport(
            origin=destination_full,
            trip_date=trip_date,
            total_free=len(all_free),
            buckets=[
                self._bucket_dead_hours(all_free),
                self._bucket_long_distance(all_free, 3.0),
                self._bucket_long_distance(all_free, 5.0),
                self._bucket_midday_gold(all_free),
                self._bucket_unpopular_routes(all_free),
                self._bucket_weekend(all_free),
            ],
        )

    # ------------------------------------------------------------------
    # Bucket helpers
    # ------------------------------------------------------------------

    def _bucket_dead_hours(self, trips: List[Trip]) -> FreeTripBucket:
        early = [t for t in trips if t.departure_time < time(7, 0)]
        late = [t for t in trips if t.departure_time > time(21, 0)]
        return FreeTripBucket(
            label="Dead hours (<7am / >9pm)",
            trips=early + late,
            note=f"{len(early)} early, {len(late)} late",
        )

    def _bucket_long_distance(self, trips: List[Trip], min_h: float) -> FreeTripBucket:
        items = [t for t in trips if t.duration.total_seconds() / 3600 >= min_h]
        return FreeTripBucket(
            label=f"Long distance (>={min_h}h)",
            trips=items,
            note=f"{len(items)} trips",
        )

    def _bucket_midday_gold(self, trips: List[Trip]) -> FreeTripBucket:
        items = [t for t in trips if time(10, 0) <= t.departure_time <= time(15, 0)]
        return FreeTripBucket(label="Midday gold (10-15h)", trips=items,
                              note=f"{len(items)} free in peak hours")

    def _bucket_unpopular_routes(self, trips: List[Trip]) -> FreeTripBucket:
        popular = {"PARIS (intramuros)", "LYON (intramuros)", "LYON PART DIEU",
                   "MARSEILLE ST CHARLES", "LILLE FLANDRES", "BORDEAUX ST JEAN"}
        items = [t for t in trips
                 if str(t.origin) not in popular or str(t.destination) not in popular]
        return FreeTripBucket(label="Unpopular / regional", trips=items,
                              note=f"{len(items)} non-hub trips")

    def _bucket_weekend(self, trips: List[Trip]) -> FreeTripBucket:
        items = [t for t in trips if t.departure_date.weekday() in (4, 6)]
        return FreeTripBucket(label="Fri/Sun weekend", trips=items,
                              note=f"{len(items)} trips")

    # ------------------------------------------------------------------
    # Simple single-route search
    # ------------------------------------------------------------------

    def find_from_to(self, origin: str, destination: str,
                     trip_date: Optional[date] = None) -> List[Trip]:
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        free, _paid = self._client.search_all_trips(origin=origin,
                                                     destination=destination,
                                                     trip_date=trip_date)
        return free

    # ------------------------------------------------------------------
    # Decomposed all-MAX combos (parallel over intermediates)
    # ------------------------------------------------------------------

    def find_all_max_combos(
        self,
        origin: str,
        destination: str,
        trip_date: date,
        intermediate_stations: Optional[List[str]] = None,
    ) -> List[Tuple[Trip, Trip]]:
        """2-leg journeys where BOTH legs are MAX.

        Leg 1 (origin->via) and leg 2 (via->destination) are both answered by
        the same two sweeps a detour search uses, so the whole country's
        intermediates cost nothing beyond those sweeps.
        """
        origin_full = get_station_name(origin)
        dest_full = get_station_name(destination)
        o_index = self._client.day(origin_full, SIDE_FROM, trip_date)
        d_index = self._client.day(dest_full, SIDE_TO, trip_date)

        if intermediate_stations is None:
            intermediate_stations = o_index.counterparts()

        combos: List[Tuple[Trip, Trip]] = []
        seen: Set[str] = set()
        for via in intermediate_stations:
            if via in (origin_full, dest_full):
                continue
            free1, _ = self._client._split_rows(o_index.rows_to(via))
            free2, _ = self._client._split_rows(d_index.rows_to(via))
            for t1 in free1:
                for t2 in free2:
                    arr = t1.arrival_datetime
                    dep = t2.departure_datetime
                    if arr < dep and 15 * 60 <= (dep - arr).total_seconds() <= 120 * 60:
                        key = f"{t1.trip_key}|{t2.trip_key}"
                        if key not in seen:
                            seen.add(key)
                            combos.append((t1, t2))

        combos.sort(key=lambda x: (
            datetime.combine(trip_date, x[0].departure_time),
            x[0].duration + x[1].duration,
        ))
        return combos


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def hunt(origin: str = "paris",
         trip_date: Optional[date] = None) -> FinderReport:
    return FreeTripFinder().find_all_from(origin=origin, trip_date=trip_date)


def broadcast(origin: str = "paris",
              trip_date: Optional[date] = None) -> List[Trip]:
    return hunt(origin=origin, trip_date=trip_date).all_free_trips


def broadcast_to(destination: str = "lyon",
                 trip_date: Optional[date] = None) -> List[Trip]:
    """All free trips arriving at *destination* on a date (reverse hunt)."""
    return FreeTripFinder().find_all_to(
        destination=destination, trip_date=trip_date).all_free_trips
