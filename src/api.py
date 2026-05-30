"""Thin facade over core -- the public entry point for sncf_max.

Delegates everything to the composable modules:
  core     -> search, autobook, broadcast
  client   -> raw API queries
  finder   -> weird free trip hunting
  decomposition -> multi-leg alternatives
  auth     -> authentication (playwright)
  booking  -> booking (playwright)
  voyages  -> trip management (playwright)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional, List, Dict

from models import Trip, BookingResult, BookingStatus, Session
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from network.core import search, autobook, broadcast, SearchResult
from network.decomposition import TripDecomposer, CompositeTrip
from network.finder import FreeTripFinder, hunt

from booking.auth import login_sync, load_or_login
from booking.booking import book_sync


class TGVMaxAPI:
    """Thin wrapper composing all modules.

    >>> api = TGVMaxAPI()
    >>> result = search("paris", "lyon", date(2025, 6, 15))
    >>> print(result.summary())
    """

    def __init__(self, config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self._session: Optional[Session] = None
        self._client = SNCFMaxClient(config=self.config)

    # -- discovery (delegates to core / client) ---------------------------

    def search(self, origin: str, destination: str,
               trip_date: Optional[date] = None) -> SearchResult:
        return search(origin, destination, trip_date,
                      decompose=True, config=self.config)

    def search_simple(self, origin: str, destination: str,
                      trip_date: Optional[date] = None) -> List[Trip]:
        r = search(origin, destination, trip_date,
                   decompose=False, config=self.config)
        return r.direct_free

    def broadcast(self, origin: str,
                  trip_date: Optional[date] = None) -> List[Trip]:
        return broadcast(origin=origin, trip_date=trip_date,
                         config=self.config)

    def hunt_weird(self, origin: str, 
                   trip_date: Optional[date] = None) -> List[Trip]:
        return hunt(origin=origin, trip_date=trip_date).all_free_trips

    # -- decomposition ---------------------------------------------------

    def decompose(self, origin: str, destination: str,
                  trip_date: date) -> List[CompositeTrip]:
        d = TripDecomposer(config=self.config)
        return d.find_max_only_combos(origin, destination, trip_date)

    # -- auth ------------------------------------------------------------

    def login(self, email: str, password: str,
              session_file: Optional[str] = None) -> Optional[Session]:
        session_path = Path(session_file) if session_file else None
        self._session = load_or_login(email, password, self.config, session_path)
        return self._session

    @property
    def is_authenticated(self) -> bool:
        return self._session is not None and self._session.is_valid

    # -- booking ---------------------------------------------------------

    def book(self, trip: Trip) -> BookingResult:
        if not self._session:
            return BookingResult(status=BookingStatus.AUTH_REQUIRED, trip=trip,
                                 message="call login() first")
        return book_sync(trip, session=self._session, config=self.config)

    def auto_book(self, origin: str, destination: str,
                  trip_date: date, email: str, password: str) -> BookingResult:
        return autobook(origin, destination, trip_date, email, password,
                         config=self.config)

    # -- date range helpers ----------------------------------------------

    def search_range(self, origin: str, destination: str,
                     start: date, end: date) -> Dict[date, List[Trip]]:
        return self._client.search_trips_range(
            origin, destination, start, end, only_available=True)

    def find_available_dates(self, origin: str, destination: str,
                             days_ahead: int = 30) -> List[date]:
        return self._client.find_available_dates(
            origin, destination, days_ahead=days_ahead)

    def get_stations(self) -> List[str]:
        return self._client.get_stations()

    def get_routes(self) -> List[tuple]:
        return self._client.get_routes()
