"""Integration with SNCF Connect "Mes Voyages" (My Trips) page.

This module handles:
- Fetching existing TGV Max bookings
- Cancelling trips
- Tracking the 6-trip limit
- Getting trip confirmations/e-tickets
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
import logging
import re

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = Any  # type: ignore
    Page = Any  # type: ignore
    BrowserContext = Any  # type: ignore

from models import Trip, Session, BookingStatus, Station
from config import SNCFConfig, default_config


logger = logging.getLogger(__name__)


# Maximum number of TGV Max trips allowed at once
MAX_TGVMAX_BOOKINGS = 6


class TripState(Enum):
    """State of a booked trip."""
    UPCOMING = "upcoming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class BookedTrip:
    """A booked TGV Max trip from Mes Voyages."""
    reference: str  # Booking reference (e.g., "ABCDEF")
    train_number: str
    origin: str
    destination: str
    departure_date: date
    departure_time: time
    arrival_time: time
    state: TripState = TripState.UPCOMING
    passenger_name: Optional[str] = None
    seat: Optional[str] = None  # e.g., "Car 12, Seat 45"
    e_ticket_url: Optional[str] = None
    
    @property
    def is_upcoming(self) -> bool:
        now = datetime.now()
        trip_dt = datetime.combine(self.departure_date, self.departure_time)
        return trip_dt > now and self.state == TripState.UPCOMING
    
    @property
    def is_cancellable(self) -> bool:
        """TGV Max trips can be cancelled up to departure."""
        return self.is_upcoming
    
    def to_trip(self) -> Trip:
        """Convert to a Trip object."""
        return Trip(
            train_number=self.train_number,
            origin=Station(name=self.origin),
            destination=Station(name=self.destination),
            departure_date=self.departure_date,
            departure_time=self.departure_time,
            arrival_time=self.arrival_time,
        )
    
    def __str__(self) -> str:
        return (
            f"{self.reference} | Train {self.train_number}: "
            f"{self.origin} → {self.destination} "
            f"({self.departure_date} {self.departure_time.strftime('%H:%M')})"
        )


@dataclass
class VoyagesStatus:
    """Status of the user's TGV Max bookings."""
    trips: List[BookedTrip]
    upcoming_count: int
    slots_available: int
    can_book_more: bool
    
    @classmethod
    def from_trips(cls, trips: List[BookedTrip]) -> "VoyagesStatus":
        upcoming = [t for t in trips if t.is_upcoming]
        upcoming_count = len(upcoming)
        slots_available = MAX_TGVMAX_BOOKINGS - upcoming_count
        
        return cls(
            trips=trips,
            upcoming_count=upcoming_count,
            slots_available=slots_available,
            can_book_more=slots_available > 0,
        )


