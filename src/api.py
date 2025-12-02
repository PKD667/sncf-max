"""High-level API for SNCF Max trip discovery and booking."""

from __future__ import annotations

import asyncio
from typing import Optional, List, Callable, Union
from datetime import date, datetime, time, timedelta
from dataclasses import dataclass, field

from models import (
    Trip, BookingResult, BookingStatus, Session, 
    UserCredentials, TripStatus, SearchCriteria
)
from config import SNCFConfig, default_config, get_station_name
from client import SNCFMaxClient

# Optional imports for booking (require playwright)
try:
    from auth import SNCFAuthenticator, AuthenticationError, load_or_login
    from booking import SNCFBookingClient, book_sync
    BOOKING_AVAILABLE = True
except ImportError:
    BOOKING_AVAILABLE = False
    SNCFAuthenticator = None  # type: ignore
    AuthenticationError = Exception  # type: ignore
    load_or_login = None  # type: ignore
    SNCFBookingClient = None  # type: ignore
    book_sync = None  # type: ignore


@dataclass
class TGVMaxAPI:
    """Unified API for TGV Max trip discovery and booking.
    
    Example usage:
        
        # Simple search
        api = TGVMaxAPI()
        trips = api.search("paris", "lyon", date(2025, 1, 15))
        
        # Book a trip
        api.login("email@example.com", "password")
        result = api.book(trips[0])
        
        # Or use auto-book
        result = api.auto_book("paris", "lyon", date(2025, 1, 15))
    """
    
    config: SNCFConfig = field(default_factory=lambda: default_config)
    _discovery_client: Optional[SNCFMaxClient] = field(default=None, init=False)
    _session: Optional[Session] = field(default=None, init=False)
    
    def __post_init__(self):
        self._discovery_client = SNCFMaxClient(self.config)
    
    # ==========================================================================
    # DISCOVERY (Public API)
    # ==========================================================================
    
    def search(self,
               origin: str,
               destination: str,
               trip_date: Optional[date] = None,
               only_available: bool = True,
               limit: int = 100) -> List[Trip]:
        """Search for TGV Max eligible trips.
        
        Args:
            origin: Origin station (name or alias like "paris", "lyon")
            destination: Destination station
            trip_date: Specific date (defaults to tomorrow)
            only_available: Only return trips with TGV Max availability
            limit: Maximum results
            
        Returns:
            List of Trip objects
        """
        if trip_date is None:
            trip_date = date.today() + timedelta(days=1)
        
        return self._discovery_client.search_trips(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=only_available,
            limit=limit
        )
    
    def search_range(self,
                     origin: str,
                     destination: str,
                     start_date: date,
                     end_date: date,
                     only_available: bool = True) -> dict[date, List[Trip]]:
        """Search for trips across a date range.
        
        Args:
            origin: Origin station
            destination: Destination station
            start_date: First date to search
            end_date: Last date to search (inclusive)
            only_available: Only return available trips
            
        Returns:
            Dictionary mapping dates to trip lists
        """
        return self._discovery_client.search_trips_range(
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            only_available=only_available
        )
    
    def find_available_dates(self,
                             origin: str,
                             destination: str,
                             days_ahead: int = 30) -> List[date]:
        """Find all dates with available TGV Max trips.
        
        Args:
            origin: Origin station
            destination: Destination station
            days_ahead: Number of days to search (max 30 for TGV Max)
            
        Returns:
            List of dates with availability
        """
        return self._discovery_client.find_available_dates(
            origin=origin,
            destination=destination,
            days_ahead=min(days_ahead, 30)
        )
    
    def get_stations(self) -> List[str]:
        """Get list of all station names."""
        return self._discovery_client.get_stations()
    
    def get_routes(self) -> List[tuple]:
        """Get list of all origin-destination pairs."""
        return self._discovery_client.get_routes()
    
    # ==========================================================================
    # AUTHENTICATION
    # ==========================================================================
    
    def login(self, email: str, password: str, session_file: Optional[str] = None) -> Session:
        """Log in to SNCF Connect.
        
        Args:
            email: SNCF Connect email
            password: SNCF Connect password
            
        Returns:
            Session object
            
        Raises:
            AuthenticationError: If login fails
            ImportError: If playwright is not installed
        """
        if not BOOKING_AVAILABLE:
            raise ImportError("Playwright is required for login. Install with: pip install playwright")
        from pathlib import Path

        session_path = Path(session_file) if session_file else None
        self._session = load_or_login(email, password, self.config, session_path)
        return self._session
    
    def login_async(self, credentials: UserCredentials) -> Session:
        """Async login for use in async contexts."""
        if not BOOKING_AVAILABLE:
            raise ImportError("Playwright is required for login. Install with: pip install playwright")
        async def _login():
            async with SNCFAuthenticator(self.config) as auth:
                self._session = await auth.login(credentials)
                return self._session
        return asyncio.run(_login())
    
    def load_session(self) -> Optional[Session]:
        """Load a previously saved session."""
        if not BOOKING_AVAILABLE:
            return None
        auth = SNCFAuthenticator(self.config)
        self._session = auth.load_session()
        return self._session
    
    @property
    def is_authenticated(self) -> bool:
        """Check if we have a valid session."""
        return self._session is not None and self._session.is_valid
    
    # ==========================================================================
    # BOOKING
    # ==========================================================================
    
    def book(self, trip: Trip) -> BookingResult:
        """Book a specific trip.
        
        Args:
            trip: The trip to book
            
        Returns:
            BookingResult with status and details
            
        Note:
            You must be logged in first via login() or provide credentials
            via environment variables.
        """
        if not BOOKING_AVAILABLE:
            return BookingResult(
                status=BookingStatus.FAILED,
                trip=trip,
                message="Playwright is required for booking. Install with: pip install playwright"
            )
        
        if not self._session:
            # Try to load existing session
            self.load_session()
        
        if not self._session:
            return BookingResult(
                status=BookingStatus.AUTH_REQUIRED,
                trip=trip,
                message="Not authenticated. Call login() first."
            )
        
        return book_sync(trip, session=self._session, config=self.config)
    
    async def book_async(self, trip: Trip) -> BookingResult:
        """Async version of book()."""
        if not BOOKING_AVAILABLE:
            return BookingResult(
                status=BookingStatus.FAILED,
                trip=trip,
                message="Playwright is required for booking."
            )
        
        if not self._session:
            return BookingResult(
                status=BookingStatus.AUTH_REQUIRED,
                trip=trip,
                message="Not authenticated. Call login() first."
            )
        
        async with SNCFBookingClient(session=self._session, config=self.config) as client:
            return await client.book_trip(trip)
    
    def book_multiple(self, trips: List[Trip]) -> List[BookingResult]:
        """Book multiple trips.
        
        Args:
            trips: List of trips to book
            
        Returns:
            List of BookingResults (one per trip)
        """
        if not self._session:
            self.load_session()
        
        if not self._session:
            return [
                BookingResult(
                    status=BookingStatus.AUTH_REQUIRED,
                    trip=trip,
                    message="Not authenticated"
                )
                for trip in trips
            ]
        
        async def _book_all():
            async with SNCFBookingClient(session=self._session, config=self.config) as client:
                return await client.book_multiple(trips)
        
        return asyncio.run(_book_all())
    
    # ==========================================================================
    # HIGH-LEVEL CONVENIENCE METHODS
    # ==========================================================================
    
    def auto_book(self,
                  origin: str,
                  destination: str,
                  trip_date: date,
                  preferred_time: Optional[str] = None,
                  email: Optional[str] = None,
                  password: Optional[str] = None) -> BookingResult:
        """Automatically search and book the best available trip.
        
        This is the easiest way to book a trip - it handles everything:
        1. Searches for available trips
        2. Selects the best one (earliest or closest to preferred time)
        3. Authenticates if needed
        4. Books the trip
        
        Args:
            origin: Origin station
            destination: Destination station
            trip_date: Date of travel
            preferred_time: Optional preferred departure time (HH:MM)
            email: SNCF Connect email (uses env var if not provided)
            password: SNCF Connect password (uses env var if not provided)
            
        Returns:
            BookingResult
        """
        # Search for trips
        trips = self.search(origin, destination, trip_date, only_available=True)
        
        if not trips:
            return BookingResult(
                status=BookingStatus.NO_AVAILABILITY,
                trip=Trip(
                    train_number="N/A",
                    origin=get_station_name(origin),
                    destination=get_station_name(destination),
                    departure_date=trip_date,
                    departure_time=time(0, 0),
                    arrival_time=time(0, 0),
                ),
                message="No TGV Max trips available"
            )
        
        # Select best trip
        selected = trips[0]
        if preferred_time:
            target = datetime.strptime(preferred_time, "%H:%M").time()
            selected = min(
                trips,
                key=lambda t: abs(
                    datetime.combine(trip_date, t.departure_time) -
                    datetime.combine(trip_date, target)
                ).total_seconds()
            )
        
        # Ensure authenticated
        if not self.is_authenticated:
            email = email or self.config.SNCF_EMAIL
            password = password or self.config.SNCF_PASSWORD
            
            if not email or not password:
                return BookingResult(
                    status=BookingStatus.AUTH_REQUIRED,
                    trip=selected,
                    message="Credentials required"
                )
            
            try:
                self.login(email, password)
            except AuthenticationError as e:
                return BookingResult(
                    status=BookingStatus.AUTH_REQUIRED,
                    trip=selected,
                    message=str(e)
                )
        
        # Book
        return self.book(selected)
    
    def find_and_book_first(self,
                            origin: str,
                            destination: str,
                            start_date: Optional[date] = None,
                            days_to_search: int = 7) -> Optional[BookingResult]:
        """Find the first available trip and book it.
        
        Args:
            origin: Origin station
            destination: Destination station
            start_date: Start searching from this date (default: tomorrow)
            days_to_search: Number of days to search
            
        Returns:
            BookingResult if a trip was found and booked, None otherwise
        """
        if start_date is None:
            start_date = date.today() + timedelta(days=1)
        
        end_date = start_date + timedelta(days=days_to_search)
        
        trips_by_date = self.search_range(
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            only_available=True
        )
        
        # Find first available
        for trip_date in sorted(trips_by_date.keys()):
            trips = trips_by_date[trip_date]
            if trips:
                return self.book(trips[0])
        
        return None


# ==========================================================================
# CONVENIENCE FUNCTIONS
# ==========================================================================

def quick_search(origin: str, destination: str, trip_date: date) -> List[Trip]:
    """Quick search for trips without creating an API instance."""
    return TGVMaxAPI().search(origin, destination, trip_date)


def quick_book(origin: str, 
               destination: str, 
               trip_date: date,
               email: str,
               password: str,
               preferred_time: Optional[str] = None) -> BookingResult:
    """Quick booking without creating an API instance."""
    api = TGVMaxAPI()
    return api.auto_book(
        origin=origin,
        destination=destination,
        trip_date=trip_date,
        preferred_time=preferred_time,
        email=email,
        password=password
    )

