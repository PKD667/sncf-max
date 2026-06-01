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


def _sort_key(hhmm: str) -> str:
    """Order stops by clock time, pushing after-midnight times to the end so a
    train running e.g. 22:50 -> 00:40 keeps its real stop order."""
    if not hhmm:
        return "z"
    return ("1" + hhmm) if hhmm < "04:00" else ("0" + hhmm)


@dataclass
class TripLeg:
    trip: Trip
    is_max: bool


@dataclass
class CompositeTrip:
    legs: List[TripLeg]
    # Descentres ("book longer, get off early"): the train rides past the
    # target, so the journey that matters ends where you alight, not at the
    # booked terminus.  When set, these override destination/arrival_time.
    alight_station: Optional[str] = None
    alight_time: Optional[time] = None

    @property
    def total_duration(self) -> timedelta:
        if not self.legs:
            return timedelta()
        d = datetime.combine(self.legs[0].trip.departure_date, self.legs[0].trip.departure_time)
        a = datetime.combine(self.legs[-1].trip.departure_date, self.arrival_time)
        if self.arrival_time < self.legs[0].trip.departure_time:
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
    def total_fare(self) -> "Fare":
        """Combined fare for the paid legs (MAX legs are free).

        Each paid leg is priced via the fare registry — exact published
        tariff when known, otherwise a per-km estimate.  Returns a Fare whose
        ``exact`` flag is True only if every paid leg was exact."""
        from network.fares import estimate_fare, Fare
        paid = [leg for leg in self.legs if not leg.is_max]
        if not paid:
            return Fare(0, 0, True, "max")
        lo = hi = 0
        exact = True
        known = False
        for leg in paid:
            f = estimate_fare(str(leg.trip.origin), str(leg.trip.destination),
                              leg.trip.carrier)
            if f.min_cents is None:
                exact = False
                continue
            lo += f.min_cents
            hi += f.max_cents or f.min_cents
            exact = exact and f.exact
            known = True
        if not known:
            return Fare(None, None, False, "unknown")
        return Fare(lo, hi, exact, "composite")

    @property
    def total_price_cents(self) -> Optional[int]:
        return self.total_fare.min_cents

    @property
    def price_display(self) -> str:
        if self.is_fully_max:
            return "MAX (0EUR)"
        return self.total_fare.display

    @property
    def origin(self) -> str:
        return str(self.legs[0].trip.origin) if self.legs else ""

    @property
    def destination(self) -> str:
        if self.alight_station:
            return self.alight_station
        return str(self.legs[-1].trip.destination) if self.legs else ""

    @property
    def booked_to(self) -> str:
        """Where the ticket actually runs to (differs from destination only
        for descentres, where you get off early at the target)."""
        return str(self.legs[-1].trip.destination) if self.legs else ""

    @property
    def is_descentre(self) -> bool:
        return self.alight_station is not None

    @property
    def departure_date(self) -> date:
        return self.legs[0].trip.departure_date if self.legs else date.today()

    @property
    def departure_time(self) -> time:
        return self.legs[0].trip.departure_time if self.legs else time(0, 0)

    @property
    def arrival_time(self) -> time:
        if self.alight_time:
            return self.alight_time
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
    # Cap on transfer stations to probe per detour search (bounds API calls).
    MAX_INTERMEDIATES = 30

    def __init__(self, config: Optional[SNCFConfig] = None, max_workers: int = 8):
        self.config = config or default_config
        self._client = SNCFMaxClient(config=self.config)
        self._workers = max_workers
        self._cache: Dict[str, List[Trip]] = {}
        self._stops_cache: Dict[str, List[Tuple[str, str]]] = {}

    def _cache_key(self, origin: str, destination: str, date: date, only_max: bool) -> str:
        return f"{origin}|{destination}|{date}|{only_max}"

    def _ordered_stops(self, train_no: str, trip_date: date) -> List[Tuple[str, str]]:
        """A train's stops on a date as (station, HH:MM), in travel order.

        Reconstructed from all the train's (origine, destination) records,
        ordered by time — the exact, stop-wise truth used to decide where you
        can get on and off, and when.  Cached.
        """
        key = f"{train_no}|{trip_date}"
        if key not in self._stops_cache:
            resp = self._client.get_trips_raw(
                trip_date=trip_date, only_available=False, limit=100, train_no=train_no)
            first_seen: Dict[str, str] = {}
            for record in resp.get("results", []):
                for station_key, time_key in (("origine", "heure_depart"),
                                              ("destination", "heure_arrivee")):
                    s = record.get(station_key)
                    hhmm = record.get(time_key) or ""
                    if s and (s not in first_seen or _sort_key(hhmm) < _sort_key(first_seen[s])):
                        first_seen[s] = hhmm
            self._stops_cache[key] = sorted(
                first_seen.items(), key=lambda kv: _sort_key(kv[1]))
        return self._stops_cache[key]

    @staticmethod
    def _pos(stops: List[Tuple[str, str]], name: str) -> int:
        up = name.upper()
        for i, (s, _) in enumerate(stops):
            su = s.upper()
            if up == su or up in su or su in up:
                return i
        return -1

    def _passes_through(self, trip: Trip, target: str, trip_date: date) -> bool:
        """Does *trip*'s train reach *target* strictly between where you board
        (its origin) and where your ticket ends (its destination)?

        This is the single test behind both descentres (book this trip, get
        off early at the target) and detour redundancy (if the first leg
        already passes the target, you'd never change trains).  A target that
        sits *after* the booked destination — e.g. Marseille on a Lyon->Valence
        trip — fails, because you can't ride past your ticket.
        """
        stops = self._ordered_stops(trip.train_number, trip_date)
        o = self._pos(stops, str(trip.origin))
        x = self._pos(stops, str(trip.destination))
        t = self._pos(stops, target)
        return o >= 0 and x >= 0 and t >= 0 and o < t < x

    def _arrival_at(self, trip: Trip, target: str, trip_date: date) -> Optional[time]:
        """Clock time the train reaches *target* (for descentre filtering)."""
        stops = self._ordered_stops(trip.train_number, trip_date)
        i = self._pos(stops, target)
        if i < 0 or not stops[i][1]:
            return None
        try:
            return datetime.strptime(stops[i][1], "%H:%M").time()
        except ValueError:
            return None

    def _fetch(self, origin: str, destination: str, trip_date: date,
               only_max: bool = True) -> List[Trip]:
        key = self._cache_key(origin, destination, trip_date, only_max)
        if key not in self._cache:
            free, paid = self._client.search_all_trips(
                origin=origin, destination=destination, trip_date=trip_date)
            trips = free if only_max else free + paid
            # TER legs are paid (per-km), so only when paid legs are allowed
            if not only_max:
                from network import ter
                if ter.has_data():
                    trips = trips + ter.legs_between(origin, destination, trip_date)
            self._cache[key] = trips
        return self._cache[key]

    def _transfer_candidates(self, o_canon: str, d_canon: str,
                             trip_date: date, include_ter: bool) -> List[str]:
        """Stations to change trains at, across MAX *and* TER (when allowed).

        A via M works if you can ride origin->M and M->dest, by either mode —
        so a MAX leg can hand off to a TER leg (e.g. arrive Valence TGV by
        MAX, take the TER navette onward)."""
        max_from_o = set(stn.neighbors(o_canon))
        graph = stn.graph()
        max_into_d = {x for x in graph if d_canon in graph.get(x, [])}
        vias = set(stn.transfer_stations(o_canon, d_canon))
        if include_ter:
            from network import ter
            if ter.has_data():
                from_o = max_from_o | set(ter.destinations(o_canon, trip_date))
                into_d = max_into_d | set(ter.origins(d_canon, trip_date))
                vias |= (from_o & into_d)
        vias.discard(o_canon)
        vias.discard(d_canon)

        # Prioritise vias that keep a free MAX leg (reachable from origin or
        # into destination by MAX), so the cap never drops the useful
        # multimodal handoffs (e.g. a MAX leg to Valence TGV before the cap).
        def _priority(v: str) -> tuple:
            free_leg = (v in max_from_o) or (v in max_into_d)
            return (0 if free_leg else 1, v)

        return sorted(vias, key=_priority)

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

        # 2. Change-of-train detours — for when no single MAX train covers
        #    origin->dest on this date (the whole point of the tool).  We probe
        #    every transfer station M where origin->M and M->dest are both real
        #    train rides, build 2-leg combos, then drop the redundant ones: if
        #    the first leg's actual train already stops at the destination you
        #    wouldn't change trains, you'd just stay on it and get off there
        #    (that's a descentre).  So Paris->St-Etienne->Lyon is dropped (that
        #    train stops at Lyon) while Paris->Le Creusot->Lyon survives.
        intermediates = self._transfer_candidates(
            o_canon, d_canon, trip_date, include_ter=include_paid)[: self.MAX_INTERMEDIATES]
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

            raw_detours: List[CompositeTrip] = []
            with ThreadPoolExecutor(max_workers=self._workers) as ex:
                futures = {ex.submit(_eval, v): v for v in intermediates}
                for future in as_completed(futures):
                    raw_detours.extend(future.result())

            # Drop detours whose first-leg ticket already passes through the
            # destination (you'd just get off there — a descentre, not a
            # change of trains).  Verified exactly against each first-leg
            # train's real ordered stops, prefetched in parallel.
            leg1_trains = {c.legs[0].trip.train_number for c in raw_detours}
            with ThreadPoolExecutor(max_workers=self._workers) as ex:
                list(ex.map(lambda tn: self._ordered_stops(tn, trip_date), leg1_trains))
            for c in raw_detours:
                if not self._passes_through(c.legs[0].trip, d_canon, trip_date):
                    alternatives.append(c)

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

    def find_descentres(self, origin: str, target: str, trip_date: date,
                        departure_after: Optional[time] = None,
                        arrival_before: Optional[time] = None) -> List[CompositeTrip]:
        """Book a longer MAX trip and get off early at the target stop.

        Stop-wise and exact: a candidate is a MAX trip origin->X whose train
        actually stops at the target on the way (verified against the train's
        real stop list).  We prefilter X to stations rideable from *both* the
        origin and the target — i.e. plausibly past the target — then confirm
        each train truly passes through the target before keeping it.

        Time filters apply to the journey you actually take: *departure_after*
        to when you leave the origin, *arrival_before* to when you reach the
        target (where you get off), not the train's booked terminus.
        """
        origin_full = get_station_name(origin)
        target_full = get_station_name(target)
        o_canon = stn.resolve(origin_full) or origin_full
        t_canon = stn.resolve(target_full) or target_full

        # Termini worth probing: rideable from the origin AND from the target,
        # so a single origin->X train can plausibly stop at the target en
        # route.  (Exact stop verification below rejects the false positives,
        # e.g. an origin->X train that doesn't actually pass the target.)
        from_o = set(stn.neighbors(o_canon))
        from_t = set(stn.neighbors(t_canon))
        destinations = sorted((from_o & from_t) - {o_canon, t_canon})
        if not destinations:
            return []

        # fetch all free trips from origin to those termini (parallel)
        free_dict = self._client.search_all_to_destinations(
            origin=origin_full, destinations=destinations,
            trip_date=trip_date, only_available=True, workers=self._workers)

        # One candidate trip per (train_no), skipping trips already going
        # straight to the target (those show up under DIRECT) and those that
        # leave too early for the requested window.
        candidates: Dict[str, Trip] = {}
        for _dest, trips in free_dict.items():
            for trip in trips:
                if not trip.is_free:
                    continue
                if str(trip.destination).upper() == t_canon.upper():
                    continue
                if departure_after and trip.departure_time < departure_after:
                    continue
                candidates.setdefault(trip.train_number, trip)

        results: List[CompositeTrip] = []
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futures = {ex.submit(self._ordered_stops, tn, trip_date): trip
                       for tn, trip in candidates.items()}
            for future in as_completed(futures):
                trip = futures[future]
                try:
                    future.result()
                except Exception:
                    continue
                # the train must reach the target *before* its booked
                # terminus, so you can actually get off there
                if not self._passes_through(trip, t_canon, trip_date):
                    continue
                alight = self._arrival_at(trip, t_canon, trip_date)
                if arrival_before and alight and alight > arrival_before:
                    continue
                results.append(CompositeTrip(
                    legs=[TripLeg(trip=trip, is_max=True)],
                    alight_station=t_canon,
                    alight_time=alight,
                ))

        results.sort(key=lambda c: c.score)
        return results


def find_trip_with_decomposition(origin: str, destination: str, trip_date: date,
                                  config: Optional[SNCFConfig] = None) -> List[CompositeTrip]:
    return TripDecomposer(config=config).find_alternatives(
        origin, destination, trip_date, include_paid=True)
