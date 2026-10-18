"""Client for the SNCF Max public API - Trip Discovery.

Every request this client makes goes through one shared budget and one
shared cache (``network.cache``), and the batch questions - "everything
leaving this station that day" - are answered by ``DayIndex`` sweeps
instead of one request per destination.  Both exist because one frontend
click used to cost ~220 requests and 40-220 seconds.
"""

import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any, Optional, Iterator, Set, Tuple, Callable
from urllib.parse import urljoin
from datetime import date, datetime, timedelta
import json
import time
import logging

from models import Trip, Station, TripStatus, SearchCriteria
from config import SNCFConfig, default_config, get_station_name
from network.cache import TTLCache, TokenBucket
from network.dayindex import (
    DayIndex,
    SIDE_FROM,
    SIDE_TO,
    normalise,
    sort_key,
)
from network.fares import estimate_fare

logger = logging.getLogger(__name__)

# Fields every cached query asks for: the four Trip needs, plus the label
# bits the frontend shows.  Asking for less keeps each page ~30% smaller.
_RECORD_FIELDS = (
    "date,origine,destination,heure_depart,heure_arrivee,train_no,"
    "od_happy_card,entity,axe"
)

# Shared by every client instance in the process, deliberately: the budget
# is politeness towards one dataset, not a per-object private allowance.
_RAW_CACHE = TTLCache(ttl=default_config.CACHE_TTL, maxsize=2048)
_DAY_CACHE = TTLCache(ttl=default_config.CACHE_TTL,
                      maxsize=default_config.DAILY_CACHE_SIZE)
_STOPS_CACHE = TTLCache(ttl=default_config.CACHE_TTL, maxsize=4096)
_BUDGET = TokenBucket(rate=default_config.REQUEST_RATE,
                      burst=default_config.REQUEST_BURST)


