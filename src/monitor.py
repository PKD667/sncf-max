"""Availability monitoring and notification system for TGV Max trips."""

import asyncio
import time
from typing import Optional, List, Callable, Any
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

from models import Trip, BookingResult, BookingStatus
from config import SNCFConfig, default_config
from client import SNCFMaxClient
from api import TGVMaxAPI


logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Type of notification."""
    CONSOLE = "console"
    EMAIL = "email"
    WEBHOOK = "webhook"
    CALLBACK = "callback"


@dataclass
class WatchRequest:
    """Request to watch for trip availability."""
    origin: str
    destination: str
    dates: List[date]
    preferred_times: Optional[List[str]] = None  # List of "HH:MM" strings
    auto_book: bool = False
    watch_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d%H%M%S"))
    
    def matches(self, trip: Trip) -> bool:
        """Check if a trip matches this watch request."""
        # Check date
        if trip.departure_date not in self.dates:
            return False
        
        # Check time preference if specified
        if self.preferred_times:
            trip_time = trip.departure_time.strftime("%H:%M")
            # Allow 30 min window around preferred times
            for pref_time in self.preferred_times:
                pref_dt = datetime.strptime(pref_time, "%H:%M")
                trip_dt = datetime.combine(date.today(), trip.departure_time)
                pref_dt = datetime.combine(date.today(), pref_dt.time())
                if abs((trip_dt - pref_dt).total_seconds()) <= 1800:  # 30 min
                    return True
            return False
        
        return True


@dataclass
class AlertEvent:
    """An availability alert event."""
    watch: WatchRequest
    trips: List[Trip]
    timestamp: datetime = field(default_factory=datetime.now)
    booked: Optional[BookingResult] = None


class TGVMaxMonitor:
    """Monitor for TGV Max trip availability.
    
    Watches for availability and can auto-book or send notifications.
    
    Example:
        monitor = TGVMaxMonitor()
        
        # Add a watch
        monitor.watch(
            origin="paris",
            destination="lyon",
            dates=[date(2025, 1, 15), date(2025, 1, 16)],
            auto_book=True
        )
        
        # Set up notifications
        monitor.on_available(lambda event: print(f"Found {len(event.trips)} trips!"))
        
        # Start monitoring
        monitor.start(interval=300)  # Check every 5 minutes
    """
    
    def __init__(self, 
                 config: Optional[SNCFConfig] = None,
                 email: Optional[str] = None,
                 password: Optional[str] = None):
        """Initialize the monitor.
        
        Args:
            config: Optional configuration
            email: SNCF Connect email for auto-booking
            password: SNCF Connect password for auto-booking
        """
        self.config = config or default_config
        self.email = email or self.config.SNCF_EMAIL
        self.password = password or self.config.SNCF_PASSWORD
        
        self._api = TGVMaxAPI(config=self.config)
        self._watches: List[WatchRequest] = []
        self._callbacks: List[Callable[[AlertEvent], None]] = []
        self._running = False
        self._seen_trips: set = set()  # Track already-seen trips
        
        # Email configuration
        self._smtp_host: Optional[str] = None
        self._smtp_port: int = 587
        self._smtp_user: Optional[str] = None
        self._smtp_password: Optional[str] = None
        self._notify_email: Optional[str] = None
        
        # Webhook configuration
        self._webhook_url: Optional[str] = None
    
    def watch(self,
              origin: str,
              destination: str,
              dates: List[date],
              preferred_times: Optional[List[str]] = None,
              auto_book: bool = False) -> str:
        """Add a watch for trip availability.
        
        Args:
            origin: Origin station
            destination: Destination station
            dates: List of dates to watch
            preferred_times: Optional list of preferred departure times ("HH:MM")
            auto_book: Automatically book when availability found
            
        Returns:
            Watch ID for reference
        """
        watch = WatchRequest(
            origin=origin,
            destination=destination,
            dates=dates,
            preferred_times=preferred_times,
            auto_book=auto_book
        )
        self._watches.append(watch)
        logger.info(f"Added watch {watch.watch_id}: {origin} -> {destination}")
        return watch.watch_id
    
    def unwatch(self, watch_id: str) -> bool:
        """Remove a watch by ID.
        
        Returns:
            True if watch was found and removed
        """
        for i, watch in enumerate(self._watches):
            if watch.watch_id == watch_id:
                del self._watches[i]
                return True
        return False
    
    def on_available(self, callback: Callable[[AlertEvent], None]) -> None:
        """Register a callback for when availability is found.
        
        Args:
            callback: Function to call with AlertEvent
        """
        self._callbacks.append(callback)
    
    def configure_email(self,
                        smtp_host: str,
                        smtp_user: str,
                        smtp_password: str,
                        notify_email: str,
                        smtp_port: int = 587) -> None:
        """Configure email notifications.
        
        Args:
            smtp_host: SMTP server hostname
            smtp_user: SMTP username
            smtp_password: SMTP password
            notify_email: Email address to send notifications to
            smtp_port: SMTP port (default 587)
        """
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._notify_email = notify_email
    
    def configure_webhook(self, url: str) -> None:
        """Configure webhook notifications.
        
        Args:
            url: Webhook URL to POST to
        """
        self._webhook_url = url
    
    def _trip_key(self, trip: Trip) -> str:
        """Generate unique key for a trip."""
        return f"{trip.train_number}_{trip.departure_date}_{trip.departure_time}"
    
    async def _check_once(self) -> List[AlertEvent]:
        """Check all watches once and return events."""
        events = []
        
        for watch in self._watches:
            try:
                # Search for available trips
                for trip_date in watch.dates:
                    trips = self._api.search(
                        origin=watch.origin,
                        destination=watch.destination,
                        trip_date=trip_date,
                        only_available=True
                    )
                    
                    # Filter to matching trips we haven't seen
                    new_trips = []
                    for trip in trips:
                        key = self._trip_key(trip)
                        if key not in self._seen_trips and watch.matches(trip):
                            new_trips.append(trip)
                            self._seen_trips.add(key)
                    
                    if new_trips:
                        event = AlertEvent(watch=watch, trips=new_trips)
                        
                        # Auto-book if enabled
                        if watch.auto_book and self.email and self.password:
                            if not self._api.is_authenticated:
                                try:
                                    self._api.login(self.email, self.password)
                                except Exception as e:
                                    logger.error(f"Auto-book auth failed: {e}")
                            
                            if self._api.is_authenticated:
                                # Book the first matching trip
                                result = self._api.book(new_trips[0])
                                event.booked = result
                                logger.info(f"Auto-booked: {result}")
                        
                        events.append(event)
            
            except Exception as e:
                logger.error(f"Error checking watch {watch.watch_id}: {e}")
        
        return events
    
    def _notify(self, event: AlertEvent) -> None:
        """Send notifications for an event."""
        # Console logging
        logger.info(f"🚄 Found {len(event.trips)} TGV Max trips!")
        for trip in event.trips:
            logger.info(f"  - {trip}")
        if event.booked:
            logger.info(f"  Booked: {event.booked}")
        
        # Custom callbacks
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Callback error: {e}")
        
        # Email notification
        if self._smtp_host and self._notify_email:
            self._send_email(event)
        
        # Webhook notification
        if self._webhook_url:
            self._send_webhook(event)
    
    def _send_email(self, event: AlertEvent) -> None:
        """Send email notification."""
        try:
            msg = MIMEMultipart()
            msg['From'] = self._smtp_user
            msg['To'] = self._notify_email
            msg['Subject'] = f"🚄 TGV Max: {len(event.trips)} trips available!"
            
            body = f"""
