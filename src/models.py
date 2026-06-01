"""Data models for SNCF Max API."""

from dataclasses import dataclass, field, replace
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum


class TripStatus(Enum):
    AVAILABLE = "OUI"
    UNAVAILABLE = "NON"
    UNKNOWN = "UNKNOWN"


class BookingStatus(Enum):
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

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Station):
            return self.name.lower() == other.name.lower()
        if isinstance(other, str):
            return self.name.lower() == other.lower()
        return False

    def __hash__(self) -> int:
        return hash(self.name.lower())


@dataclass
class Trip:
    """Represents a train trip with TGV Max and pricing information."""
    train_number: str
    origin: Station
    destination: Station
    departure_date: date
    departure_time: time
    arrival_time: time
    available_for_max: TripStatus = TripStatus.UNKNOWN
    axe: Optional[str] = None
    entity: Optional[str] = None
    price_cents: Optional[int] = None
    price_currency: str = "EUR"
    _raw: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @property
    def is_free(self) -> bool:
        """True if this trip can be booked for FREE with TGV Max.

        A trip is free when:
        1. od_happy_card == 'OUI' (a MAX seat is available), AND
        2. The train is operated by INOUI (OUIGO is sometimes not MAX-eligible)
        """
        if self.available_for_max != TripStatus.AVAILABLE:
            return False
        if self.entity and self.entity.upper() == "OUIGO":
            return False
        return True

    @property
    def carrier(self) -> str:
        """Operator class, for fare resolution: TGV / OUIGO / INTERCITES / TER.

        Derived from the dataset's ``entity``.  TGV Max records are TGV/IC;
        TER and regional carriers arrive once their timetables are ingested,
        and will set this explicitly."""
        e = (self.entity or "").upper()
        if "OUIGO" in e:
            return "OUIGO"
        if "INTERCIT" in e or e == "IC":          # check before TER ("inTERcites")
            return "INTERCITES"
        if "TER" in e or "NAVETTE" in e or "TRAMTRAIN" in e:
            return "TER"                            # "Train TER", "Car TER", navettes
        return "TGV"

    @property
    def is_max(self) -> bool:
        return self.is_free

    @property
    def is_paid(self) -> bool:
        return not self.is_free

    @property
    def price(self) -> Optional[float]:
        return self.price_cents / 100 if self.price_cents is not None else None

    @property
    def price_display(self) -> str:
        if self.is_free:
            return "MAX (0EUR)"
        if self.price_cents is None:
            return "Price unknown"
        return f"{self.price:.2f}EUR"

    @property
    def duration(self) -> timedelta:
        dep = datetime.combine(self.departure_date, self.departure_time)
        arr = datetime.combine(self.departure_date, self.arrival_time)
        if arr < dep:
            arr += timedelta(days=1)
        return arr - dep

    @property
    def departure_datetime(self) -> datetime:
        return datetime.combine(self.departure_date, self.departure_time)

    @property
    def arrival_datetime(self) -> datetime:
        dt = datetime.combine(self.departure_date, self.arrival_time)
        if self.arrival_time < self.departure_time:
            dt += timedelta(days=1)
        return dt

    @property
    def trip_key(self) -> str:
        """Unique key for deduplication."""
        return f"{self.train_number}:{self.departure_date}:{self.origin}:{self.destination}"

    def __str__(self) -> str:
        flag = " [MAX]" if self.is_free else f" [{self.price_display}]"
        return (
            f"Train {self.train_number}: {self.origin} -> {self.destination} "
            f"({self.departure_time.strftime('%H:%M')} - {self.arrival_time.strftime('%H:%M')}){flag}"
        )

    def __lt__(self, other: "Trip") -> bool:
        if self.departure_date != other.departure_date:
            return self.departure_date < other.departure_date
        return self.departure_time < other.departure_time

    def with_stations_resolved(self, station_map: Dict[str, str]) -> "Trip":
        """Return a copy with alias-based station names resolved."""
        return replace(
            self,
            origin=Station(name=station_map.get(str(self.origin), str(self.origin))),
            destination=Station(name=station_map.get(str(self.destination), str(self.destination))),
        )

    @classmethod
    def from_api_response(cls, data: dict) -> "Trip":
        """Create a Trip from API response data.

        The SNCF data API (data.sncf.com) returns records with fields like:
          - date: YYYY-MM-DD
          - heure_depart: HH:MM departure
          - heure_arrivee: HH:MM arrival
          - train_no: train number string
          - origine / destination: station names
          - axe: route axis (e.g. 'SUD EST', 'ATLANTIQUE')
          - entity: operator ('INOUI', 'OUIGO')
          - od_happy_card: 'OUI' or 'NON' for TGV Max availability
          - prix_2nde / prix / amount: actual ticket price when present
        """
        dep_date = datetime.strptime(data.get("date", "1970-01-01"), "%Y-%m-%d").date()

        dep_time_str = data.get("heure_depart", "00:00")
        arr_time_str = data.get("heure_arrivee", "00:00")
        dep_time = datetime.strptime(dep_time_str, "%H:%M").time()
        arr_time = datetime.strptime(arr_time_str, "%H:%M").time()

        od_happy_card = data.get("od_happy_card", "").upper()
        if od_happy_card == "OUI":
            status = TripStatus.AVAILABLE
        elif od_happy_card == "NON":
            status = TripStatus.UNAVAILABLE
        else:
            status = TripStatus.UNKNOWN

        # Try multiple possible price field names from the API
        price = None
        for field in ("prix_2nde", "prix", "amount", "prix_voyageur", "prix_total"):
            val = data.get(field)
            if val is not None:
                try:
                    price = int(float(str(val)) * 100)
                    break
                except (ValueError, TypeError):
                    pass

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
            price_cents=price,
            _raw=data,
        )

    def to_dict(self) -> dict:
        return {
            "train_number": self.train_number,
            "origin": str(self.origin),
            "destination": str(self.destination),
            "departure_date": self.departure_date.isoformat(),
            "departure_time": self.departure_time.strftime("%H:%M"),
            "arrival_time": self.arrival_time.strftime("%H:%M"),
            "available_for_max": self.available_for_max.value,
            "axe": self.axe,
            "entity": self.entity,
            "price_cents": self.price_cents,
            "is_free": self.is_free,
        }


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
            return f"OK Booking confirmed: {self.confirmation_number}"
        return f"X Booking failed: {self.message}"


@dataclass
class SearchCriteria:
    """Criteria for searching trips."""
    origin: str
    destination: str
    date: Optional[date] = None
    departure_time_min: Optional[time] = None
    departure_time_max: Optional[time] = None
    only_available: bool = True
    max_price_cents: Optional[int] = None

    def matches(self, trip: Trip) -> bool:
        if self.only_available and trip.available_for_max != TripStatus.AVAILABLE:
            return False
        if self.departure_time_min and trip.departure_time < self.departure_time_min:
            return False
        if self.departure_time_max and trip.departure_time > self.departure_time_max:
            return False
        if self.max_price_cents is not None and trip.price_cents is not None:
            if trip.price_cents > self.max_price_cents:
                return False
        return True

    def to_api_params(self) -> dict:
        params: Dict[str, Any] = {
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