class MesVoyagesClient:
    """Client for interacting with the SNCF Connect Mes Voyages page.
    
    Uses Playwright to scrape and interact with the user's bookings.
    """
    
    # URLs
    HOME_URL = "https://www.sncf-connect.com/app/home/"
    TRIPS_URL = "https://www.sncf-connect.com/trips"
    
    # Selectors
    SELECTORS = {
        # Trip cards
        "trip_card": '[data-testid="trip-card"], .travel-card, .journey-summary',
        "trip_reference": '.reference, .booking-reference, [class*="reference"]',
        "trip_train": '.train-number, [class*="train"]',
        "trip_origin": '.origin, .departure-station, [class*="departure"]',
        "trip_destination": '.destination, .arrival-station, [class*="arrival"]',
        "trip_date": '.date, [class*="date"]',
        "trip_time": '.time, .departure-time, [class*="time"]',
        
        # Actions
        "cancel_button": 'button:has-text("Annuler"), button:has-text("Supprimer")',
        "confirm_cancel": 'button:has-text("Confirmer"), button:has-text("Oui")',
        "download_ticket": 'button:has-text("Télécharger"), a:has-text("E-billet")',
        
        # Status
        "upcoming_trips": '[data-testid="upcoming-travels"], .upcoming',
        "past_trips": '[data-testid="past-travels"], .past',
        
        # Login required
        "login_prompt": 'a[href*="login"], button:has-text("Se connecter")',
    }
    
    def __init__(self,
                 session: Optional[Session] = None,
                 config: Optional[SNCFConfig] = None):
        """Initialize the client.
        
        Args:
            session: Authenticated session
            config: Optional configuration
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("Playwright is required. Install with: pip install playwright")
        
        self.config = config or default_config
        self._session = session
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._playwright = None
        self._cached_status: Optional[VoyagesStatus] = None
        self._cache_time: Optional[datetime] = None
    
    async def __aenter__(self):
        await self._start_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _start_browser(self) -> None:
        """Start browser with session cookies."""
        self._playwright = await async_playwright().start()
        
        launch_options = {
            "headless": self.config.HEADLESS,
        }
        
        # Add proxy if configured
        if hasattr(self.config, 'PROXY') and self.config.PROXY:
            launch_options["proxy"] = {"server": self.config.PROXY}
        
        self._browser = await self._playwright.chromium.launch(**launch_options)
        
        self._context = await self._browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent=(
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
            locale='fr-FR',
        )
        
        # Apply session cookies. If some are rejected, log them but continue
        # with the others. Handle "__Secure-"/"__Host-" cookie prefixes.
        if self._session and self._session.cookies:
            cookies = []
            for name, value in self._session.cookies.items():
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": ".sncf-connect.com",
                    "path": "/",
                }
                if name.startswith("__Secure-") or name.startswith("__Host-"):
                    cookie["secure"] = True
                if name.startswith("__Host-"):
                    cookie.pop("domain", None)
                cookies.append(cookie)
            try:
                await self._context.add_cookies(cookies)
            except Exception as exc:
                if self.config.DEBUG:
                    logger.warning("Error adding cookies in MesVoyages context: %s", exc)
                    logger.warning("Retrying cookies one by one to find the problematic ones...")
                for c in cookies:
                    try:
                        await self._context.add_cookies([c])
                    except Exception as e:
                        logger.warning("Cookie rejected in MesVoyages context: %s -> %s", c["name"], e)
    
    async def close(self) -> None:
        """Close browser."""
        try:
            if self._context:
                await self._context.close()
        except Exception as exc:
            if self.config.DEBUG:
                logger.warning("Error while closing MesVoyages browser context: %s", exc)
        try:
            if self._browser:
                await self._browser.close()
        except Exception as exc:
            if self.config.DEBUG:
                logger.warning("Error while closing MesVoyages browser: %s", exc)
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            if self.config.DEBUG:
                logger.warning("Error while stopping Playwright in MesVoyages client: %s", exc)
    
    async def _take_screenshot(self, page: Page, name: str) -> None:
        """Take debug screenshot."""
        if self.config.DEBUG:
            self.config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = self.config.SCREENSHOTS_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
            await page.screenshot(path=str(path))
    
    async def fetch_trips(self, force_refresh: bool = False) -> VoyagesStatus:
        """Fetch all booked trips from Mes Voyages.
        
        Args:
            force_refresh: Bypass cache
            
        Returns:
            VoyagesStatus with all trips
        """
        # Check cache
        if not force_refresh and self._cached_status and self._cache_time:
            age = (datetime.now() - self._cache_time).total_seconds()
            if age < 300:  # 5 min cache
                return self._cached_status
        
        if not self._browser:
            await self._start_browser()
        
        page = await self._context.new_page()
        
        try:
            await page.goto(self.HOME_URL, timeout=self.config.BROWSER_TIMEOUT)
            await page.goto(self.TRIPS_URL, timeout=self.config.BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle")
            
            # Check if login is required
            login_btn = page.locator(self.SELECTORS["login_prompt"])
            if await login_btn.count() > 0:
                raise PermissionError("Not authenticated. Please log in first.")
            
            await self._take_screenshot(page, "mes_voyages")
            
            # Extract trips
            trips = []
            trip_cards = page.locator(self.SELECTORS["trip_card"])
            
            for i in range(await trip_cards.count()):
                card = trip_cards.nth(i)
                
                try:
                    trip = await self._parse_trip_card(card)
                    if trip:
                        trips.append(trip)
                except Exception as e:
                    logger.warning(f"Failed to parse trip card: {e}")
            
            status = VoyagesStatus.from_trips(trips)
            self._cached_status = status
            self._cache_time = datetime.now()
            
            logger.info(f"Fetched {len(trips)} trips ({status.upcoming_count} upcoming)")
            return status
            
        finally:
            await page.close()
    
    async def _parse_trip_card(self, card) -> Optional[BookedTrip]:
        """Parse a trip card element into a BookedTrip."""
        try:
            text = await card.text_content()
            
            # Extract reference
            ref_el = card.locator(self.SELECTORS["trip_reference"])
            reference = ""
            if await ref_el.count() > 0:
                reference = (await ref_el.first.text_content()).strip()
            
            # Try to extract from text with regex
            if not reference:
                ref_match = re.search(r'\b([A-Z]{6})\b', text)
                if ref_match:
                    reference = ref_match.group(1)
            
            # Extract train number
            train_match = re.search(r'(?:Train|TGV|INOUI)\s*(?:n°)?\s*(\d+)', text, re.I)
            train_number = train_match.group(1) if train_match else "N/A"
            
            # Extract date
            date_match = re.search(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})', text)
            if date_match:
                day, month, year = date_match.groups()
                departure_date = date(int(year), int(month), int(day))
            else:
                # Try other formats
                departure_date = date.today()
            
            # Extract times
            time_matches = re.findall(r'(\d{1,2})[h:](\d{2})', text)
            if len(time_matches) >= 2:
                dep_h, dep_m = time_matches[0]
                arr_h, arr_m = time_matches[1]
                departure_time = time(int(dep_h), int(dep_m))
                arrival_time = time(int(arr_h), int(arr_m))
            else:
                departure_time = time(0, 0)
                arrival_time = time(0, 0)
            
            # Extract stations (harder, use selectors)
            origin_el = card.locator(self.SELECTORS["trip_origin"])
            destination_el = card.locator(self.SELECTORS["trip_destination"])
            
            origin = "Unknown"
            destination = "Unknown"
            
            if await origin_el.count() > 0:
                origin = (await origin_el.first.text_content()).strip()
            if await destination_el.count() > 0:
                destination = (await destination_el.first.text_content()).strip()
            
            # Determine state
            now = datetime.now()
            trip_dt = datetime.combine(departure_date, departure_time)
            state = TripState.UPCOMING if trip_dt > now else TripState.COMPLETED
            
            return BookedTrip(
                reference=reference or f"TEMP{hash(text) % 10000:04d}",
                train_number=train_number,
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                departure_time=departure_time,
                arrival_time=arrival_time,
                state=state,
            )
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None
    
    async def cancel_trip(self, reference: str) -> bool:
        """Cancel a booked trip.
        
        Args:
            reference: Booking reference code
            
        Returns:
            True if cancellation was successful
        """
        if not self._browser:
            await self._start_browser()
        
        page = await self._context.new_page()
        
        try:
            await page.goto(self.MES_VOYAGES_URL)
            await page.wait_for_load_state("networkidle")
            
            # Find the trip card with this reference
            trip_card = page.locator(f':has-text("{reference}")').first
            
            if not await trip_card.count():
                logger.error(f"Trip {reference} not found")
                return False
            
            # Click cancel button
            cancel_btn = trip_card.locator(self.SELECTORS["cancel_button"])
            if not await cancel_btn.count():
                logger.error(f"Cancel button not found for {reference}")
                return False
            
            await cancel_btn.click()
            await page.wait_for_timeout(1000)
            
            # Confirm cancellation
            confirm_btn = page.locator(self.SELECTORS["confirm_cancel"])
            if await confirm_btn.count() > 0:
                await confirm_btn.click()
                await page.wait_for_load_state("networkidle")
            
            await self._take_screenshot(page, f"cancel_{reference}")
            
            # Invalidate cache
            self._cached_status = None
            
            logger.info(f"Cancelled trip {reference}")
            return True
            
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            await self._take_screenshot(page, "cancel_error")
            return False
        finally:
            await page.close()
    
    async def get_slots_available(self) -> int:
        """Get number of available booking slots."""
        status = await self.fetch_trips()
        return status.slots_available
    
    async def can_book(self) -> bool:
        """Check if we can book more trips."""
        status = await self.fetch_trips()
        return status.can_book_more
    
    def get_slots_available_sync(self) -> int:
        """Synchronous version."""
        async def _get():
            async with MesVoyagesClient(self._session, self.config) as client:
                return await client.get_slots_available()
        return asyncio.run(_get())


def fetch_my_trips_sync(session: Session, config: Optional[SNCFConfig] = None) -> VoyagesStatus:
    """Synchronous helper to fetch trips."""
    async def _fetch():
        async with MesVoyagesClient(session, config) as client:
            return await client.fetch_trips()
    return asyncio.run(_fetch())


def cancel_trip_sync(session: Session, reference: str, config: Optional[SNCFConfig] = None) -> bool:
    """Synchronous helper to cancel a trip."""
    async def _cancel():
        async with MesVoyagesClient(session, config) as client:
            return await client.cancel_trip(reference)
    return asyncio.run(_cancel())