TGV Max Availability Alert!

Route: {event.watch.origin} → {event.watch.destination}
Found {len(event.trips)} available trips:

"""
            for trip in event.trips:
                body += f"• Train {trip.train_number}: {trip.departure_time} - {trip.arrival_time} on {trip.departure_date}\n"
            
            if event.booked:
                body += f"\n✅ Auto-booked: {event.booked.confirmation_number or 'Confirmed'}\n"
            
            msg.attach(MIMEText(body, 'plain'))
            
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.starttls()
                server.login(self._smtp_user, self._smtp_password)
                server.send_message(msg)
            
            logger.info(f"Email notification sent to {self._notify_email}")
        
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
    
    def _send_webhook(self, event: AlertEvent) -> None:
        """Send webhook notification."""
        import requests
        
        try:
            payload = {
                "type": "tgvmax_availability",
                "timestamp": event.timestamp.isoformat(),
                "watch": {
                    "id": event.watch.watch_id,
                    "origin": event.watch.origin,
                    "destination": event.watch.destination,
                },
                "trips": [
                    {
                        "train_number": t.train_number,
                        "departure_date": t.departure_date.isoformat(),
                        "departure_time": t.departure_time.strftime("%H:%M"),
                        "arrival_time": t.arrival_time.strftime("%H:%M"),
                    }
                    for t in event.trips
                ],
            }
            
            if event.booked:
                payload["booked"] = {
                    "status": event.booked.status.value,
                    "confirmation": event.booked.confirmation_number,
                }
            
            response = requests.post(
                self._webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Webhook notification sent")
        
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
    
    def check_now(self) -> List[AlertEvent]:
        """Check all watches immediately (synchronous).
        
        Returns:
            List of alert events
        """
        events = asyncio.run(self._check_once())
        for event in events:
            self._notify(event)
        return events
    
    async def run_async(self, interval: int = 300, max_checks: Optional[int] = None) -> None:
        """Run the monitor asynchronously.
        
        Args:
            interval: Seconds between checks
            max_checks: Maximum number of checks (None for infinite)
        """
        self._running = True
        checks = 0
        
        logger.info(f"Starting monitor with {len(self._watches)} watches (interval: {interval}s)")
        
        while self._running:
            if max_checks and checks >= max_checks:
                break
            
            events = await self._check_once()
            for event in events:
                self._notify(event)
            
            checks += 1
            
            if self._running:
                await asyncio.sleep(interval)
        
        logger.info("Monitor stopped")
    
    def start(self, interval: int = 300, max_checks: Optional[int] = None) -> None:
        """Start the monitor (blocking).
        
        Args:
            interval: Seconds between checks (default 5 minutes)
            max_checks: Maximum checks before stopping (None = run forever)
        """
        asyncio.run(self.run_async(interval, max_checks))
    
    def start_background(self, interval: int = 300) -> asyncio.Task:
        """Start the monitor in the background.
        
        Returns:
            The asyncio Task running the monitor
        """
        return asyncio.create_task(self.run_async(interval))
    
    def stop(self) -> None:
        """Stop the monitor."""
        self._running = False


# ==========================================================================
# CONVENIENCE FUNCTIONS
# ==========================================================================

def watch_and_book(origin: str,
                   destination: str,
                   dates: List[date],
                   email: str,
                   password: str,
                   preferred_times: Optional[List[str]] = None,
                   check_interval: int = 300) -> None:
    """Watch for availability and auto-book when found.
    
    This is a blocking call that runs until a trip is booked.
    
    Args:
        origin: Origin station
        destination: Destination station
        dates: Dates to watch
        email: SNCF Connect email
        password: SNCF Connect password
        preferred_times: Optional preferred departure times
        check_interval: Seconds between checks
    """
    monitor = TGVMaxMonitor(email=email, password=password)
    
    booked = False
    
    def on_booked(event: AlertEvent):
        nonlocal booked
        if event.booked and event.booked.is_success:
            booked = True
            monitor.stop()
    
    monitor.on_available(on_booked)
    monitor.watch(
        origin=origin,
        destination=destination,
        dates=dates,
        preferred_times=preferred_times,
        auto_book=True
    )
    
    # Run until booked
    while not booked:
        events = monitor.check_now()
        if booked:
            break
        time.sleep(check_interval)


def quick_monitor(origin: str,
                  destination: str,
                  trip_date: date,
                  callback: Callable[[List[Trip]], None],
                  interval: int = 300) -> TGVMaxMonitor:
    """Quick setup for monitoring a single route.
    
    Args:
        origin: Origin station
        destination: Destination station
        trip_date: Date to monitor
        callback: Function to call when trips are found
        interval: Check interval in seconds
        
    Returns:
        The monitor instance (call .start() to begin)
    """
    monitor = TGVMaxMonitor()
    monitor.watch(origin, destination, [trip_date])
    monitor.on_available(lambda event: callback(event.trips))
    return monitor

