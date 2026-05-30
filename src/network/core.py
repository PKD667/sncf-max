"""
core  --  the central algorithm of the TGV Max trip finder.

All the heavy work is delegated to small composable modules
(models, client, finder, decomposition).  This module is just
the *glue* that ties them together into a single pipeline.

The algorithm
=============

  Given (origin, destination, date):

  1. Fetch direct free trips via the public data.sncf.com API.
  2. If none (or we want more options), decompose the route:
     search for intermediate stations where both legs are MAX.
  3. Score and rank: free first, then by departure time.
  4. Optionally: auto-book the best option.

All functions below are synchronous and do not require
authentication (unless auto-booking).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Dict, Tuple

from models import Trip, BookingResult, BookingStatus, TripStatus
from config import SNCFConfig, default_config, get_station_name
from network.client import SNCFMaxClient
from network.decomposition import TripDecomposer, CompositeTrip
from network.finder import FreeTripFinder

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """Everything returned by a core search."""

    origin: str
    destination: str
    trip_date: date
    direct_free: List[Trip] = field(default_factory=list)
    direct_paid: List[Trip] = field(default_factory=list)
    decompositions: List[CompositeTrip] = field(default_factory=list)

    @property
    def best_free(self) -> Optional[Trip]:
        return self.direct_free[0] if self.direct_free else None

    @property
    def best_decomposed(self) -> Optional[CompositeTrip]:
        return self.decompositions[0] if self.decompositions else None

    @property
    def has_any_free(self) -> bool:
        return bool(self.direct_free or self.decompositions)

    @property
    def all_free(self) -> List[Trip]:
        """Flattened list of unique free trips (direct + legs)."""
        seen: set[str] = set()
        out: List[Trip] = []
        for t in self.direct_free:
            key = t.trip_key
            if key not in seen:
                seen.add(key)
                out.append(t)
        for c in self.decompositions:
            if not c.is_fully_free:
                continue
            for leg in c.legs:
                key = leg.trip.trip_key
                if key not in seen:
                    seen.add(key)
                    out.append(leg.trip)
        return sorted(out)

    def summary(self) -> str:
        lines: List[str] = []
        lines.append(f"Search:  {self.origin} -> {self.destination} on {self.trip_date}")
        lines.append(f"Direct free:  {len(self.direct_free)}")
        lines.append(f"Direct paid:  {len(self.direct_paid)}")
        n_free_decomp = sum(1 for c in self.decompositions if c.is_fully_free)
        n_paid_decomp = len(self.decompositions) - n_free_decomp
        lines.append(f"Decomposed:   {n_free_decomp} fully-MAX, {n_paid_decomp} with paid legs")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The algorithm
# ---------------------------------------------------------------------------


def search(
    origin: str,
    destination: str,
    trip_date: Optional[date] = None,
    *,
    decompose: bool = True,
    config: Optional[SNCFConfig] = None,
) -> SearchResult:
    """Core search algorithm: find free TGV Max trips for a route.

    Args:
        origin: Station alias or full name (e.g. 'paris', 'PARIS (intramuros)')
        destination: Station alias or full name
        trip_date: Date of travel. Defaults to tomorrow.
        decompose: Whether to search for multi-leg all-MAX combos.
        config: Optional SNCFConfig.

    Returns:
        SearchResult with direct_free, direct_paid, decompositions.

    Example:
        >>> result = search("paris", "lyon", date(2025, 6, 15))
        >>> print(result.summary())
        Search:  PARIS (intramuros) -> LYON (intramuros) on 2025-06-15
        Direct free:  3
        Direct paid:  12
        Decomposed:   2 fully-MAX, 5 with paid legs
    """
    # -- step 0: defaults ---------------------------------------------------
    cfg = config or default_config
    if trip_date is None:
        trip_date = date.today() + timedelta(days=1)

    client = SNCFMaxClient(config=cfg)
    origin_full = get_station_name(origin)
    dest_full = get_station_name(destination)

    # -- step 1: direct trips -----------------------------------------------
    free, paid = client.search_all_trips(
        origin=origin_full, destination=dest_full, trip_date=trip_date
    )
    free.sort()
    paid.sort()

    result = SearchResult(
        origin=origin_full,
        destination=dest_full,
        trip_date=trip_date,
        direct_free=free,
        direct_paid=paid,
    )

    # -- step 2: decompose into multi-leg combos (MAX + paid fallback) -----
    if decompose:
        decomposer = TripDecomposer(config=cfg)
        all_decomp = decomposer.find_alternatives(
            origin=origin_full, destination=dest_full, trip_date=trip_date,
            include_paid=True,
        )
        # keep only real multi-leg (>1 leg) — single-leg is just the direct trip
        result.decompositions = [c for c in all_decomp if len(c.legs) > 1]

    return result


def autobook(
    origin: str,
    destination: str,
    trip_date: date,
    email: str,
    password: str,
    config: Optional[SNCFConfig] = None,
) -> BookingResult:
    """Find and book the best free trip.  Requires credentials / playwright.

    Falls back to decomposed if no direct MAX trip is found.
    """
    result = search(origin, destination, trip_date, decompose=True, config=config)

    # -- pick the best option ------------------------------------------------
    trip_to_book: Optional[Trip] = None
    if result.best_free is not None:
        trip_to_book = result.best_free
    elif result.best_decomposed is not None:
        # Book first leg of best decomposed trip
        trip_to_book = result.best_decomposed.legs[0].trip
    else:
        return BookingResult(
            status=BookingStatus.NO_AVAILABILITY,
            trip=Trip(
                train_number="N/A",
                origin=result.origin,
                destination=result.destination,
                departure_date=trip_date,
                departure_time=time(0, 0),
                arrival_time=time(0, 0),
            ),
            message="No free trip available (MAX or decomposed)",
        )

    # -- authenticate & book -------------------------------------------------
    try:
        from booking.auth import load_or_login
        from booking.booking import book_sync

        session = load_or_login(email, password, config)
        return book_sync(trip_to_book, session=session, config=config or default_config)
    except ImportError:
        return BookingResult(
            status=BookingStatus.FAILED,
            trip=trip_to_book,
            message="Playwright not installed. Install with: pip install playwright",
        )


def broadcast(
    origin: str,
    trip_date: Optional[date] = None,
    config: Optional[SNCFConfig] = None,
) -> List[Trip]:
    """Find ALL free trips departing a station on a given date.

    This is the 'where can I go for free today?' query.
    """
    finder = FreeTripFinder(config=config)
    report = finder.find_all_from(origin=origin, trip_date=trip_date)
    return report.all_free_trips


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

__all__ = [
    "search",
    "autobook",
    "broadcast",
    "SearchResult",
]
