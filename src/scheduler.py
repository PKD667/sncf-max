"""Calendar-based scheduling system for automated TGV Max booking.

This module provides:
- Recurring trip schedules (weekly commutes, etc.)
- Calendar integration
- Automatic booking when slots open 30 days ahead
- Persistent configuration
"""

import json
import asyncio
from datetime import date, datetime, time, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Callable, Set
from enum import Enum
from pathlib import Path
import logging
import hashlib

from models import Trip, BookingResult, BookingStatus, UserCredentials
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from api import TGVMaxAPI


logger = logging.getLogger(__name__)


class Weekday(Enum):
    """Days of the week."""
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


@dataclass
class TimeWindow:
    """A time window for acceptable departure times."""
    start: time
    end: time
    
    def contains(self, t: time) -> bool:
        """Check if a time falls within this window."""
        return self.start <= t <= self.end
    
    def to_dict(self) -> dict:
        return {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "TimeWindow":
        return cls(
            start=datetime.strptime(data["start"], "%H:%M").time(),
            end=datetime.strptime(data["end"], "%H:%M").time(),
        )
    
    @classmethod
    def morning(cls) -> "TimeWindow":
        """6:00 - 10:00"""
        return cls(time(6, 0), time(10, 0))
    
    @classmethod
    def midday(cls) -> "TimeWindow":
        """10:00 - 14:00"""
        return cls(time(10, 0), time(14, 0))
    
    @classmethod
    def afternoon(cls) -> "TimeWindow":
        """14:00 - 18:00"""
        return cls(time(14, 0), time(18, 0))
    
    @classmethod
    def evening(cls) -> "TimeWindow":
        """18:00 - 23:00"""
        return cls(time(18, 0), time(23, 0))
    
    @classmethod
    def all_day(cls) -> "TimeWindow":
        """5:00 - 23:59"""
        return cls(time(5, 0), time(23, 59))


@dataclass
class RecurringTrip:
    """A recurring trip schedule.
    
    Example:
        # Every Friday evening Paris -> Lyon
        trip = RecurringTrip(
            name="Weekend home",
            origin="paris",
            destination="lyon",
            weekdays=[Weekday.FRIDAY],
            time_windows=[TimeWindow.evening()],
            priority=1,
        )
    """
    name: str
    origin: str
    destination: str
    weekdays: List[Weekday]
    time_windows: List[TimeWindow] = field(default_factory=lambda: [TimeWindow.all_day()])
    priority: int = 1  # Higher = more important (try to book first)
    enabled: bool = True
    
    # Internal tracking
    id: str = field(default="")
    created_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        if not self.id:
            # Generate stable ID from route and schedule
            content = f"{self.origin}_{self.destination}_{sorted([w.value for w in self.weekdays])}"
            self.id = hashlib.md5(content.encode()).hexdigest()[:8]
    
    def get_upcoming_dates(self, days_ahead: int = 30) -> List[date]:
        """Get all upcoming dates that match this schedule."""
        dates = []
        today = date.today()
        
        for i in range(days_ahead):
            d = today + timedelta(days=i)
            if Weekday(d.weekday()) in self.weekdays:
                dates.append(d)
        
        return dates
    
    def matches_trip(self, trip: Trip) -> bool:
        """Check if a trip matches this recurring schedule."""
        # Check weekday
        if Weekday(trip.departure_date.weekday()) not in self.weekdays:
            return False
        
        # Check time window
        for window in self.time_windows:
            if window.contains(trip.departure_time):
                return True
        
        return False
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "origin": self.origin,
            "destination": self.destination,
            "weekdays": [w.value for w in self.weekdays],
            "time_windows": [tw.to_dict() for tw in self.time_windows],
            "priority": self.priority,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "RecurringTrip":
        return cls(
            id=data.get("id", ""),
            name=data["name"],
            origin=data["origin"],
            destination=data["destination"],
            weekdays=[Weekday(w) for w in data["weekdays"]],
            time_windows=[TimeWindow.from_dict(tw) for tw in data.get("time_windows", [])],
            priority=data.get("priority", 1),
            enabled=data.get("enabled", True),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
        )


@dataclass 
class OneTimeTrip:
    """A one-time trip request.
    
    Use for specific trips that aren't recurring.
    """
    origin: str
    destination: str
    trip_date: date
    time_windows: List[TimeWindow] = field(default_factory=lambda: [TimeWindow.all_day()])
    priority: int = 1
    booked: bool = False
    
    id: str = field(default="")
    
    def __post_init__(self):
        if not self.id:
            content = f"{self.origin}_{self.destination}_{self.trip_date}"
            self.id = hashlib.md5(content.encode()).hexdigest()[:8]
    
    def matches_trip(self, trip: Trip) -> bool:
        """Check if a trip matches this request."""
        if trip.departure_date != self.trip_date:
            return False
        
        for window in self.time_windows:
            if window.contains(trip.departure_time):
                return True
        
        return False
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "origin": self.origin,
            "destination": self.destination,
            "trip_date": self.trip_date.isoformat(),
            "time_windows": [tw.to_dict() for tw in self.time_windows],
            "priority": self.priority,
            "booked": self.booked,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "OneTimeTrip":
        return cls(
            id=data.get("id", ""),
            origin=data["origin"],
            destination=data["destination"],
            trip_date=date.fromisoformat(data["trip_date"]),
            time_windows=[TimeWindow.from_dict(tw) for tw in data.get("time_windows", [])],
            priority=data.get("priority", 1),
            booked=data.get("booked", False),
        )


@dataclass
class BookingRecord:
    """Record of a successful booking."""
    trip: Trip
    schedule_id: str  # ID of RecurringTrip or OneTimeTrip
    confirmation: Optional[str]
    booked_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "train_number": self.trip.train_number,
            "origin": str(self.trip.origin),
            "destination": str(self.trip.destination),
            "departure_date": self.trip.departure_date.isoformat(),
            "departure_time": self.trip.departure_time.strftime("%H:%M"),
            "confirmation": self.confirmation,
            "booked_at": self.booked_at.isoformat(),
        }