def _quote(value: str) -> str:
    """Quote a value for an ODSQL `where` clause."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def clear_caches() -> None:
    """Forget every cached answer (tests, and the CLI's --no-cache)."""
    _RAW_CACHE.clear()
    _DAY_CACHE.clear()
    _STOPS_CACHE.clear()


def cache_stats() -> Dict[str, Any]:
    return {
        "raw": _RAW_CACHE.stats(),
        "days": _DAY_CACHE.stats(),
        "stops": _STOPS_CACHE.stats(),
        "waited": round(_BUDGET.waited, 2),
    }


class SNCFMaxClient:
    """Client for the SNCF Max public API (trip discovery).

    Queries the public data.sncf.com API to find TGV Max eligible trips
    and real pricing information.
    """

    _API_RECORDS_URL: str

    def __init__(self, config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://data.sncf.com/explore/dataset/tgvmax/api/",
            "DNT": "1",
            "Connection": "keep-alive",
        })
        self._proxy = self.config.get_proxy_dict()
        if self._proxy:
            self.session.proxies.update(self._proxy)

    @property
    def _records_url(self) -> str:
        base = self.config.PUBLIC_API_BASE_URL.rstrip("/")
        return f"{base}/{self.config.TGVMAX_DATASET}/records"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Spend one request from the process-wide budget."""
        _BUDGET.take()

    def _build_where_clause(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        trip_date: Optional[date] = None,
        only_available: bool = True,
        **extra_filters: str,
    ) -> str:
        conditions: List[str] = []

        if origin:
            station = get_station_name(origin)
            conditions.append(f'origine="{station}"')

        if destination:
            station = get_station_name(destination)
            conditions.append(f'destination="{station}"')

        if trip_date:
            date_str = trip_date.strftime("%Y-%m-%d")
            conditions.append(f"date=date'{date_str}'")

        if only_available:
            conditions.append('od_happy_card="OUI"')

        for field, value in extra_filters.items():
            conditions.append(f'{field}="{value}"')

        return " and ".join(conditions) if conditions else ""

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """One cached, paced query against the records endpoint."""
        key = ("records", self._records_url,
               json.dumps(params, sort_keys=True, default=str))
        return _RAW_CACHE.get_or(key, lambda: self._request_uncached(params))

    def _request_uncached(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._rate_limit()
        response = self.session.get(self._records_url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _parse_trip(self, record: dict) -> Trip:
        return Trip.from_api_response(record)

    # ------------------------------------------------------------------
    # Station-day sweeps
    # ------------------------------------------------------------------

    def day(self, station: str, side: str, trip_date: date) -> DayIndex:
        """Every record touching *station* on *trip_date*, one side.

        Fetched once per (side, station, date) for the whole process and
        shared single-flight, so two clicks at the same moment cost one
        sweep.  ``side`` is :data:`network.dayindex.SIDE_FROM` (what leaves
        the station) or ``SIDE_TO`` (what reaches it).
        """
        station = get_station_name(station)
        key = ("day", side, normalise(station), trip_date.isoformat())
        return _DAY_CACHE.get_or(
            key, lambda: self._load_day(station, side, trip_date))

    def _page_size(self) -> int:
        """Rows per request: the config's PAGE_SIZE, within the API's cap."""
        return max(1, min(int(self.config.PAGE_SIZE or 100), 100))

    def _paged(self, params: Dict[str, Any]) -> List[dict]:
        """Every row matching *params*, paged by ``total_count``.

        The first page reports how many rows there are, so the rest can go in
        parallel instead of guessing until a short page arrives.  Truncating
        at one page is not an option: a long train has more than 100
        origin/destination pairs.
        """
        size = self._page_size()
        base = dict(params, limit=size)
        first = self._request(dict(base, offset=0))
        rows: List[dict] = list(first.get("results", []))
        total = int(first.get("total_count") or len(rows))
        offsets = list(range(size, total, size))
        if offsets:
            def _page(offset: int) -> List[dict]:
                return self._request(dict(base, offset=offset)).get("results", [])
            with ThreadPoolExecutor(
                    max_workers=max(1, self.config.PAGE_WORKERS)) as ex:
                for page in ex.map(_page, offsets):
                    rows.extend(page)
        return rows

    def _load_day(self, station: str, side: str, trip_date: date) -> DayIndex:
        column = "origine" if side == SIDE_FROM else "destination"
        rows = self._paged({
            "where": f'date=date\'{trip_date.isoformat()}\' and {column}="{_quote(station)}"',
            "order_by": "heure_depart",
            "select": _RECORD_FIELDS,
        })
        return DayIndex(rows, station, side, trip_date)

    def train_stops(self, train_no: str, trip_date: date) -> List[Tuple[str, str]]:
        """A train's stops on a date, as (station, HH:MM), in travel order.

        One request per (train, date) for the whole process.  Prefer the
        station-day sweep when it already knows the train - a sweep holds
        every stop a train makes after its origin, which is the only part
        the callers ask about - and fall back to this for the rest.
        """
        key = ("stops", str(train_no), trip_date.isoformat())
        return _STOPS_CACHE.get_or(
            key, lambda: self._load_train_stops(train_no, trip_date))

    def _load_train_stops(self, train_no: str, trip_date: date) -> List[Tuple[str, str]]:
        rows = self._paged({
            "where": self._build_where_clause(
                trip_date=trip_date, only_available=False, train_no=str(train_no)),
            "order_by": "heure_depart",
            "select": _RECORD_FIELDS,
        })
        first_seen: Dict[str, str] = {}
        for record in rows:
            for station_key, time_key in (("origine", "heure_depart"),
                                          ("destination", "heure_arrivee")):
                s = record.get(station_key)
                hhmm = record.get(time_key) or ""
                if s and (s not in first_seen
                          or sort_key(hhmm) < sort_key(first_seen[s])):
                    first_seen[s] = hhmm
        return sorted(first_seen.items(), key=lambda kv: sort_key(kv[1]))

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def get_trips_raw(
        self,
        limit: int = 100,
        offset: int = 0,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        trip_date: Optional[date] = None,
        only_available: bool = True,
        order_by: str = "heure_depart",
        **extra_filters: str,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "limit": min(limit, 100),
            "offset": offset,
            "order_by": order_by,
        }
        where = self._build_where_clause(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=only_available,
            **extra_filters,
        )
        if where:
            params["where"] = where
        return self._request(params)

    # ------------------------------------------------------------------
    # Main search entry point
    # ------------------------------------------------------------------

    def search_trips(
        self,
        origin: str,
        destination: str,
        trip_date: Optional[date] = None,
        only_available: bool = True,
        limit: int = 100,
        **filters: str,
    ) -> List[Trip]:
        """Search for trips (MAX and optionally non-MAX)."""
        response = self.get_trips_raw(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=only_available,
            limit=limit,
            **filters,
        )
        trips: List[Trip] = []
        for record in response.get("results", []):
            trips.append(self._parse_trip(record))
        return trips

    # ------------------------------------------------------------------
    # Search trips with *all* statuses (MAX + non-MAX, for comparison)
    # ------------------------------------------------------------------

    def enrich_with_prices(self, trips: List[Trip]) -> List[Trip]:
        """Fill in price_cents for paid trips using the fare estimation system."""
        for trip in trips:
            if trip.is_free or trip.price_cents is not None:
                continue
            # Use the fare estimation system which handles exact tariffs vs per-km estimates
            f = estimate_fare(str(trip.origin), str(trip.destination), trip.carrier)
            trip.price_cents = f.min_cents
        return trips

    def search_all_trips(
        self,
        origin: str,
        destination: str,
        trip_date: Optional[date] = None,
        limit: int = 200,
    ) -> Tuple[List[Trip], List[Trip]]:
        """Return (free_trips, paid_trips) for a route+date.

        Only free trips are usable with TGV Max.  Paid trips are shown
        as reference (pricing must still be obtained from the API or
        estimated).
        """
        response = self.get_trips_raw(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=False,  # get everything
            limit=limit,
        )
        free: List[Trip] = []
        paid: List[Trip] = []
        for record in response.get("results", []):
            trip = self._parse_trip(record)
            if trip.is_free:
                free.append(trip)
            else:
                paid.append(trip)
        return free, paid

    # ------------------------------------------------------------------
    # Range queries
    # ------------------------------------------------------------------

    def search_trips_range(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        only_available: bool = True,
    ) -> Dict[date, List[Trip]]:
        results: Dict[date, List[Trip]] = {}
        current = start_date
        while current <= end_date:
            results[current] = self.search_trips(
                origin=origin,
                destination=destination,
                trip_date=current,
                only_available=only_available,
            )
            current += timedelta(days=1)
        return results

    def iter_all_trips(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        trip_date: Optional[date] = None,
        only_available: bool = True,
        **filters: str,
    ) -> Iterator[Trip]:
        offset = 0
        limit = 100
        while True:
            response = self.get_trips_raw(
                origin=origin,
                destination=destination,
                trip_date=trip_date,
                only_available=only_available,
                limit=limit,
                offset=offset,
                **filters,
            )
            results = response.get("results", [])
            if not results:
                break
            for record in results:
                yield self._parse_trip(record)
            if len(results) < limit:
                break
            offset += limit

    # ------------------------------------------------------------------
    # Convenience / helpers
    # ------------------------------------------------------------------

    def find_available_dates(
        self,
        origin: str,
        destination: str,
        start_date: Optional[date] = None,
        days_ahead: int = 30,
    ) -> List[date]:
        if start_date is None:
            start_date = date.today()
        end_date = start_date + timedelta(days=days_ahead)
        trips_by_date = self.search_trips_range(
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            only_available=True,
        )
        return [d for d, trips in trips_by_date.items() if trips]

    def get_earliest(self, origin: str, destination: str, trip_date: date) -> Optional[Trip]:
        trips = self.search_trips(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=True,
            limit=1,
            order_by="heure_depart",
        )
        return trips[0] if trips else None

    # ------------------------------------------------------------------
    # Station / route introspection
    # ------------------------------------------------------------------

    def get_stations(self) -> List[str]:
        params: Dict[str, Any] = {
            "select": "origine",
            "group_by": "origine",
            "limit": 100,
        }
        data = self._request(params)
        stations = [
            r.get("origine") for r in data.get("results", []) if r.get("origine")
        ]
        return sorted(set(stations))

    # ------------------------------------------------------------------
    # Batch search — one station-day sweep answers every destination
    # ------------------------------------------------------------------

    def _split_rows(self, rows: List[dict]) -> Tuple[List[Trip], List[Trip]]:
        free: List[Trip] = []
        paid: List[Trip] = []
        for record in rows:
            trip = self._parse_trip(record)
            (free if trip.is_free else paid).append(trip)
        return free, paid

    def search_all_to_destinations(
        self,
        origin: str,
        destinations: List[str],
        trip_date: Optional[date] = None,
        only_available: bool = True,
        workers: int = 8,
    ) -> Dict[str, List[Trip]]:
        """Every destination reachable from *origin*, from one sweep.

        ``destinations`` still filters what is returned, so callers keep
        their own candidate set; ``workers`` is accepted for compatibility
        and ignored - the sweep does its own parallel paging.
        """
        free, paid = self.search_all_to_destinations_split(
            origin=origin, destinations=destinations, trip_date=trip_date)
        results = dict(paid)
        if only_available:
            results = dict(free)
        else:
            for dest, trips in free.items():
                results[dest] = results.get(dest, []) + trips
        return results

    def search_all_to_destinations_split(
        self,
        origin: str,
        destinations: List[str],
        trip_date: Optional[date] = None,
        workers: int = 8,
    ) -> Tuple[Dict[str, List[Trip]], Dict[str, List[Trip]]]:
        """(free_dict, paid_dict) per destination, from one origin sweep."""
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        index = self.day(get_station_name(origin), SIDE_FROM, trip_date)
        free_dict: Dict[str, List[Trip]] = {}
        paid_dict: Dict[str, List[Trip]] = {}
        for dest in destinations:
            free, paid = self._split_rows(index.rows_to(dest))
            if free:
                free_dict[dest] = free
            if paid:
                paid_dict[dest] = paid
        return free_dict, paid_dict

    def search_all_from_origins_split(
        self,
        destination: str,
        origins: List[str],
        trip_date: Optional[date] = None,
        workers: int = 8,
    ) -> Tuple[Dict[str, List[Trip]], Dict[str, List[Trip]]]:
        """(free_dict, paid_dict) per origin, from one sweep into the destination."""
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        index = self.day(get_station_name(destination), SIDE_TO, trip_date)
        free_dict: Dict[str, List[Trip]] = {}
        paid_dict: Dict[str, List[Trip]] = {}
        for orig in origins:
            free, paid = self._split_rows(index.rows_to(orig))
            if free:
                free_dict[orig] = free
            if paid:
                paid_dict[orig] = paid
        return free_dict, paid_dict

    def get_routes(self, limit: int = 100) -> List[Tuple[str, str]]:
        params: Dict[str, Any] = {
            "select": "origine, destination",
            "group_by": "origine, destination",
            "limit": limit,
        }
        data = self._request(params)
        routes = [
            (r.get("origine"), r.get("destination"))
            for r in data.get("results", [])
            if r.get("origine") and r.get("destination")
        ]
        return sorted(set(routes))
