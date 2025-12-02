"""SNCF Max API - Discover and book TGV Max trips.

This package provides:
- Trip discovery via the public SNCF data API
- Automated booking via browser automation (Playwright)
- Availability monitoring with notifications
- Calendar-based scheduling for recurring trips
- CLI interface for easy usage

Quick Start:
    >>> from src import TGVMaxAPI
    >>> from datetime import date
    >>> 
    >>> api = TGVMaxAPI()
    >>> trips = api.search("paris", "lyon", date(2025, 1, 15))
    >>> print(f"Found {len(trips)} trips")
    >>> 
    >>> # Book a trip (requires authentication)
    >>> api.login("your@email.com", "password")
    >>> result = api.book(trips[0])
    >>> print(result)

CLI Usage:
    $ sncf-max search paris-lyon
    $ sncf-max watch paris-lyon --auto-book
    $ sncf-max trips
    $ sncf-max schedule add paris-lyon --name "Friday" --days fri
"""

__version__ = "0.1.0"

# Core models
from models import (
    Trip,
    Station,
    TripStatus,
    BookingRequest,
    BookingResult,
    BookingStatus,
    SearchCriteria,
    UserCredentials,
    Session,
)

# Configuration
from config import (
    SNCFConfig,
    default_config,
    get_station_name,
    STATIONS,
)

# Discovery client (public API)
from client import SNCFMaxClient

# High-level API
from api import (
    TGVMaxAPI,
    quick_search,
    quick_book,
)

# Scheduler
from scheduler import (
    TGVMaxScheduler,
    RecurringTrip,
    OneTimeTrip,
    TimeWindow,
    Weekday,
    create_weekly_commute,
)

# Scanner
from scanner import (
    ContinuousScanner,
    ScanTarget,
    ScanResult,
    ScanMode,
)

# Trip Decomposition
from decomposition import (
    TripDecomposer,
    CompositeTrip,
    TripLeg,
    find_trip_with_decomposition,
    INTERMEDIATE_STATIONS,
)

# Deadline-based search
from deadline import (
    DeadlineSearcher,
    DeadlineBooker,
    DeadlineConstraint,
    DeadlineMatch,
    DeadlineStrategy,
    search_by_deadline,
    find_best_for_deadline,
    book_for_deadline,
)

# Monitor
from monitor import (
    TGVMaxMonitor,
    WatchRequest,
    AlertEvent,
    watch_and_book,
    quick_monitor,
)

# Authentication (requires playwright)
try:
    from auth import (
        SNCFAuthenticator,
        AuthenticationError,
        login_sync,
        load_or_login,
    )
except ImportError:
    pass

# Booking (requires playwright)
try:
    from booking import (
        SNCFBookingClient,
        BookingError,
        book_sync,
        auto_book,
    )
except ImportError:
    pass

# Voyages / Trip management (requires playwright)
try:
    from voyages import (
        MesVoyagesClient,
        BookedTrip,
        VoyagesStatus,
        TripState,
        MAX_TGVMAX_BOOKINGS,
        fetch_my_trips_sync,
        cancel_trip_sync,
    )
except ImportError:
    pass

# Browser debugging (requires playwright)
try:
    from browser_debug import (
        BrowserDebugger,
        SelectorTest,
        PageState,
        run_debug_session,
        debug_sync,
    )
except ImportError:
    pass

__all__ = [
    # Version
    "__version__",
    
    # Models
    "Trip",
    "Station", 
    "TripStatus",
    "BookingRequest",
    "BookingResult",
    "BookingStatus",
    "SearchCriteria",
    "UserCredentials",
    "Session",
    
    # Config
    "SNCFConfig",
    "default_config",
    "get_station_name",
    "STATIONS",
    
    # Clients
    "SNCFMaxClient",
    "TGVMaxAPI",
    
    # Scheduler
    "TGVMaxScheduler",
    "RecurringTrip",
    "OneTimeTrip", 
    "TimeWindow",
    "Weekday",
    "create_weekly_commute",
    
    # Scanner
    "ContinuousScanner",
    "ScanTarget",
    "ScanResult",
    "ScanMode",
    
    # Auth
    "SNCFAuthenticator",
    "AuthenticationError",
    "login_sync",
    "load_or_login",
    
    # Booking
    "SNCFBookingClient",
    "BookingError",
    "book_sync",
    "auto_book",
    
    # Voyages
    "MesVoyagesClient",
    "BookedTrip",
    "VoyagesStatus",
    "TripState",
    "MAX_TGVMAX_BOOKINGS",
    "fetch_my_trips_sync",
    "cancel_trip_sync",
    
    # Monitoring
    "TGVMaxMonitor",
    "WatchRequest",
    "AlertEvent",
    "watch_and_book",
    "quick_monitor",
    
    # Decomposition
    "TripDecomposer",
    "CompositeTrip",
    "TripLeg",
    "find_trip_with_decomposition",
    "INTERMEDIATE_STATIONS",
    
    # Deadline
    "DeadlineSearcher",
    "DeadlineBooker",
    "DeadlineConstraint",
    "DeadlineMatch",
    "DeadlineStrategy",
    "search_by_deadline",
    "find_best_for_deadline",
    "book_for_deadline",
    
    # Convenience
    "quick_search",
    "quick_book",
    
    # Browser debugging
    "BrowserDebugger",
    "SelectorTest",
    "PageState",
    "run_debug_session",
    "debug_sync",
]