class TGVMaxScheduler:
    """Calendar-based scheduler for automated TGV Max booking.
    
    This is the core of the automation system. It:
    1. Manages recurring and one-time trip schedules
    2. Continuously scans for availability
    3. Automatically books when slots open
    4. Persists configuration across restarts
    
    Example:
        scheduler = TGVMaxScheduler(
            email="your@email.com",
            password="yourpassword"
        )
        
        # Add a weekly commute
        scheduler.add_recurring(
            name="Friday evening home",
            origin="paris",
            destination="lyon",
            weekdays=[Weekday.FRIDAY],
            time_windows=[TimeWindow.evening()],
        )
        
        # Add return trip
        scheduler.add_recurring(
            name="Sunday evening return",
            origin="lyon",
            destination="paris", 
            weekdays=[Weekday.SUNDAY],
            time_windows=[TimeWindow.evening()],
        )
        
        # Start the scanner
        scheduler.run()  # Runs forever, scanning and booking
    """
    
    DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sncf-max" / "scheduler.json"
    
    def __init__(self,
                 email: Optional[str] = None,
                 password: Optional[str] = None,
                 config: Optional[SNCFConfig] = None,
                 config_path: Optional[Path] = None):
        """Initialize the scheduler.
        
        Args:
            email: SNCF Connect email
            password: SNCF Connect password
            config: Optional SNCFConfig
            config_path: Path to persist configuration (default: ~/.config/sncf-max/scheduler.json)
        """
        self.config = config or default_config
        self.email = email or self.config.SNCF_EMAIL
        self.password = password or self.config.SNCF_PASSWORD
        self.config_path = config_path or self.DEFAULT_CONFIG_PATH
        
        self._api = TGVMaxAPI(config=self.config)
        self._client = SNCFMaxClient(config=self.config)
        
        # Schedules
        self._recurring: Dict[str, RecurringTrip] = {}
        self._one_time: Dict[str, OneTimeTrip] = {}
        self._bookings: List[BookingRecord] = []
        
        # Track what we've already tried to book (to avoid spam)
        self._attempted: Set[str] = set()  # "schedule_id:date:train_no"
        
        # Callbacks
        self._on_booking: List[Callable[[BookingRecord], None]] = []
        self._on_availability: List[Callable[[Trip, str], None]] = []  # trip, schedule_id
        
        # State
        self._running = False
        self._authenticated = False
        
        # Load saved config
        self._load_config()
    
    # ==========================================================================
    # SCHEDULE MANAGEMENT
    # ==========================================================================
    
    def add_recurring(self,
                      name: str,
                      origin: str,
                      destination: str,
                      weekdays: List[Weekday],
                      time_windows: Optional[List[TimeWindow]] = None,
                      priority: int = 1) -> str:
        """Add a recurring trip schedule.
        
        Args:
            name: Friendly name for this schedule
            origin: Origin station
            destination: Destination station
            weekdays: List of weekdays to travel
            time_windows: Acceptable departure time windows
            priority: Booking priority (higher = more important)
            
        Returns:
            Schedule ID
        """
        schedule = RecurringTrip(
            name=name,
            origin=get_station_name(origin),
            destination=get_station_name(destination),
            weekdays=weekdays,
            time_windows=time_windows or [TimeWindow.all_day()],
            priority=priority,
        )
        
        self._recurring[schedule.id] = schedule
        self._save_config()
        
        logger.info(f"Added recurring schedule: {name} ({schedule.id})")
        return schedule.id
    
    def add_one_time(self,
                     origin: str,
                     destination: str,
                     trip_date: date,
                     time_windows: Optional[List[TimeWindow]] = None,
                     priority: int = 1) -> str:
        """Add a one-time trip request.
        
        Args:
            origin: Origin station
            destination: Destination station
            trip_date: Date of travel
            time_windows: Acceptable departure times
            priority: Booking priority
            
        Returns:
            Schedule ID
        """
        schedule = OneTimeTrip(
            origin=get_station_name(origin),
            destination=get_station_name(destination),
            trip_date=trip_date,
            time_windows=time_windows or [TimeWindow.all_day()],
            priority=priority,
        )
        
        self._one_time[schedule.id] = schedule
        self._save_config()
        
        logger.info(f"Added one-time trip: {origin} → {destination} on {trip_date} ({schedule.id})")
        return schedule.id
    
    def remove_schedule(self, schedule_id: str) -> bool:
        """Remove a schedule by ID."""
        if schedule_id in self._recurring:
            del self._recurring[schedule_id]
            self._save_config()
            return True
        if schedule_id in self._one_time:
            del self._one_time[schedule_id]
            self._save_config()
            return True
        return False
    
    def list_schedules(self) -> Dict[str, List]:
        """List all schedules."""
        return {
            "recurring": list(self._recurring.values()),
            "one_time": list(self._one_time.values()),
        }
    
    def list_bookings(self) -> List[BookingRecord]:
        """List all successful bookings."""
        return self._bookings.copy()
    
    # ==========================================================================
    # CALLBACKS
    # ==========================================================================
    
    def on_booking(self, callback: Callable[[BookingRecord], None]) -> None:
        """Register callback for successful bookings."""
        self._on_booking.append(callback)
    
    def on_availability(self, callback: Callable[[Trip, str], None]) -> None:
        """Register callback when availability is found (before booking attempt)."""
        self._on_availability.append(callback)
    
    # ==========================================================================
    # PERSISTENCE
    # ==========================================================================
    
    def _save_config(self) -> None:
        """Save configuration to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "recurring": [s.to_dict() for s in self._recurring.values()],
            "one_time": [s.to_dict() for s in self._one_time.values()],
            "bookings": [b.to_dict() for b in self._bookings[-100:]],  # Keep last 100
            "attempted": list(self._attempted)[-1000:],  # Keep last 1000
        }
        
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)
    
    def _load_config(self) -> None:
        """Load configuration from disk."""
        if not self.config_path.exists():
            return
        
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            
            for item in data.get("recurring", []):
                schedule = RecurringTrip.from_dict(item)
                self._recurring[schedule.id] = schedule
            
            for item in data.get("one_time", []):
                schedule = OneTimeTrip.from_dict(item)
                # Only load if not in the past
                if schedule.trip_date >= date.today():
                    self._one_time[schedule.id] = schedule
            
            self._attempted = set(data.get("attempted", []))
            
            logger.info(f"Loaded {len(self._recurring)} recurring, {len(self._one_time)} one-time schedules")
        
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    
    # ==========================================================================
    # SCANNING & BOOKING
    # ==========================================================================
    
    def _get_all_needed_dates(self) -> Dict[str, List[date]]:
        """Get all dates we need to scan, grouped by route.
        
        Returns:
            Dict mapping "origin|destination" to list of dates
        """
        routes: Dict[str, Set[date]] = {}
        
        # From recurring schedules
        for schedule in self._recurring.values():
            if not schedule.enabled:
                continue
            
            key = f"{schedule.origin}|{schedule.destination}"
            if key not in routes:
                routes[key] = set()
            
            routes[key].update(schedule.get_upcoming_dates(days_ahead=30))
        
        # From one-time trips
        for schedule in self._one_time.values():
            if schedule.booked:
                continue
            
            key = f"{schedule.origin}|{schedule.destination}"
            if key not in routes:
                routes[key] = set()
            
            # Only if in the future and within 30 days
            days_until = (schedule.trip_date - date.today()).days
            if 0 <= days_until <= 30:
                routes[key].add(schedule.trip_date)
        
        return {k: sorted(v) for k, v in routes.items()}
    
    def _get_matching_schedules(self, trip: Trip) -> List[tuple]:
        """Find all schedules that match a given trip.
        
        Returns:
            List of (schedule, schedule_id) tuples, sorted by priority
        """
        matches = []
        
        origin = str(trip.origin)
        destination = str(trip.destination)
        
        # Check recurring
        for schedule in self._recurring.values():
            if not schedule.enabled:
                continue
            if schedule.origin != origin or schedule.destination != destination:
                continue
            if schedule.matches_trip(trip):
                matches.append((schedule, schedule.id))
        
        # Check one-time
        for schedule in self._one_time.values():
            if schedule.booked:
                continue
            if schedule.origin != origin or schedule.destination != destination:
                continue
            if schedule.matches_trip(trip):
                matches.append((schedule, schedule.id))
        
        # Sort by priority (higher first)
        matches.sort(key=lambda x: x[0].priority, reverse=True)
        return matches
    
    def _attempt_key(self, schedule_id: str, trip: Trip) -> str:
        """Generate key for tracking booking attempts."""
        return f"{schedule_id}:{trip.departure_date}:{trip.train_number}"
    
    def _is_already_booked(self, trip: Trip) -> bool:
        """Check if we already have a booking for this trip."""
        for record in self._bookings:
            if (record.trip.train_number == trip.train_number and
                record.trip.departure_date == trip.departure_date):
                return True
        return False
    
    async def _ensure_authenticated(self) -> bool:
        """Ensure we're logged in."""
        if self._authenticated and self._api.is_authenticated:
            return True
        
        if not self.email or not self.password:
            logger.error("No credentials configured")
            return False
        
        try:
            self._api.login(self.email, self.password)
            self._authenticated = True
            logger.info("Successfully authenticated")
            return True
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    async def scan_once(self) -> List[BookingRecord]:
        """Scan for availability once and attempt bookings.
        
        Returns:
            List of successful bookings
        """
        new_bookings = []
        routes = self._get_all_needed_dates()
        
        if not routes:
            logger.debug("No routes to scan")
            return new_bookings
        
        logger.info(f"Scanning {len(routes)} routes...")
        
        for route_key, dates in routes.items():
            origin, destination = route_key.split("|")
            
            for trip_date in dates:
                try:
                    trips = self._client.search_trips(
                        origin=origin,
                        destination=destination,
                        trip_date=trip_date,
                        only_available=True
                    )
                    
                    for trip in trips:
                        # Skip if already booked
                        if self._is_already_booked(trip):
                            continue
                        
                        # Find matching schedules
                        matches = self._get_matching_schedules(trip)
                        
                        for schedule, schedule_id in matches:
                            attempt_key = self._attempt_key(schedule_id, trip)
                            
                            # Skip if recently attempted
                            if attempt_key in self._attempted:
                                continue
                            
                            # Notify availability
                            for callback in self._on_availability:
                                try:
                                    callback(trip, schedule_id)
                                except Exception:
                                    pass
                            
                            # Attempt booking
                            logger.info(f"Found availability: {trip}")
                            
                            if not await self._ensure_authenticated():
                                continue
                            
                            result = self._api.book(trip)
                            self._attempted.add(attempt_key)
                            
                            if result.is_success:
                                record = BookingRecord(
                                    trip=trip,
                                    schedule_id=schedule_id,
                                    confirmation=result.confirmation_number,
                                )
                                self._bookings.append(record)
                                new_bookings.append(record)
                                
                                # Mark one-time as booked
                                if schedule_id in self._one_time:
                                    self._one_time[schedule_id].booked = True
                                
                                logger.info(f"✅ BOOKED: {trip}")
                                
                                # Notify
                                for callback in self._on_booking:
                                    try:
                                        callback(record)
                                    except Exception:
                                        pass
                                
                                # Only book one per schedule/date
                                break
                            else:
                                logger.warning(f"Booking failed: {result.message}")
                
                except Exception as e:
                    logger.error(f"Error scanning {origin} → {destination} on {trip_date}: {e}")
                
                # Small delay between date queries
                await asyncio.sleep(0.5)
            
            # Delay between routes
            await asyncio.sleep(1)
        
        self._save_config()
        return new_bookings
    
    def scan_now(self) -> List[BookingRecord]:
        """Synchronous scan."""
        return asyncio.run(self.scan_once())
    
    async def run_async(self, 
                        interval: int = 300,
                        aggressive_hours: Optional[List[int]] = None) -> None:
        """Run the scheduler continuously.
        
        Args:
            interval: Seconds between scans (default 5 minutes)
            aggressive_hours: Hours to scan more frequently (e.g., [6, 7, 8] for 6-9 AM)
                             During these hours, interval is reduced to 60 seconds.
        """
        aggressive_hours = aggressive_hours or [6, 7, 8]  # TGV Max releases at ~6 AM
        
        self._running = True
        logger.info(f"Scheduler started. Scanning every {interval}s (aggressive: {aggressive_hours})")
        
        while self._running:
            try:
                bookings = await self.scan_once()
                
                if bookings:
                    logger.info(f"Made {len(bookings)} new bookings!")
                
            except Exception as e:
                logger.error(f"Scan error: {e}")
            
            # Determine sleep time
            current_hour = datetime.now().hour
            if current_hour in aggressive_hours:
                sleep_time = min(60, interval)  # More aggressive during release hours
            else:
                sleep_time = interval
            
            await asyncio.sleep(sleep_time)
        
        logger.info("Scheduler stopped")
    
    def run(self, interval: int = 300) -> None:
        """Run the scheduler (blocking)."""
        asyncio.run(self.run_async(interval))
    
    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
    
    # ==========================================================================
    # STATUS & INFO
    # ==========================================================================
    
    def status(self) -> dict:
        """Get scheduler status."""
        return {
            "running": self._running,
            "authenticated": self._authenticated,
            "recurring_schedules": len(self._recurring),
            "one_time_schedules": len(self._one_time),
            "total_bookings": len(self._bookings),
            "pending_dates": sum(len(dates) for dates in self._get_all_needed_dates().values()),
        }
    
    def upcoming(self, days: int = 7) -> List[dict]:
        """Get upcoming trips we're watching for."""
        upcoming = []
        cutoff = date.today() + timedelta(days=days)
        
        for schedule in self._recurring.values():
            if not schedule.enabled:
                continue
            for d in schedule.get_upcoming_dates(days):
                if d <= cutoff:
                    upcoming.append({
                        "date": d,
                        "schedule": schedule.name,
                        "origin": schedule.origin,
                        "destination": schedule.destination,
                        "windows": [f"{tw.start}-{tw.end}" for tw in schedule.time_windows],
                    })
        
        for schedule in self._one_time.values():
            if not schedule.booked and schedule.trip_date <= cutoff:
                upcoming.append({
                    "date": schedule.trip_date,
                    "schedule": f"One-time: {schedule.origin} → {schedule.destination}",
                    "origin": schedule.origin,
                    "destination": schedule.destination,
                    "windows": [f"{tw.start}-{tw.end}" for tw in schedule.time_windows],
                })
        
        upcoming.sort(key=lambda x: x["date"])
        return upcoming


