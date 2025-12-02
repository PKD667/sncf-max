"""Deadline-based trip search and booking.

Find the best trip arriving before a deadline, with continuous scanning
to optimize for minimal wait time at destination.

Example: "I need to be in Lyon by 1 PM" - the system will:
1. Search for trains arriving before 13:00
2. Prefer trains arriving closest to 13:00 (minimize wait)
3. If no same-day option, check the day before
4. Continuously scan for better options until booked or deadline passes

BROAD alternatives support: When no MAX is available, scan everything:
- Different routes via intermediate stations
- Priced alternatives with real price retrieval
- Mixed MAX + paid combinations
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Callable, Tuple
from enum import Enum
import logging

from models import Trip, BookingResult, BookingStatus, Session, TripStatus
from config import SNCFConfig, default_config, get_station_name
from client import SNCFMaxClient
from decomposition import TripDecomposer, CompositeTrip, TripLeg


logger = logging.getLogger(__name__)


class DeadlineStrategy(Enum):
    """Strategy for deadline-based search."""
    SAME_DAY_ONLY = "same_day"       # Only search same day
    PREVIOUS_DAY = "previous_day"    # Allow arriving day before
    FLEXIBLE = "flexible"             # Search multiple days before


class AlternativeType(Enum):
    """Type of travel alternative."""
    DIRECT_MAX = "direct_max"           # Direct TGV MAX trip
    DECOMPOSED_MAX = "decomposed_max"   # Multi-leg, all MAX
    MIXED = "mixed"                      # Mix of MAX and paid legs
    DIRECT_PAID = "direct_paid"          # Direct, requires payment
    DECOMPOSED_PAID = "decomposed_paid"  # Multi-leg, all paid


@dataclass
class DeadlineConstraint:
    """Constraint for deadline-based search."""
    arrival_city: str
    deadline: datetime  # Must arrive before this time
    departure_city: str
    strategy: DeadlineStrategy = DeadlineStrategy.PREVIOUS_DAY
    max_wait_hours: float = 24.0  # Maximum hours to wait at destination
    min_travel_buffer: timedelta = timedelta(minutes=30)  # Buffer before deadline
    include_priced: bool = True  # Include priced alternatives as fallback
    max_price_cents: Optional[int] = None  # Maximum price to consider (None = no limit)
    
    @property
    def effective_deadline(self) -> datetime:
        """Deadline minus buffer."""
        return self.deadline - self.min_travel_buffer
    
    @property 
    def deadline_date(self) -> date:
        return self.deadline.date()
    
    @property
    def deadline_time(self) -> time:
        return self.deadline.time()
    
    def days_to_search(self) -> List[date]:
        """Get list of dates to search based on strategy."""
        dates = [self.deadline_date]
        
        if self.strategy == DeadlineStrategy.PREVIOUS_DAY:
            dates.append(self.deadline_date - timedelta(days=1))
        elif self.strategy == DeadlineStrategy.FLEXIBLE:
            # Search up to 3 days before
            for i in range(1, 4):
                dates.append(self.deadline_date - timedelta(days=i))
        
        return dates
    
    def __str__(self) -> str:
        return f"Arrive in {self.arrival_city} by {self.deadline.strftime('%Y-%m-%d %H:%M')}"


@dataclass
class DeadlineMatch:
    """A trip option matching a deadline constraint."""
    trip: Trip
    composite: Optional[CompositeTrip]  # If decomposed
    wait_time: timedelta  # Time between arrival and deadline
    score: float  # Lower is better
    alternative_type: AlternativeType = AlternativeType.DIRECT_MAX
    total_price_cents: Optional[int] = None  # Total price for paid legs
    
    @property
    def arrival_datetime(self) -> datetime:
        if self.composite:
            # For overnight trips, arrival could be next day
            last_leg = self.composite.legs[-1]
            arr_date = last_leg.trip.departure_date
            # Handle overnight: if arrival < departure time, it's next day
            if last_leg.trip.arrival_time < last_leg.trip.departure_time:
                arr_date = arr_date + timedelta(days=1)
            return datetime.combine(arr_date, self.composite.arrival_time)
        
        # Handle overnight for single trip
        arr_date = self.trip.departure_date
        if self.trip.arrival_time < self.trip.departure_time:
            arr_date = arr_date + timedelta(days=1)
        return datetime.combine(arr_date, self.trip.arrival_time)
    
    @property
    def departure_datetime(self) -> datetime:
        if self.composite:
            return datetime.combine(
                self.composite.departure_date,
                self.composite.departure_time
            )
        return datetime.combine(self.trip.departure_date, self.trip.departure_time)
    
    @property
    def is_same_day(self) -> bool:
        return self.departure_datetime.date() == self.arrival_datetime.date()
    
    @property
    def is_decomposed(self) -> bool:
        return self.composite is not None and len(self.composite.legs) > 1
    
    @property
    def is_free(self) -> bool:
        """True if entirely covered by MAX (no payment needed)."""
        return self.alternative_type in (AlternativeType.DIRECT_MAX, AlternativeType.DECOMPOSED_MAX)
    
    @property
    def total_price(self) -> Optional[float]:
        """Price in euros."""
        return self.total_price_cents / 100 if self.total_price_cents else None
    
    @property
    def price_display(self) -> str:
        """Human readable price."""
        if self.is_free:
            return "MAX (0€)"
        if self.total_price_cents is None:
            return "Price unknown"
        return f"{self.total_price:.2f}€"
    
    def __str__(self) -> str:
        arrival = self.arrival_datetime.strftime("%d/%m %H:%M")
        wait_hours = self.wait_time.total_seconds() / 3600
        type_str = self.alternative_type.value
        
        if self.is_decomposed:
            route = f"{self.composite}"
        else:
            route = f"Train {self.trip.train_number}"
        
        price_str = f" [{self.price_display}]" if not self.is_free else " [MAX]"
        return f"{route} - arrives {arrival} ({wait_hours:.1f}h before){price_str}"


class DeadlineSearcher:
    """Search for trips based on arrival deadline.
    
    BROAD search mode: Scans everything to find alternatives:
    - Direct MAX trips
    - Decomposed MAX (via intermediate stations)
    - Mixed MAX + paid
    - Fully paid as last resort
    
    Example:
        searcher = DeadlineSearcher()
        
        # Find trips arriving in Lyon by 1 PM
        constraint = DeadlineConstraint(
            departure_city="paris",
            arrival_city="lyon",
            deadline=datetime(2025, 1, 15, 13, 0),
            include_priced=True,  # Include paid alternatives
        )
        
        matches = searcher.search(constraint)
        for match in matches[:5]:
            print(match)
    """
    
    def __init__(self, config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self._client = SNCFMaxClient(config=self.config)
        self._decomposer = TripDecomposer(config=self.config)
        self._price_cache: dict[str, int] = {}  # "train_no:date" -> price_cents
    
    def _calculate_score(self, 
                         arrival: datetime, 
                         deadline: datetime,
                         is_same_day: bool,
                         alternative_type: AlternativeType,
                         total_price_cents: Optional[int] = None,
                         num_legs: int = 1) -> float:
        """Calculate score for a trip option (lower is better).
        
        Priorities (in order):
        1. Must ARRIVE before deadline
        2. Prefer same-day arrival
        3. Prefer less wait time (closer to deadline)
        4. Prefer MAX over paid
        5. Prefer direct over decomposed
        6. If paid, prefer cheaper
        """
        if arrival > deadline:
            return float('inf')  # Invalid - arrives after deadline
        
        wait_seconds = (deadline - arrival).total_seconds()
        wait_hours = wait_seconds / 3600
        
        score = 0.0
        
        # Heavily penalize previous day (but still prefer it over nothing)
        if not is_same_day:
            score += 1000
        
        # Penalize wait time (each hour adds to score)
        score += wait_hours * 10
        
        # Type penalties
        type_penalties = {
            AlternativeType.DIRECT_MAX: 0,
            AlternativeType.DECOMPOSED_MAX: 50,
            AlternativeType.MIXED: 300,  # Mixed is better than fully paid
            AlternativeType.DIRECT_PAID: 500,
            AlternativeType.DECOMPOSED_PAID: 600,
        }
        score += type_penalties.get(alternative_type, 500)
        
        # If paid, add price penalty (1 point per euro)
        if total_price_cents:
            score += total_price_cents / 100
        
        # Small penalty for more connections
        if num_legs > 1:
            score += (num_legs - 1) * 20
        
        return score
    
    def _get_alternative_type(self, composite: Optional[CompositeTrip], 
                               trip: Trip, is_max: bool) -> AlternativeType:
        """Determine the alternative type."""
        if composite:
            if composite.is_fully_max:
                return AlternativeType.DECOMPOSED_MAX
            elif composite.max_legs > 0:
                return AlternativeType.MIXED
            else:
                return AlternativeType.DECOMPOSED_PAID
        else:
            if is_max:
                return AlternativeType.DIRECT_MAX
            else:
                return AlternativeType.DIRECT_PAID
    
    def _fetch_price(self, trip: Trip) -> Optional[int]:
        """Fetch real price for a trip (in cents).
        
        This attempts to get the actual price from SNCF.
        Returns cached value if available.
        
        NOTE: Currently uses estimates based on distance/route.
        Real prices would require querying SNCF Connect API.
        """
        cache_key = f"{trip.train_number}:{trip.departure_date}:{trip.origin}:{trip.destination}"
        
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]
        
        # If trip already has price info, use it
        if trip.price_cents is not None:
            self._price_cache[cache_key] = trip.price_cents
            return trip.price_cents
        
        # Estimate based on route distance (rough km estimates)
        # TGV pricing is roughly 10-15 cents per km for 2nd class
        route_distances_km = {
            # Paris hub
            ("PARIS (intramuros)", "LYON (intramuros)"): 460,
            ("PARIS (intramuros)", "LYON PART DIEU"): 460,
            ("PARIS (intramuros)", "DIJON VILLE"): 310,
            ("PARIS (intramuros)", "MACON LOCHE"): 390,
            ("PARIS (intramuros)", "LE CREUSOT MONTCEAU MONTCHANIN"): 350,
            ("PARIS (intramuros)", "MARSEILLE ST CHARLES"): 775,
            ("PARIS (intramuros)", "BORDEAUX ST JEAN"): 585,
            ("PARIS (intramuros)", "LILLE FLANDRES"): 225,
            ("PARIS (intramuros)", "STRASBOURG"): 490,
            ("PARIS (intramuros)", "NANTES"): 385,
            ("PARIS (intramuros)", "RENNES"): 350,
            ("PARIS (intramuros)", "TOULOUSE MATABIAU"): 680,
            ("PARIS (intramuros)", "MONTPELLIER ST ROCH"): 750,
            ("PARIS (intramuros)", "NICE VILLE"): 930,
            ("PARIS (intramuros)", "AVIGNON TGV"): 690,
            ("PARIS (intramuros)", "VALENCE TGV"): 560,
            # Lyon hub
            ("LYON (intramuros)", "MARSEILLE ST CHARLES"): 315,
            ("LYON (intramuros)", "DIJON VILLE"): 190,
            ("LYON (intramuros)", "PARIS (intramuros)"): 460,
            ("LYON PART DIEU", "MARSEILLE ST CHARLES"): 315,
            ("LYON PART DIEU", "PARIS (intramuros)"): 460,
            # Dijon connections
            ("DIJON VILLE", "LYON (intramuros)"): 190,
            ("DIJON VILLE", "LYON PART DIEU"): 190,
            ("DIJON VILLE", "MACON LOCHE"): 100,
            ("DIJON VILLE", "LE CREUSOT MONTCEAU MONTCHANIN"): 80,
            # Mâcon connections  
            ("MACON LOCHE", "LYON (intramuros)"): 70,
            ("MACON LOCHE", "LYON PART DIEU"): 70,
            # Le Creusot connections
            ("LE CREUSOT MONTCEAU MONTCHANIN", "LYON (intramuros)"): 130,
            ("LE CREUSOT MONTCEAU MONTCHANIN", "LYON PART DIEU"): 130,
        }
        
        origin = str(trip.origin)
        dest = str(trip.destination)
        
        # Try to find distance
        distance = route_distances_km.get((origin, dest))
        if not distance:
            distance = route_distances_km.get((dest, origin))
        
        if distance:
            # Price estimate: ~12 cents/km for TGV, cheaper for OUIGO
            rate = 10 if trip.entity == "OUIGO" else 12
            price = int(distance * rate)
        else:
            # Fallback: estimate based on travel time if we have it
            # Rough estimate: TGV averages 250 km/h
            if trip.departure_time and trip.arrival_time:
                from datetime import datetime, timedelta
                dep_dt = datetime.combine(trip.departure_date, trip.departure_time)
                arr_dt = datetime.combine(trip.departure_date, trip.arrival_time)
                if arr_dt < dep_dt:  # overnight
                    arr_dt += timedelta(days=1)
                hours = (arr_dt - dep_dt).total_seconds() / 3600
                estimated_km = hours * 220  # conservative speed
                rate = 10 if trip.entity == "OUIGO" else 12
                price = int(estimated_km * rate)
            else:
                # Last resort default
                price = 4500 if trip.entity == "OUIGO" else 5500
        
        self._price_cache[cache_key] = price
        return price
    
    def _calculate_total_price(self, composite: Optional[CompositeTrip], 
                                trip: Trip) -> Optional[int]:
        """Calculate total price for all paid legs."""
        if composite:
            total = 0
            for leg in composite.legs:
                if not leg.is_max:
                    price = self._fetch_price(leg.trip)
                    if price:
                        total += price
            return total if total > 0 else None
        else:
            # Single trip
            if trip.available_for_max == TripStatus.AVAILABLE:
                return None  # Free with MAX
            return self._fetch_price(trip)
    
    def search(self, 
               constraint: DeadlineConstraint,
               include_decomposition: bool = True) -> List[DeadlineMatch]:
        """Search for trips meeting the deadline constraint (based on ARRIVAL time).
        
        Args:
            constraint: The deadline constraint
            include_decomposition: Include multi-leg alternatives
            
        Returns:
            List of matches sorted by score (best first)
        """
        matches = []
        deadline = constraint.effective_deadline
        include_paid = constraint.include_priced
        
        for trip_date in constraint.days_to_search():
            is_same_day = trip_date == constraint.deadline_date
            
            # For deadline checking: we need to check ARRIVAL time against deadline
            # For same day: arrival must be before deadline time
            # For previous day: any arrival is OK (they arrive the day before)
            deadline_for_date = constraint.deadline_time if is_same_day else time(23, 59)
            
            # === DIRECT TRIPS ===
            # First get ALL trips (not just MAX) if include_paid is True
            all_direct = self._client.search_trips(
                origin=constraint.departure_city,
                destination=constraint.arrival_city,
                trip_date=trip_date,
                only_available=False  # Get ALL trips
            )
            
            # Get normalized destination name for comparison
            dest_normalized = get_station_name(constraint.arrival_city).upper()
            
            for trip in all_direct:
                # Verify destination matches what we asked for (API might return intermediate stops)
                trip_dest = str(trip.destination).upper()
                if dest_normalized not in trip_dest and trip_dest not in dest_normalized:
                    logger.debug(f"Skipping trip {trip.train_number}: dest {trip.destination} != {constraint.arrival_city}")
                    continue
                
                # Compute arrival datetime properly (handle overnight)
                arr_date = trip_date
                if trip.arrival_time < trip.departure_time:
                    arr_date = trip_date + timedelta(days=1)
                arrival_dt = datetime.combine(arr_date, trip.arrival_time)
                
                # Check if ARRIVES before deadline
                if arrival_dt > deadline:
                    continue
                
                # Check max wait time
                wait = deadline - arrival_dt
                if wait.total_seconds() / 3600 > constraint.max_wait_hours:
                    continue
                
                is_max = trip.available_for_max == TripStatus.AVAILABLE
                
                # Skip non-MAX if not including paid
                if not is_max and not include_paid:
                    continue
                
                alt_type = AlternativeType.DIRECT_MAX if is_max else AlternativeType.DIRECT_PAID
                total_price = None if is_max else self._fetch_price(trip)
                
                # Check max price constraint
                if constraint.max_price_cents and total_price:
                    if total_price > constraint.max_price_cents:
                        continue
                
                score = self._calculate_score(
                    arrival=arrival_dt,
                    deadline=deadline,
                    is_same_day=is_same_day,
                    alternative_type=alt_type,
                    total_price_cents=total_price
                )
                
                matches.append(DeadlineMatch(
                    trip=trip,
                    composite=None,
                    wait_time=wait,
                    score=score,
                    alternative_type=alt_type,
                    total_price_cents=total_price
                ))
            
            # === DECOMPOSED ALTERNATIVES ===
            if include_decomposition:
                alternatives = self._decomposer.find_alternatives(
                    origin=constraint.departure_city,
                    destination=constraint.arrival_city,
                    trip_date=trip_date,
                    arrival_before=deadline_for_date,
                    include_paid=include_paid
                )
                
                for alt in alternatives:
                    # Skip single-leg (already covered by direct)
                    if len(alt.legs) == 1:
                        continue
                    
                    # Compute arrival datetime (from last leg)
                    last_leg = alt.legs[-1]
                    arr_date = trip_date
                    if last_leg.trip.arrival_time < last_leg.trip.departure_time:
                        arr_date = trip_date + timedelta(days=1)
                    arrival_dt = datetime.combine(arr_date, alt.arrival_time)
                    
                    if arrival_dt > deadline:
                        continue
                    
                    wait = deadline - arrival_dt
                    if wait.total_seconds() / 3600 > constraint.max_wait_hours:
                        continue
                    
                    # Determine type and price
                    if alt.is_fully_max:
                        alt_type = AlternativeType.DECOMPOSED_MAX
                        total_price = None
                    elif alt.max_legs > 0:
                        alt_type = AlternativeType.MIXED
                        total_price = self._calculate_total_price(alt, alt.legs[0].trip)
                    else:
                        alt_type = AlternativeType.DECOMPOSED_PAID
                        total_price = self._calculate_total_price(alt, alt.legs[0].trip)
                    
                    # Skip fully paid decomposed if not including paid
                    if alt_type == AlternativeType.DECOMPOSED_PAID and not include_paid:
                        continue
                    
                    # Check max price constraint
                    if constraint.max_price_cents and total_price:
                        if total_price > constraint.max_price_cents:
                            continue
                    
                    score = self._calculate_score(
                        arrival=arrival_dt,
                        deadline=deadline,
                        is_same_day=is_same_day,
                        alternative_type=alt_type,
                        total_price_cents=total_price,
                        num_legs=len(alt.legs)
                    )
                    
                    matches.append(DeadlineMatch(
                        trip=alt.legs[0].trip,
                        composite=alt,
                        wait_time=wait,
                        score=score,
                        alternative_type=alt_type,
                        total_price_cents=total_price
                    ))
        
        # Sort by score
        matches.sort(key=lambda m: m.score)
        
        return matches
    
    def find_best(self, constraint: DeadlineConstraint) -> Optional[DeadlineMatch]:
        """Find the single best trip for a deadline."""
        matches = self.search(constraint)
        return matches[0] if matches else None
    
    def search_broad(self, 
                     constraint: DeadlineConstraint,
                     fallback_to_any: bool = True) -> List[DeadlineMatch]:
        """BROAD search - scan everything possible.
        
        This is the most aggressive search mode:
        1. First tries to find MAX options
        2. Then decomposed MAX
        3. Then mixed MAX + paid
        4. Finally pure paid options
        
        Args:
            constraint: The deadline constraint  
            fallback_to_any: If no MAX found, include any priced alternative
            
        Returns:
            All possible matches sorted by preference
        """
        # Force include priced for broad search
        constraint_copy = DeadlineConstraint(
            arrival_city=constraint.arrival_city,
            deadline=constraint.deadline,
            departure_city=constraint.departure_city,
            strategy=constraint.strategy,
            max_wait_hours=constraint.max_wait_hours,
            min_travel_buffer=constraint.min_travel_buffer,
            include_priced=True,  # Always include priced in broad search
            max_price_cents=constraint.max_price_cents
        )
        
        all_matches = self.search(constraint_copy, include_decomposition=True)
        
        if not all_matches:
            return []
        
        # If we have MAX options and don't want to fallback, filter
        if not fallback_to_any:
            max_matches = [m for m in all_matches if m.is_free]
            if max_matches:
                return max_matches
        
        return all_matches
    
    def get_price_summary(self, matches: List[DeadlineMatch]) -> dict:
        """Get a summary of prices across matches."""
        prices = [m.total_price_cents for m in matches if m.total_price_cents]
        
        if not prices:
            return {
                "has_free": any(m.is_free for m in matches),
                "min_price": None,
                "max_price": None,
                "avg_price": None,
                "count_free": sum(1 for m in matches if m.is_free),
                "count_paid": sum(1 for m in matches if not m.is_free)
            }
        
        return {
            "has_free": any(m.is_free for m in matches),
            "min_price": min(prices) / 100,
            "max_price": max(prices) / 100,
            "avg_price": sum(prices) / len(prices) / 100,
            "count_free": sum(1 for m in matches if m.is_free),
            "count_paid": sum(1 for m in matches if not m.is_free)
        }


class DeadlineBooker:
    """Continuously scan and book the best trip for a deadline.
    
    Supports booking both MAX and paid alternatives.
    
    Example:
        booker = DeadlineBooker(email="...", password="...")
        
        constraint = DeadlineConstraint(
            departure_city="paris",
            arrival_city="lyon",
            deadline=datetime(2025, 1, 15, 13, 0),
            include_priced=True,  # Will book paid if no MAX available
        )
        
        # Will scan until a good option is booked or deadline passes
        result = booker.run(constraint)
    """
    
    def __init__(self,
                 email: Optional[str] = None,
                 password: Optional[str] = None,
                 config: Optional[SNCFConfig] = None):
        self.config = config or default_config
        self.email = email or self.config.SNCF_EMAIL
        self.password = password or self.config.SNCF_PASSWORD
        
        self._searcher = DeadlineSearcher(config=self.config)
        self._current_best: Optional[DeadlineMatch] = None
        self._booked: Optional[BookingResult] = None
        
        # Callbacks
        self._on_better_found: List[Callable[[DeadlineMatch, Optional[DeadlineMatch]], None]] = []
        self._on_booked: List[Callable[[BookingResult, DeadlineMatch], None]] = []
        self._on_price_update: List[Callable[[DeadlineMatch, int], None]] = []
    
    def on_better_found(self, callback: Callable[[DeadlineMatch, Optional[DeadlineMatch]], None]) -> None:
        """Register callback when a better option is found."""
        self._on_better_found.append(callback)
    
    def on_booked(self, callback: Callable[[BookingResult, DeadlineMatch], None]) -> None:
        """Register callback when booking is made."""
        self._on_booked.append(callback)
    
    def on_price_update(self, callback: Callable[[DeadlineMatch, int], None]) -> None:
        """Register callback when price information is updated."""
        self._on_price_update.append(callback)
    
    def _is_better(self, new: DeadlineMatch, current: Optional[DeadlineMatch]) -> bool:
        """Check if new match is better than current."""
        if current is None:
            return True
        
        # Always prefer free over paid
        if new.is_free and not current.is_free:
            return True
        
        # If both same type, must be significantly better (at least 10% improvement)
        return new.score < current.score * 0.9
    
    async def _try_book(self, match: DeadlineMatch) -> Optional[BookingResult]:
        """Attempt to book a match."""
        if not self.email or not self.password:
            logger.warning("No credentials - cannot book")
            return None
        
        try:
            from .api import TGVMaxAPI
            
            api = TGVMaxAPI(config=self.config)
            api.login(self.email, self.password)
            
            if match.is_decomposed:
                # Book each leg (MAX legs use MAX, paid legs need payment)
                results = []
                for leg in match.composite.legs:
                    if leg.is_max:
                        result = api.book(leg.trip)
                        results.append(result)
                        if not result.is_success:
                            return result  # Return first failure
                    else:
                        # For paid legs, we'd need to handle payment
                        # This is a placeholder - real implementation needed
                        logger.info(f"Paid leg: {leg.trip} - price: {match.price_display}")
                        result = api.book(leg.trip)  # Will need payment flow
                        results.append(result)
                
                if results:
                    return results[-1]
            else:
                return api.book(match.trip)
                
        except Exception as e:
            logger.error(f"Booking failed: {e}")
            return BookingResult(
                status=BookingStatus.FAILED,
                trip=match.trip,
                message=str(e)
            )
        
        return None
    
    async def scan_once(self, 
                        constraint: DeadlineConstraint,
                        broad_search: bool = True) -> Optional[DeadlineMatch]:
        """Scan once and return the best match.
        
        Args:
            constraint: The deadline constraint
            broad_search: Use broad search (includes all alternatives)
        """
        if broad_search:
            matches = self._searcher.search_broad(constraint)
        else:
            matches = self._searcher.search(constraint)
        
        if not matches:
            return None
        
        best = matches[0]
        
        if self._is_better(best, self._current_best):
            old = self._current_best
            self._current_best = best
            
            # Notify
            for callback in self._on_better_found:
                try:
                    callback(best, old)
                except Exception as e:
                    logger.error(f"Callback error: {e}")
        
        return self._current_best
    
    async def run_async(self,
                        constraint: DeadlineConstraint,
                        auto_book: bool = True,
                        scan_interval: int = 120,
                        min_score_to_book: Optional[float] = None,
                        stop_after_book: bool = True,
                        prefer_free: bool = True,
                        broad_search: bool = True) -> Optional[BookingResult]:
        """Run continuous scanning until deadline or booking.
        
        Args:
            constraint: The deadline constraint
            auto_book: Automatically book when good option found
            scan_interval: Seconds between scans
            min_score_to_book: Only book if score is below this threshold
            stop_after_book: Stop scanning after successful booking
            prefer_free: Wait for MAX option even if paid is available
            broad_search: Use broad search mode (all alternatives)
            
        Returns:
            BookingResult if booked, None otherwise
        """
        logger.info(f"Starting deadline search: {constraint}")
        logger.info(f"Broad search: {broad_search}, Include priced: {constraint.include_priced}")
        
        best_free: Optional[DeadlineMatch] = None
        best_paid: Optional[DeadlineMatch] = None
        
        while datetime.now() < constraint.deadline:
            match = await self.scan_once(constraint, broad_search=broad_search)
            
            if match:
                logger.info(f"Best option: {match}")
                
                # Track best free and paid separately
                if match.is_free:
                    if best_free is None or match.score < best_free.score:
                        best_free = match
                else:
                    if best_paid is None or match.score < best_paid.score:
                        best_paid = match
                
                # Log price summary
                if broad_search:
                    all_matches = self._searcher.search_broad(constraint)
                    summary = self._searcher.get_price_summary(all_matches)
                    logger.info(f"Options: {summary['count_free']} free, {summary['count_paid']} paid")
                    if summary['min_price']:
                        logger.info(f"Prices: {summary['min_price']:.2f}€ - {summary['max_price']:.2f}€")
                
                # Check if good enough to book
                should_book = auto_book
                
                if min_score_to_book and match.score > min_score_to_book:
                    should_book = False
                    logger.info(f"Score {match.score} > threshold {min_score_to_book}, waiting...")
                
                # If prefer_free, only book paid when deadline is close
                if should_book and not match.is_free and prefer_free:
                    time_left = (constraint.deadline - datetime.now()).total_seconds() / 3600
                    if time_left > 2:  # More than 2 hours left, wait for MAX
                        logger.info(f"Paid option available ({match.price_display}) but waiting for MAX...")
                        should_book = False
                
                if should_book and not self._booked:
                    result = await self._try_book(match)
                    
                    if result and result.is_success:
                        self._booked = result
                        
                        # Notify
                        for callback in self._on_booked:
                            try:
                                callback(result, match)
                            except Exception:
                                pass
                        
                        logger.info(f"✅ Booked: {result}")
                        
                        if stop_after_book:
                            return result
                    elif result:
                        logger.warning(f"Booking failed: {result.message}")
            else:
                logger.info("No matches found, will retry...")
            
            # Wait before next scan
            await asyncio.sleep(scan_interval)
        
        # Deadline approaching - book best available if any
        if not self._booked and auto_book:
            final_match = best_free or best_paid
            if final_match:
                logger.warning(f"Deadline imminent - booking best available: {final_match}")
                result = await self._try_book(final_match)
                if result and result.is_success:
                    self._booked = result
        
        logger.info("Deadline passed")
        return self._booked
    
    def run(self,
            constraint: DeadlineConstraint,
            auto_book: bool = True,
            scan_interval: int = 120,
            prefer_free: bool = True,
            broad_search: bool = True) -> Optional[BookingResult]:
        """Synchronous run."""
        return asyncio.run(self.run_async(
            constraint=constraint,
            auto_book=auto_book,
            scan_interval=scan_interval,
            prefer_free=prefer_free,
            broad_search=broad_search
        ))


# ==========================================================================
# CONVENIENCE FUNCTIONS
# ==========================================================================

def search_by_deadline(origin: str,
                       destination: str,
                       deadline: datetime,
                       include_priced: bool = True,
                       config: Optional[SNCFConfig] = None) -> List[DeadlineMatch]:
    """Quick search for trips arriving before a deadline.
    
    Args:
        origin: Departure city
        destination: Arrival city
        deadline: Must arrive before this time
        include_priced: Include paid alternatives when MAX unavailable
        
    Returns:
        List of matches sorted by preference
    """
    searcher = DeadlineSearcher(config=config)
    constraint = DeadlineConstraint(
        departure_city=origin,
        arrival_city=destination,
        deadline=deadline,
        include_priced=include_priced,
    )
    return searcher.search_broad(constraint)


def find_best_for_deadline(origin: str,
                           destination: str,
                           deadline: datetime,
                           prefer_free: bool = True,
                           config: Optional[SNCFConfig] = None) -> Optional[DeadlineMatch]:
    """Find the single best trip for a deadline.
    
    Args:
        origin: Departure city
        destination: Arrival city
        deadline: Must arrive before this time
        prefer_free: Prefer MAX options over paid
        
    Returns:
        Best match or None
    """
    matches = search_by_deadline(origin, destination, deadline, 
                                  include_priced=True, config=config)
    
    if not matches:
        return None
    
    if prefer_free:
        free_matches = [m for m in matches if m.is_free]
        if free_matches:
            return free_matches[0]
    
    return matches[0]


def book_for_deadline(origin: str,
                      destination: str,
                      deadline: datetime,
                      email: str,
                      password: str,
                      scan_until_booked: bool = True,
                      include_priced: bool = True,
                      prefer_free: bool = True,
                      config: Optional[SNCFConfig] = None) -> Optional[BookingResult]:
    """Book the best trip for a deadline.
    
    Args:
        origin: Departure city
        destination: Arrival city
        deadline: Must arrive before this time
        email: SNCF Connect email
        password: SNCF Connect password
        scan_until_booked: Keep scanning until a booking is made
        include_priced: Include paid alternatives if no MAX
        prefer_free: Wait for MAX even if paid is available
        
    Returns:
        BookingResult or None
    """
    booker = DeadlineBooker(email=email, password=password, config=config)
    constraint = DeadlineConstraint(
        departure_city=origin,
        arrival_city=destination,
        deadline=deadline,
        include_priced=include_priced,
    )
    
    if scan_until_booked:
        return booker.run(constraint, auto_book=True, prefer_free=prefer_free)
    else:
        # Just try once
        match = asyncio.run(booker.scan_once(constraint, broad_search=True))
        if match:
            result = asyncio.run(booker._try_book(match))
            return result
        return None


def get_all_options(origin: str,
                    destination: str,
                    deadline: datetime,
                    config: Optional[SNCFConfig] = None) -> dict:
    """Get all options with pricing summary.
    
    Returns a dict with:
    - matches: List of all DeadlineMatch objects
    - summary: Price summary dict
    - best_free: Best free option (or None)
    - best_paid: Best paid option (or None)
    """
    searcher = DeadlineSearcher(config=config)
    constraint = DeadlineConstraint(
        departure_city=origin,
        arrival_city=destination,
        deadline=deadline,
        include_priced=True,
    )
    
    matches = searcher.search_broad(constraint)
    summary = searcher.get_price_summary(matches)
    
    free_matches = [m for m in matches if m.is_free]
    paid_matches = [m for m in matches if not m.is_free]
    
    return {
        "matches": matches,
        "summary": summary,
        "best_free": free_matches[0] if free_matches else None,
        "best_paid": paid_matches[0] if paid_matches else None,
        "count": len(matches),
    }


