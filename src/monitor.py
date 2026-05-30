"""Availability monitoring for TGV Max trips.

Watches routes for new MAX slots and notifies via callbacks.
Uses the core.search() algorithm under the hood.
"""

from typing import Optional, List, Callable
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
import logging

from models import Trip, BookingResult
from config import SNCFConfig, default_config
from network.core import search

logger = logging.getLogger(__name__)


@dataclass
class WatchRequest:
    origin: str
    destination: str
    dates: List[date]
    preferred_times: Optional[List[str]] = None
    auto_book: bool = False
    watch_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d%H%M%S"))

    def matches(self, trip: Trip) -> bool:
        if trip.departure_date not in self.dates:
            return False
        if self.preferred_times:
            trip_time = trip.departure_time.strftime("%H:%M")
            for pref in self.preferred_times:
                pref_dt = datetime.strptime(pref, "%H:%M")
                trip_dt = datetime.combine(trip.departure_date, trip.departure_time)
                target = datetime.combine(trip.departure_date, pref_dt.time())
                if abs((trip_dt - target).total_seconds()) <= 1800:
                    return True
            return False
        return True


@dataclass
class AlertEvent:
    watch: WatchRequest
    trips: List[Trip]
    timestamp: datetime = field(default_factory=datetime.now)
    booked: Optional[BookingResult] = None


class TGVMaxMonitor:
    """Monitor routes for TGV Max availability."""

    def __init__(self, config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self._watches: List[WatchRequest] = []
        self._callbacks: List[Callable[[AlertEvent], None]] = []
        self._seen: set[str] = set()

    def watch(self, origin: str, destination: str, dates: List[date],
              preferred_times: Optional[List[str]] = None,
              auto_book: bool = False) -> str:
        w = WatchRequest(origin=origin, destination=destination, dates=dates,
                         preferred_times=preferred_times, auto_book=auto_book)
        self._watches.append(w)
        return w.watch_id

    def unwatch(self, watch_id: str) -> bool:
        for i, w in enumerate(self._watches):
            if w.watch_id == watch_id:
                del self._watches[i]
                return True
        return False

    def on_available(self, callback: Callable[[AlertEvent], None]) -> None:
        self._callbacks.append(callback)

    def check_now(self) -> List[AlertEvent]:
        events: List[AlertEvent] = []
        for w in self._watches:
            for trip_date in w.dates:
                try:
                    r = search(w.origin, w.destination, trip_date, decompose=False)
                    new = [t for t in r.direct_free
                           if t.trip_key not in self._seen and w.matches(t)]
                    for t in new:
                        self._seen.add(t.trip_key)
                    if new:
                        event = AlertEvent(watch=w, trips=new)
                        for cb in self._callbacks:
                            try:
                                cb(event)
                            except Exception:
                                pass
                        events.append(event)
                except Exception as e:
                    logger.error("check error %s: %s", w.watch_id, e)
        return events


def quick_monitor(origin: str, destination: str, trip_date: date,
                  callback: Callable[[List[Trip]], None],
                  interval: int = 300) -> TGVMaxMonitor:
    m = TGVMaxMonitor()
    m.watch(origin, destination, [trip_date])
    m.on_available(lambda e: callback(e.trips))
    return m
