"""Aggressive continuous scanner for TGV Max availability.

TGV Max slots are:
- Released daily around 6 AM French time
- Available for booking 30 days in advance
- Very limited and get snapped up quickly

This scanner is optimized to:
- Scan aggressively during release windows (5:45 AM - 8:00 AM)
- Immediately attempt booking when slots are found
- Handle rate limiting and retries gracefully
- Run as a background daemon
"""

import asyncio
import time
import signal
import sys
from datetime import datetime, date, time as dt_time, timedelta
from typing import Optional, List, Dict, Callable, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging
import random

from models import Trip, BookingResult, BookingStatus
from config import SNCFConfig, default_config, get_station_name
from client import SNCFMaxClient
from api import TGVMaxAPI


logger = logging.getLogger(__name__)


class ScanMode(Enum):
    """Scanning intensity mode."""
    AGGRESSIVE = "aggressive"  # Fast scanning during release window
    NORMAL = "normal"          # Regular interval scanning
    IDLE = "idle"              # Minimal scanning (night hours)


@dataclass
class ScanTarget:
    """A target route and date to scan for."""
    origin: str
    destination: str
    trip_date: date
    time_min: Optional[dt_time] = None
    time_max: Optional[dt_time] = None
    schedule_id: Optional[str] = None  # Reference to parent schedule
    priority: int = 1
    
    def matches_trip(self, trip: Trip) -> bool:
        """Check if a trip matches this target."""
        if trip.departure_date != self.trip_date:
            return False
        
        if self.time_min and trip.departure_time < self.time_min:
            return False
        
        if self.time_max and trip.departure_time > self.time_max:
            return False
        
        return True
    
    @property
    def route_key(self) -> str:
        return f"{self.origin}|{self.destination}"


@dataclass
class ScanResult:
    """Result of a scan operation."""
    target: ScanTarget
    trips_found: List[Trip]
    booked: Optional[BookingResult] = None
    timestamp: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None


