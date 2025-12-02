"""Data models for SNCF Max API."""

from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import Optional, List
from enum import Enum


class TripStatus(Enum):
    """Status of a trip."""
    AVAILABLE = "OUI"
    UNAVAILABLE = "NON"
    UNKNOWN = "UNKNOWN"


class BookingStatus(Enum):
    """Status of a booking attempt."""
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    NO_AVAILABILITY = "no_availability"
    AUTH_REQUIRED = "auth_required"
    ALREADY_BOOKED = "already_booked"
    MAX_BOOKINGS_REACHED = "max_bookings_reached"


@dataclass
class Station:
    """Represents a train station."""
    name: str
    code: Optional[str] = None
    city: Optional[str] = None
    
    def __str__(self) -> str:
        return self.name


@dataclass
class Trip:
    """Represents a train trip."""
    train_number: str
    origin: Station
    destination: Station
    departure_date: date
    departure_time: time
    arrival_time: time
    available_for_max: TripStatus = TripStatus.UNKNOWN
    axe: Optional[str] = None  # e.g., "SUD EST", "ATLANTIQUE"
    entity: Optional[str] = None  # e.g., "INOUI", "OUIGO"
    price_cents: Optional[int] = None  # Price in cents (e.g., 3500 = 35.00€)
    price_currency: str = "EUR"
    
    @property
    def price(self) -> Optional[float]:
        """Price in euros (or main currency unit)."""
        return self.price_cents / 100 if self.price_cents is not None else None
    
    @property
    def price_display(self) -> str:
        """Human readable price string."""
        if self.price_cents is None:
            return "N/A"
        return f"{self.price:.2f}€"
    
    @property
    def departure_datetime(self) -> datetime:
        return datetime.combine(self.departure_date, self.departure_time)
    
    @property
    def arrival_datetime(self) -> datetime:
        return datetime.combine(self.departure_date, self.arrival_time)
    
    def __str__(self) -> str:
        return (
            f"Train {self.train_number}: {self.origin} → {self.destination} "
            f"({self.departure_time.strftime('%H:%M')} - {self.arrival_time.strftime('%H:%M')})"
        )

    @classmethod
    def from_api_response(cls, data: dict) -> "Trip":
        """Create a Trip from API response data."""
        # Parse date (format: YYYY-MM-DD)
        dep_date = datetime.strptime(data.get("date", "1970-01-01"), "%Y-%m-%d").date()
        
        # Parse times (format: HH:MM)
        dep_time_str = data.get("heure_depart", "00:00")
        arr_time_str = data.get("heure_arrivee", "00:00")
        dep_time = datetime.strptime(dep_time_str, "%H:%M").time()
        arr_time = datetime.strptime(arr_time_str, "%H:%M").time()
        
        # Determine availability
        od_happy_card = data.get("od_happy_card", "").upper()
        if od_happy_card == "OUI":
            status = TripStatus.AVAILABLE
        elif od_happy_card == "NON":
            status = TripStatus.UNAVAILABLE
        else:
            status = TripStatus.UNKNOWN
        
        return cls(
            train_number=str(data.get("train_no", "")),
            origin=Station(name=data.get("origine", "")),
            destination=Station(name=data.get("destination", "")),
            departure_date=dep_date,
            departure_time=dep_time,
            arrival_time=arr_time,
            available_for_max=status,
            axe=data.get("axe"),
            entity=data.get("entity"),
        )


@dataclass
class BookingRequest:
    """Request to book a trip."""
    trip: Trip
    passenger_email: Optional[str] = None
    
    def __str__(self) -> str:
        return f"Booking request for {self.trip}"


@dataclass
class BookingResult:
    """Result of a booking attempt."""
    status: BookingStatus
    trip: Trip
    confirmation_number: Optional[str] = None
    message: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def is_success(self) -> bool:
        return self.status == BookingStatus.SUCCESS
    
    def __str__(self) -> str:
        if self.is_success:
            return f"✓ Booking confirmed: {self.confirmation_number}"
        return f"✗ Booking failed: {self.message}"


@dataclass
class SearchCriteria:
    """Criteria for searching trips."""
    origin: str
    destination: str
    date: Optional[date] = None
    departure_time_min: Optional[time] = None
    departure_time_max: Optional[time] = None
    only_available: bool = True
    
    def to_api_params(self) -> dict:
        """Convert to API query parameters."""
        params = {
            "origine": self.origin,
            "destination": self.destination,
        }
        if self.date:
            params["date"] = self.date.strftime("%Y-%m-%d")
        return params


@dataclass 
class UserCredentials:
    """SNCF Connect user credentials."""
    email: str
    password: str


@dataclass
class Session:
    """Authenticated session with SNCF Connect."""
    user_email: str
    cookies: dict = field(default_factory=dict)
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    
    @property
    def is_valid(self) -> bool:
        if not self.expires_at:
            return bool(self.cookies or self.access_token)
        return datetime.now() < self.expires_at
    
    def __str__(self) -> str:
        status = "valid" if self.is_valid else "expired"
        return f"Session({self.user_email}, {status})"

