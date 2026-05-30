"""SNCF Max API.
Module-based TGV Max trip discovery and booking.

Usage:
    from core import search, broadcast, hunt
    result = search("paris", "lyon", date(2025, 6, 15))

    # CLI
    $ python -m cli search paris-lyon
"""

__version__ = "0.3.0"

# ---- data ----------------------------------------------------------------
from models import Trip, Station, TripStatus, BookingRequest, BookingResult, \
    BookingStatus, SearchCriteria, UserCredentials, Session

# ---- config --------------------------------------------------------------
from config import SNCFConfig, default_config, get_station_name, STATIONS

# ---- API client (public data) --------------------------------------------
from client import SNCFMaxClient

# ---- core algorithm ------------------------------------------------------
from core import search, autobook, broadcast, SearchResult

# ---- facade --------------------------------------------------------------
from api import TGVMaxAPI

# ---- graph (TGV network topology) ----------------------------------------
from graph import TGVGraph, build_graph

# ---- decomposition -------------------------------------------------------
from decomposition import TripDecomposer, CompositeTrip, TripLeg, \
    find_trip_with_decomposition

# ---- finder --------------------------------------------------------------
from finder import FreeTripFinder, FinderReport, FreeTripBucket, hunt

# ---- monitor -------------------------------------------------------------
from monitor import TGVMaxMonitor, WatchRequest, AlertEvent, quick_monitor

# ---- scheduler -----------------------------------------------------------
from scheduler import TGVMaxScheduler, RecurringTrip, OneTimeTrip, \
    TimeWindow, Weekday, create_weekly_commute

# ---- scanner -------------------------------------------------------------
from scanner import ContinuousScanner, ScanTarget, ScanResult, ScanMode

# ---- booking (playwright required) ---------------------------------------
from booking.auth import SNCFAuthenticator, AuthenticationError, login_sync, load_or_login
from booking.booking import SNCFBookingClient, BookingError, book_sync, auto_book
from booking.voyages import MesVoyagesClient, BookedTrip, VoyagesStatus, TripState, \
    MAX_TGVMAX_BOOKINGS, fetch_my_trips_sync, cancel_trip_sync
from booking.debug import BrowserDebugger, SelectorTest, PageState, \
    run_debug_session, debug_sync

__all__ = [
    "__version__",
    "Trip", "Station", "TripStatus", "BookingRequest", "BookingResult",
    "BookingStatus", "SearchCriteria", "UserCredentials", "Session",
    "SNCFConfig", "default_config", "get_station_name", "STATIONS",
    "SNCFMaxClient",
    "search", "autobook", "broadcast", "SearchResult",
    "TGVMaxAPI",
    "TGVGraph", "build_graph",
    "TripDecomposer", "CompositeTrip", "TripLeg", "find_trip_with_decomposition",
    "FreeTripFinder", "FinderReport", "FreeTripBucket", "hunt",
    "TGVMaxMonitor", "WatchRequest", "AlertEvent", "quick_monitor",
    "TGVMaxScheduler", "RecurringTrip", "OneTimeTrip", "TimeWindow", "Weekday",
    "create_weekly_commute",
    "ContinuousScanner", "ScanTarget", "ScanResult", "ScanMode",
    "SNCFAuthenticator", "AuthenticationError", "login_sync", "load_or_login",
    "SNCFBookingClient", "BookingError", "book_sync", "auto_book",
    "MesVoyagesClient", "BookedTrip", "VoyagesStatus", "TripState",
    "MAX_TGVMAX_BOOKINGS", "fetch_my_trips_sync", "cancel_trip_sync",
    "BrowserDebugger", "SelectorTest", "PageState", "run_debug_session", "debug_sync",
]