class ContinuousScanner:
    """Aggressive continuous scanner for TGV Max availability.
    
    Optimized for catching slots the moment they're released.
    
    Example:
        scanner = ContinuousScanner(email="...", password="...")
        
        # Add targets
        scanner.add_target("paris", "lyon", date(2025, 1, 15))
        scanner.add_target("lyon", "paris", date(2025, 1, 17))
        
        # Set up notifications
        scanner.on_found(lambda trip: send_notification(trip))
        
        # Run forever
        scanner.run()
    """
    
    # French timezone offset (CET = UTC+1, CEST = UTC+2)
    PARIS_TZ_OFFSET = 1  # TODO: Handle DST properly
    
    # Release window in French time
    RELEASE_WINDOW_START = dt_time(5, 45)
    RELEASE_WINDOW_END = dt_time(8, 0)
    
    # Scan intervals (seconds)
    AGGRESSIVE_INTERVAL = 10     # During release window
    NORMAL_INTERVAL = 120        # Regular hours
    IDLE_INTERVAL = 600          # Night hours (11 PM - 5 AM)
    
    def __init__(self,
                 email: Optional[str] = None,
                 password: Optional[str] = None,
                 config: Optional[SNCFConfig] = None,
                 auto_book: bool = True):
        """Initialize the scanner.
        
        Args:
            email: SNCF Connect email for booking
            password: SNCF Connect password
            config: Optional configuration
            auto_book: Automatically book when slots are found
        """
        self.config = config or default_config
        self.email = email or self.config.SNCF_EMAIL
        self.password = password or self.config.SNCF_PASSWORD
        self.auto_book = auto_book
        
        self._client = SNCFMaxClient(config=self.config)
        self._api: Optional[TGVMaxAPI] = None  # Lazy init for booking
        
        self._targets: Dict[str, ScanTarget] = {}
        self._seen: Set[str] = set()  # "train_no:date" - avoid re-processing
        self._booked: Set[str] = set()  # Confirmed bookings
        
        # Callbacks
        self._on_found: List[Callable[[Trip, ScanTarget], None]] = []
        self._on_booked: List[Callable[[BookingResult, ScanTarget], None]] = []
        self._on_scan: List[Callable[[ScanResult], None]] = []
        
        # State
        self._running = False
        self._authenticated = False
        self._last_scan: Dict[str, datetime] = {}  # route_key -> last scan time
        
        # Stats
        self._stats = {
            "scans": 0,
            "trips_found": 0,
            "bookings_attempted": 0,
            "bookings_success": 0,
            "errors": 0,
        }
    
    # ==========================================================================
    # TARGET MANAGEMENT
    # ==========================================================================
    
    def add_target(self,
                   origin: str,
                   destination: str,
                   trip_date: date,
                   time_min: Optional[str] = None,
                   time_max: Optional[str] = None,
                   priority: int = 1,
                   schedule_id: Optional[str] = None) -> str:
        """Add a scan target.
        
        Args:
            origin: Origin station
            destination: Destination station
            trip_date: Date to scan for
            time_min: Minimum departure time (HH:MM)
            time_max: Maximum departure time (HH:MM)
            priority: Priority (higher = scanned first)
            schedule_id: Optional reference to parent schedule
            
        Returns:
            Target ID
        """
        target = ScanTarget(
            origin=get_station_name(origin),
            destination=get_station_name(destination),
            trip_date=trip_date,
            time_min=datetime.strptime(time_min, "%H:%M").time() if time_min else None,
            time_max=datetime.strptime(time_max, "%H:%M").time() if time_max else None,
            priority=priority,
            schedule_id=schedule_id,
        )
        
        target_id = f"{target.route_key}:{trip_date}"
        self._targets[target_id] = target
        
        logger.info(f"Added target: {origin} → {destination} on {trip_date}")
        return target_id
    
    def remove_target(self, target_id: str) -> bool:
        """Remove a target by ID."""
        if target_id in self._targets:
            del self._targets[target_id]
            return True
        return False
    
    def clear_targets(self) -> None:
        """Remove all targets."""
        self._targets.clear()
    
    def add_targets_from_scheduler(self, scheduler: "TGVMaxScheduler") -> int:
        """Import targets from a TGVMaxScheduler.
        
        Returns:
            Number of targets added
        """
        from .scheduler import TGVMaxScheduler
        
        count = 0
        routes = scheduler._get_all_needed_dates()
        
        for route_key, dates in routes.items():
            origin, destination = route_key.split("|")
            for trip_date in dates:
                self.add_target(origin, destination, trip_date)
                count += 1
        
        return count
    
    # ==========================================================================
    # CALLBACKS
    # ==========================================================================
    
    def on_found(self, callback: Callable[[Trip, ScanTarget], None]) -> None:
        """Register callback when an available trip is found."""
        self._on_found.append(callback)
    
    def on_booked(self, callback: Callable[[BookingResult, ScanTarget], None]) -> None:
        """Register callback when booking is attempted."""
        self._on_booked.append(callback)
    
    def on_scan(self, callback: Callable[[ScanResult], None]) -> None:
        """Register callback after each scan."""
        self._on_scan.append(callback)
    
    def on_cycle_start(self, callback: Callable[[int, int], None]) -> None:
        """Register callback at start of each scan cycle (scan_number, total_targets)."""
        if not hasattr(self, '_on_cycle_start'):
            self._on_cycle_start = []
        self._on_cycle_start.append(callback)
    
    def on_target_scanned(self, callback: Callable[[ScanTarget, int, int], None]) -> None:
        """Register callback after each target (target, current, total)."""
        if not hasattr(self, '_on_target_scanned'):
            self._on_target_scanned = []
        self._on_target_scanned.append(callback)
    
    # ==========================================================================
    # SCANNING LOGIC
    # ==========================================================================
    
    def _get_mode(self) -> ScanMode:
        """Determine current scanning mode based on time."""
        now = datetime.now()
        current_time = now.time()
        
        # Night hours (23:00 - 05:00)
        if current_time >= dt_time(23, 0) or current_time < dt_time(5, 0):
            return ScanMode.IDLE
        
        # Release window (05:45 - 08:00)
        if self.RELEASE_WINDOW_START <= current_time <= self.RELEASE_WINDOW_END:
            return ScanMode.AGGRESSIVE
        
        return ScanMode.NORMAL
    
    def _get_interval(self) -> float:
        """Get current scan interval based on mode."""
        mode = self._get_mode()
        
        if mode == ScanMode.AGGRESSIVE:
            # Add small jitter to avoid detection
            return self.AGGRESSIVE_INTERVAL + random.uniform(0, 3)
        elif mode == ScanMode.NORMAL:
            return self.NORMAL_INTERVAL + random.uniform(0, 10)
        else:  # IDLE
            return self.IDLE_INTERVAL
    
    def _prioritize_targets(self) -> List[ScanTarget]:
        """Get targets sorted by priority and relevance."""
        now = datetime.now()
        today = date.today()
        
        def score(target: ScanTarget) -> Tuple[int, int, float]:
            # Higher score = scan first
            # 1. Priority
            priority_score = target.priority
            
            # 2. Days until trip (closer = higher priority)
            days_until = (target.trip_date - today).days
            if days_until <= 0:
                urgency_score = 0  # Past dates
            elif days_until == 30:
                urgency_score = 100  # Just released today!
            else:
                urgency_score = max(0, 30 - days_until)
            
            # 3. Time since last scan
            route_key = target.route_key
            if route_key in self._last_scan:
                seconds_since = (now - self._last_scan[route_key]).total_seconds()
            else:
                seconds_since = float('inf')
            
            return (priority_score, urgency_score, seconds_since)
        
        targets = [t for t in self._targets.values() if t.trip_date >= today]
        return sorted(targets, key=score, reverse=True)
    
    def _trip_key(self, trip: Trip) -> str:
        """Generate unique key for a trip."""
        return f"{trip.train_number}:{trip.departure_date}"
    
    async def _ensure_authenticated(self) -> bool:
        """Ensure we're authenticated for booking."""
        if self._authenticated:
            return True
        
        if not self.email or not self.password:
            logger.warning("No credentials configured - booking disabled")
            return False
        
        if not self._api:
            self._api = TGVMaxAPI(config=self.config)
        
        try:
            self._api.login(self.email, self.password)
            self._authenticated = True
            logger.info("Authenticated successfully")
            return True
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    async def scan_target(self, target: ScanTarget) -> ScanResult:
        """Scan a single target for availability."""
        self._stats["scans"] += 1
        
        try:
            trips = self._client.search_trips(
                origin=target.origin,
                destination=target.destination,
                trip_date=target.trip_date,
                only_available=True
            )
            
            # Filter by time constraints
            matching = [t for t in trips if target.matches_trip(t)]
            
            # Filter out already seen/booked
            new_trips = []
            for trip in matching:
                key = self._trip_key(trip)
                if key not in self._seen and key not in self._booked:
                    new_trips.append(trip)
                    self._seen.add(key)
            
            self._stats["trips_found"] += len(new_trips)
            
            result = ScanResult(target=target, trips_found=new_trips)
            
            # Process new trips
            for trip in new_trips:
                logger.info(f"🚄 Found: {trip}")
                
                # Notify callbacks
                for callback in self._on_found:
                    try:
                        callback(trip, target)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
                
                # Attempt booking
                if self.auto_book:
                    await self._attempt_booking(trip, target, result)
            
            self._last_scan[target.route_key] = datetime.now()
            return result
            
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Scan error for {target.origin} → {target.destination}: {e}")
            return ScanResult(target=target, trips_found=[], error=str(e))
    
    async def _attempt_booking(self, trip: Trip, target: ScanTarget, result: ScanResult) -> None:
        """Attempt to book a trip."""
        self._stats["bookings_attempted"] += 1
        
        if not await self._ensure_authenticated():
            return
        
        try:
            booking_result = self._api.book(trip)
            result.booked = booking_result
            
            if booking_result.is_success:
                self._stats["bookings_success"] += 1
                self._booked.add(self._trip_key(trip))
                logger.info(f"✅ BOOKED: {trip} - {booking_result.confirmation_number}")
            else:
                logger.warning(f"Booking failed: {booking_result.message}")
            
            # Notify
            for callback in self._on_booked:
                try:
                    callback(booking_result, target)
                except Exception as e:
                    logger.error(f"Booking callback error: {e}")
                    
        except Exception as e:
            logger.error(f"Booking error: {e}")
    
    async def scan_all(self) -> List[ScanResult]:
        """Scan all targets once."""
        if not hasattr(self, '_scan_count'):
            self._scan_count = 0
        self._scan_count += 1
        
        targets = self._prioritize_targets()
        results = []
        
        if not targets:
            return results
        
        mode = self._get_mode()
        logger.info(f"Scanning {len(targets)} targets (mode: {mode.value})")
        
        # Notify cycle start
        if hasattr(self, '_on_cycle_start'):
            for callback in self._on_cycle_start:
                try:
                    callback(self._scan_count, len(targets))
                except Exception:
                    pass
        
        for i, target in enumerate(targets, 1):
            result = await self.scan_target(target)
            results.append(result)
            
            # Notify target scanned
            if hasattr(self, '_on_target_scanned'):
                for callback in self._on_target_scanned:
                    try:
                        callback(target, i, len(targets))
                    except Exception:
                        pass
            
            # Notify scan result
            for callback in self._on_scan:
                try:
                    callback(result)
                except Exception:
                    pass
            
            # Rate limiting delay
            if mode == ScanMode.AGGRESSIVE:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(1)
        
        return results
    
    def scan_now(self) -> List[ScanResult]:
        """Synchronous scan."""
        return asyncio.run(self.scan_all())
    
    # ==========================================================================
    # MAIN LOOP
    # ==========================================================================
    
    async def run_async(self) -> None:
        """Run the scanner continuously."""
        self._running = True
        
        logger.info("=" * 50)
        logger.info("🔍 TGV Max Continuous Scanner Started")
        logger.info(f"   Targets: {len(self._targets)}")
        logger.info(f"   Auto-book: {self.auto_book}")
        logger.info(f"   Release window: {self.RELEASE_WINDOW_START} - {self.RELEASE_WINDOW_END}")
        logger.info("=" * 50)
        
        while self._running:
            try:
                results = await self.scan_all()
                
                # Log summary
                found = sum(len(r.trips_found) for r in results)
                booked = sum(1 for r in results if r.booked and r.booked.is_success)
                if found > 0 or booked > 0:
                    logger.info(f"Scan complete: {found} trips found, {booked} booked")
                
            except Exception as e:
                logger.error(f"Scan cycle error: {e}")
            
            # Wait for next cycle
            interval = self._get_interval()
            mode = self._get_mode()
            logger.debug(f"Next scan in {interval:.0f}s (mode: {mode.value})")
            await asyncio.sleep(interval)
        
        logger.info("Scanner stopped")
    
    def run(self) -> None:
        """Run the scanner (blocking).
        
        Handles SIGINT/SIGTERM for graceful shutdown.
        """
        def handle_signal(signum, frame):
            logger.info("Shutdown signal received")
            self.stop()
        
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        
        asyncio.run(self.run_async())
    
    def stop(self) -> None:
        """Stop the scanner."""
        self._running = False
    
    # ==========================================================================
    # STATUS & STATS
    # ==========================================================================
    
    def status(self) -> dict:
        """Get scanner status."""
        return {
            "running": self._running,
            "mode": self._get_mode().value,
            "targets": len(self._targets),
            "authenticated": self._authenticated,
            "next_interval": self._get_interval(),
            "stats": self._stats.copy(),
        }
    
    def print_status(self) -> None:
        """Print formatted status."""
        s = self.status()
        print(f"""
🔍 TGV Max Scanner Status
{'='*40}
Running:        {s['running']}
Mode:           {s['mode']}
Targets:        {s['targets']}
Authenticated:  {s['authenticated']}
Next scan in:   {s['next_interval']:.0f}s

📊 Stats:
  Scans:        {s['stats']['scans']}
  Trips found:  {s['stats']['trips_found']}
  Bookings:     {s['stats']['bookings_success']}/{s['stats']['bookings_attempted']}
  Errors:       {s['stats']['errors']}
""")


# ==========================================================================
# DAEMON MODE
# ==========================================================================

def run_daemon(scheduler_config_path: Optional[str] = None,
               email: Optional[str] = None,
               password: Optional[str] = None) -> None:
    """Run scanner as a daemon.
    
    Args:
        scheduler_config_path: Path to scheduler config file to load targets from
        email: SNCF Connect email
        password: SNCF Connect password
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    scanner = ContinuousScanner(email=email, password=password)
    
    if scheduler_config_path:
        from .scheduler import TGVMaxScheduler
        sched = TGVMaxScheduler(email=email, password=password)
        sched._load_config()
        count = scanner.add_targets_from_scheduler(sched)
        logger.info(f"Loaded {count} targets from scheduler")
    
    scanner.run()

