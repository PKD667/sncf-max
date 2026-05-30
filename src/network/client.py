"""Client for the SNCF Max public API - Trip Discovery."""

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Iterator, Set, Tuple, Callable
from urllib.parse import urljoin
from datetime import date, datetime, timedelta
import time
import logging

from models import Trip, Station, TripStatus, SearchCriteria
from config import SNCFConfig, default_config, get_station_name

logger = logging.getLogger(__name__)


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
        self._last_request_time = 0.0
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
        elapsed = time.time() - self._last_request_time
        if elapsed < self.config.REQUEST_DELAY:
            time.sleep(self.config.REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

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
        self._rate_limit()
        response = self.session.get(self._records_url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _parse_trip(self, record: dict) -> Trip:
        return Trip.from_api_response(record)

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
        """Fill in price_cents for paid trips using Navitia API (if configured)."""
        for trip in trips:
            if trip.is_free or trip.price_cents is not None:
                continue
            price = fetch_price(
                str(trip.origin), str(trip.destination),
                trip.departure_date, trip.departure_time.strftime("%H:%M"),
            )
            if price is not None:
                trip.price_cents = price
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
        self._rate_limit()
        resp = self.session.get(self._records_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        stations = [
            r.get("origine") for r in data.get("results", []) if r.get("origine")
        ]
        return sorted(set(stations))

    # ------------------------------------------------------------------
    # Parallel batch search — runs N destinations concurrently
    # ------------------------------------------------------------------

    def search_all_to_destinations(
        self,
        origin: str,
        destinations: List[str],
        trip_date: Optional[date] = None,
        only_available: bool = True,
        workers: int = 8,
    ) -> Dict[str, List[Trip]]:
        """Search origin->each destination in parallel."""
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        origin_full = get_station_name(origin)
        results: Dict[str, List[Trip]] = {}

        def _fetch(dest: str) -> Tuple[str, List[Trip]]:
            trips = self.search_trips(
                origin=origin_full,
                destination=dest,
                trip_date=trip_date,
                only_available=only_available,
            )
            return (dest, trips)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch, d): d for d in destinations}
            for future in as_completed(futures):
                dest, trips = future.result()
                if trips:
                    results[dest] = trips

        return results

    def search_all_to_destinations_split(
        self,
        origin: str,
        destinations: List[str],
        trip_date: Optional[date] = None,
        workers: int = 8,
    ) -> Tuple[Dict[str, List[Trip]], Dict[str, List[Trip]]]:
        """Parallel search, returning (free_dict, paid_dict) per destination."""
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        origin_full = get_station_name(origin)
        free_dict: Dict[str, List[Trip]] = {}
        paid_dict: Dict[str, List[Trip]] = {}

        def _fetch(dest: str) -> Tuple[str, List[Trip], List[Trip]]:
            free, paid = self.search_all_trips(
                origin=origin_full,
                destination=dest,
                trip_date=trip_date,
            )
            return (dest, free, paid)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch, d): d for d in destinations}
            for future in as_completed(futures):
                dest, free, paid = future.result()
                if free:
                    free_dict[dest] = free
                if paid:
                    paid_dict[dest] = paid

        return free_dict, paid_dict

    def get_routes(self, limit: int = 100) -> List[Tuple[str, str]]:
        params: Dict[str, Any] = {
            "select": "origine, destination",
            "group_by": "origine, destination",
            "limit": limit,
        }
        self._rate_limit()
        resp = self.session.get(self._records_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        routes = [
            (r.get("origine"), r.get("destination"))
            for r in data.get("results", [])
            if r.get("origine") and r.get("destination")
        ]
        return sorted(set(routes))
