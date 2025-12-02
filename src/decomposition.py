"""Trip decomposition - Find multi-leg alternatives when direct trips aren't available.

TGV Max slots are limited. Sometimes a direct Paris→Lyon has no availability,
but Paris→Le Creusot + Le Creusot→Lyon does. This module finds such alternatives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Dict, Tuple, Set
from itertools import combinations
import logging

from models import Trip, Station, TripStatus
from config import SNCFConfig, default_config, get_station_name
from client import SNCFMaxClient


logger = logging.getLogger(__name__)


# Common intermediate stations for decomposition
# These are major TGV stops that can serve as connection points
INTERMEDIATE_STATIONS = {
    # Paris → Lyon axis
    "LE CREUSOT MONTCEAU MONTCHANIN": ["PARIS (intramuros)", "LYON (intramuros)", "DIJON VILLE"],
    "MACON LOCHE": ["PARIS (intramuros)", "LYON (intramuros)"],
    "DIJON VILLE": ["PARIS (intramuros)", "LYON (intramuros)", "MARSEILLE ST CHARLES"],
    
    # Paris → Marseille axis  
    "VALENCE TGV": ["PARIS (intramuros)", "LYON (intramuros)", "MARSEILLE ST CHARLES", "MONTPELLIER ST ROCH"],
    "AVIGNON TGV": ["PARIS (intramuros)", "LYON (intramuros)", "MARSEILLE ST CHARLES", "MONTPELLIER ST ROCH", "NICE VILLE"],
    "AIX EN PROVENCE TGV": ["MARSEILLE ST CHARLES", "PARIS (intramuros)", "LYON (intramuros)"],
    
    # Paris → Bordeaux axis
    "ANGOULEME": ["PARIS (intramuros)", "BORDEAUX ST JEAN"],
    "POITIERS": ["PARIS (intramuros)", "BORDEAUX ST JEAN"],
    "ST PIERRE DES CORPS": ["PARIS (intramuros)", "BORDEAUX ST JEAN", "NANTES"],
    
    # Paris → Lille axis
    "ARRAS": ["PARIS (intramuros)", "LILLE FLANDRES"],
    
    # Paris → Strasbourg axis
    "REIMS": ["PARIS (intramuros)", "STRASBOURG"],
    "METZ VILLE": ["PARIS (intramuros)", "STRASBOURG"],
    
    # Lyon connections
    "CHAMBERY CHALLES LES EAUX": ["LYON (intramuros)", "GRENOBLE"],
    "GRENOBLE": ["LYON (intramuros)", "PARIS (intramuros)"],
}


@dataclass
class TripLeg:
    """A single leg of a multi-leg journey."""
    trip: Trip
    is_max: bool  # True if this leg uses TGV Max, False if needs regular ticket


@dataclass
class CompositeTrip:
    """A trip composed of multiple legs."""
    legs: List[TripLeg]
    total_duration: timedelta = field(init=False)
    connection_time: timedelta = field(init=False)
    max_legs: int = field(init=False)
    paid_legs: int = field(init=False)
    
    def __post_init__(self):
        if not self.legs:
            self.total_duration = timedelta()
            self.connection_time = timedelta()
            self.max_legs = 0
            self.paid_legs = 0
            return
        
        # Calculate total duration
        first_departure = datetime.combine(
            self.legs[0].trip.departure_date,
            self.legs[0].trip.departure_time
        )
        last_arrival = datetime.combine(
            self.legs[-1].trip.departure_date,
            self.legs[-1].trip.arrival_time
        )
        self.total_duration = last_arrival - first_departure
        
        # Calculate connection time
        travel_time = timedelta()
        for leg in self.legs:
            leg_duration = (
                datetime.combine(leg.trip.departure_date, leg.trip.arrival_time) -
                datetime.combine(leg.trip.departure_date, leg.trip.departure_time)
            )
            travel_time += leg_duration
        self.connection_time = self.total_duration - travel_time
        
        # Count legs by type
        self.max_legs = sum(1 for leg in self.legs if leg.is_max)
        self.paid_legs = len(self.legs) - self.max_legs
    
    @property
    def origin(self) -> str:
        return str(self.legs[0].trip.origin) if self.legs else ""
    
    @property
    def destination(self) -> str:
        return str(self.legs[-1].trip.destination) if self.legs else ""
    
    @property
    def departure_date(self) -> date:
        return self.legs[0].trip.departure_date if self.legs else date.today()
    
    @property
    def departure_time(self) -> time:
        return self.legs[0].trip.departure_time if self.legs else time(0, 0)
    
    @property
    def arrival_time(self) -> time:
        return self.legs[-1].trip.arrival_time if self.legs else time(0, 0)
    
    @property
    def is_fully_max(self) -> bool:
        """True if all legs use TGV Max."""
        return self.paid_legs == 0
    
    @property
    def score(self) -> float:
        """Lower is better. Prioritizes: fewer paid legs, shorter duration, fewer connections."""
        return (
            self.paid_legs * 1000 +  # Heavily penalize paid legs
            self.total_duration.total_seconds() / 60 +  # Duration in minutes
            len(self.legs) * 30  # Slight penalty for more connections
        )
    
    def __str__(self) -> str:
        legs_str = " → ".join([
            f"{leg.trip.origin}({leg.trip.departure_time.strftime('%H:%M')})"
            for leg in self.legs
        ])
        legs_str += f" → {self.destination}({self.arrival_time.strftime('%H:%M')})"
        
        max_info = f"{self.max_legs} MAX" if self.max_legs else ""
        paid_info = f"{self.paid_legs} paid" if self.paid_legs else ""
        type_info = ", ".join(filter(None, [max_info, paid_info]))
        
        return f"{legs_str} [{type_info}]"


class TripDecomposer:
    """Finds multi-leg alternatives when direct trips aren't available.
    
    Example:
        decomposer = TripDecomposer()
        
        # Find alternatives for Paris → Lyon
        alternatives = decomposer.find_alternatives(
            origin="paris",
            destination="lyon",
            trip_date=date(2025, 1, 15),
            max_legs=2
        )
        
        for alt in alternatives:
            print(f"{alt} - {alt.total_duration}")
    """
    
    # Minimum connection time (minutes)
    MIN_CONNECTION_TIME = 15
    
    # Maximum connection time (minutes)  
    MAX_CONNECTION_TIME = 120
    
    def __init__(self, config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self._client = SNCFMaxClient(config=self.config)
        self._cache: Dict[str, List[Trip]] = {}
    
    def _get_intermediate_stations(self, origin: str, destination: str) -> List[str]:
        """Get potential intermediate stations for a route."""
        origin_full = get_station_name(origin)
        dest_full = get_station_name(destination)
        
        candidates = []
        
        for station, connects in INTERMEDIATE_STATIONS.items():
            # Station must connect to both origin and destination
            if origin_full in connects and dest_full in connects:
                candidates.append(station)
            # Or be geographically between them
            elif origin_full in connects or dest_full in connects:
                candidates.append(station)
        
        return candidates
    
    def _fetch_trips(self, origin: str, destination: str, trip_date: date,
                     only_max: bool = True) -> List[Trip]:
        """Fetch trips with caching."""
        cache_key = f"{origin}|{destination}|{trip_date}|{only_max}"
        
        if cache_key not in self._cache:
            self._cache[cache_key] = self._client.search_trips(
                origin=origin,
                destination=destination,
                trip_date=trip_date,
                only_available=only_max
            )
        
        return self._cache[cache_key]
    
    def _can_connect(self, arrival: Trip, departure: Trip) -> bool:
        """Check if two trips can be connected."""
        if arrival.departure_date != departure.departure_date:
            return False
        
        # Check arrival station matches departure station
        if str(arrival.destination) != str(departure.origin):
            return False
        
        # Calculate connection time
        arr_time = datetime.combine(arrival.departure_date, arrival.arrival_time)
        dep_time = datetime.combine(departure.departure_date, departure.departure_time)
        
        connection = (dep_time - arr_time).total_seconds() / 60
        
        return self.MIN_CONNECTION_TIME <= connection <= self.MAX_CONNECTION_TIME
    
    def find_alternatives(self,
                          origin: str,
                          destination: str,
                          trip_date: date,
                          max_legs: int = 2,
                          include_paid: bool = True,
                          departure_after: Optional[time] = None,
                          arrival_before: Optional[time] = None) -> List[CompositeTrip]:
        """Find multi-leg alternatives for a route.
        
        Args:
            origin: Origin station
            destination: Destination station
            trip_date: Date of travel
            max_legs: Maximum number of legs (default 2)
            include_paid: Include options where some legs need regular tickets
            departure_after: Only consider trips departing after this time
            arrival_before: Only consider trips arriving before this time
            
        Returns:
            List of CompositeTrip options, sorted by score (best first)
        """
        origin_full = get_station_name(origin)
        dest_full = get_station_name(destination)
        
        alternatives = []
        
        # First, try direct MAX trips
        direct_trips = self._fetch_trips(origin, destination, trip_date, only_max=True)
        for trip in direct_trips:
            if departure_after and trip.departure_time < departure_after:
                continue
            if arrival_before and trip.arrival_time > arrival_before:
                continue
            
            alternatives.append(CompositeTrip(legs=[TripLeg(trip=trip, is_max=True)]))
        
        # If we have direct MAX trips, we might still want to find alternatives
        # for different times or better options
        
        # Find 2-leg alternatives
        if max_legs >= 2:
            intermediates = self._get_intermediate_stations(origin, destination)
            
            for intermediate in intermediates:
                # Leg 1: Origin → Intermediate
                leg1_max = self._fetch_trips(origin, intermediate, trip_date, only_max=True)
                leg1_paid = self._fetch_trips(origin, intermediate, trip_date, only_max=False) if include_paid else []
                
                # Leg 2: Intermediate → Destination
                leg2_max = self._fetch_trips(intermediate, destination, trip_date, only_max=True)
                leg2_paid = self._fetch_trips(intermediate, destination, trip_date, only_max=False) if include_paid else []
                
                # Try all combinations
                for first_leg, first_is_max in [(t, True) for t in leg1_max] + [(t, False) for t in leg1_paid]:
                    if departure_after and first_leg.departure_time < departure_after:
                        continue
                    
                    for second_leg, second_is_max in [(t, True) for t in leg2_max] + [(t, False) for t in leg2_paid]:
                        if arrival_before and second_leg.arrival_time > arrival_before:
                            continue
                        
                        if self._can_connect(first_leg, second_leg):
                            composite = CompositeTrip(legs=[
                                TripLeg(trip=first_leg, is_max=first_is_max),
                                TripLeg(trip=second_leg, is_max=second_is_max),
                            ])
                            
                            # Only add if at least one leg is MAX (otherwise just buy tickets)
                            if composite.max_legs > 0 or not include_paid:
                                alternatives.append(composite)
        
        # Sort by score (lower is better)
        alternatives.sort(key=lambda x: x.score)
        
        # Remove duplicates (same departure and arrival times)
        seen = set()
        unique = []
        for alt in alternatives:
            key = (alt.departure_time, alt.arrival_time, alt.max_legs)
            if key not in seen:
                seen.add(key)
                unique.append(alt)
        
        return unique
    
    def find_best_alternative(self,
                              origin: str,
                              destination: str,
                              trip_date: date,
                              prefer_fully_max: bool = True) -> Optional[CompositeTrip]:
        """Find the best alternative for a route.
        
        Args:
            origin: Origin station
            destination: Destination station
            trip_date: Date of travel
            prefer_fully_max: Prefer fully MAX trips over mixed ones
            
        Returns:
            Best CompositeTrip or None
        """
        alternatives = self.find_alternatives(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            include_paid=not prefer_fully_max
        )
        
        if prefer_fully_max:
            # First try to find fully MAX option
            fully_max = [a for a in alternatives if a.is_fully_max]
            if fully_max:
                return fully_max[0]
        
        return alternatives[0] if alternatives else None


def find_trip_with_decomposition(origin: str,
                                  destination: str,
                                  trip_date: date,
                                  config: Optional[SNCFConfig] = None) -> List[CompositeTrip]:
    """Convenience function to find trips including decomposed alternatives.
    
    Returns all options sorted by preference (direct MAX first, then alternatives).
    """
    decomposer = TripDecomposer(config=config)
    return decomposer.find_alternatives(
        origin=origin,
        destination=destination,
        trip_date=trip_date,
        include_paid=True
    )