# ==========================================================================
# CONVENIENCE FUNCTIONS
# ==========================================================================

def create_weekly_commute(
    origin: str,
    destination: str,
    outbound_days: List[Weekday],
    return_days: List[Weekday],
    outbound_times: Optional[List[TimeWindow]] = None,
    return_times: Optional[List[TimeWindow]] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> TGVMaxScheduler:
    """Create a scheduler for a typical weekly commute pattern.
    
    Args:
        origin: Home city
        destination: Work city
        outbound_days: Days to travel to work (e.g., [Weekday.MONDAY])
        return_days: Days to travel home (e.g., [Weekday.FRIDAY])
        outbound_times: Preferred outbound times
        return_times: Preferred return times
        email: SNCF Connect email
        password: SNCF Connect password
        
    Returns:
        Configured scheduler (call .run() to start)
    """
    scheduler = TGVMaxScheduler(email=email, password=password)
    
    scheduler.add_recurring(
        name=f"Outbound: {origin} → {destination}",
        origin=origin,
        destination=destination,
        weekdays=outbound_days,
        time_windows=outbound_times or [TimeWindow.morning()],
        priority=2,
    )
    
    scheduler.add_recurring(
        name=f"Return: {destination} → {origin}",
        origin=destination,
        destination=origin,
        weekdays=return_days,
        time_windows=return_times or [TimeWindow.evening()],
        priority=2,
    )
    
    return scheduler

