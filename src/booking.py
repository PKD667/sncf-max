"""Booking automation for SNCF Connect using Playwright."""

from __future__ import annotations

import asyncio
from typing import Optional, List, Any
from datetime import datetime, date
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = Any  # type: ignore
    Page = Any  # type: ignore
    BrowserContext = Any  # type: ignore

from models import (
    Trip, BookingRequest, BookingResult, BookingStatus, 
    Session, UserCredentials
)
from config import SNCFConfig, default_config, get_station_name
from auth import SNCFAuthenticator, AuthenticationError


class BookingError(Exception):
    """Raised when a booking operation fails."""
    pass


class SNCFBookingClient:
    """Client for booking TGV Max trips on SNCF Connect.
    
    Uses browser automation to interact with the SNCF Connect website
    since there's no public booking API.
    """
    
    # UI Selectors for the booking flow
    SELECTORS = {
        # Search form
        "origin_input": 'input[placeholder*="Départ"], input[aria-label*="départ"], #origin-input',
        "destination_input": 'input[placeholder*="Arrivée"], input[aria-label*="arrivée"], #destination-input',
        "date_input": 'input[type="date"], button[aria-label*="date"], .date-picker-trigger',
        "search_button": 'button[type="submit"], button:has-text("Rechercher")',
        
        # Search results
        "trip_card": '.journey-card, .travel-proposal, [data-testid*="journey"]',
        "trip_time": '.departure-time, .time, [class*="time"]',
        "trip_price": '.price, [class*="price"]',
        "tgvmax_badge": '.tgvmax, .max-badge, :has-text("MAX")',
        "select_trip": 'button:has-text("Choisir"), button:has-text("Sélectionner")',
        
        # Booking confirmation
        "passenger_form": '.passenger-form, [data-testid="passenger"]',
        "confirm_button": 'button:has-text("Confirmer"), button:has-text("Valider")',
        "finalize_button": 'button:has-text("Finaliser"), button:has-text("Payer"), button:has-text("Réserver")',
        
        # Success/Error
        "booking_success": '.confirmation, .success, [class*="success"], [class*="confirmation"]',
        "booking_reference": '.reference, .booking-number, [class*="reference"]',
        "error_message": '[class*="error"], [role="alert"], .error',
        
        # Cookie consent
        "cookie_accept": 'button[id*="accept"], button:has-text("Accepter")',
    }
    
    def __init__(self, 
                 session: Optional[Session] = None,
                 config: Optional[SNCFConfig] = None):
        """Initialize the booking client.
        
        Args:
            session: Authenticated session (will attempt to load/create if not provided)
            config: Optional configuration
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright is required for booking. "
                "Install with: pip install playwright && playwright install chromium"
            )
        
        self.config = config or default_config
        self._session = session
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._playwright = None
    
    async def __aenter__(self):
        await self._start_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _start_browser(self) -> None:
        """Start the browser with session cookies."""
        self._playwright = await async_playwright().start()
        
        # Use Firefox by default for better bot evasion (DataDome protection)
        self._browser = await self._playwright.firefox.launch(
            headless=self.config.HEADLESS,
            slow_mo=self.config.SLOW_MO if self.config.DEBUG else 50,
        )
        
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
            locale='fr-FR',
            timezone_id='Europe/Paris',
        )
        
        # Apply session cookies if available. If some are rejected, log them in
        # DEBUG mode but still apply the rest. Handle "__Secure-"/"__Host-" prefixes.
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
                    print(f"Error adding cookies in booking context: {exc}")
                    print("Retrying cookies one by one to find the problematic ones...")
                for c in cookies:
                    try:
                        await self._context.add_cookies([c])
                    except Exception as e:
                        if self.config.DEBUG:
                            print(f"Cookie rejected in booking context: {c['name']} -> {e}")
    
    async def close(self) -> None:
        """Close the browser."""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while closing booking browser context.")
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while closing booking browser.")
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while stopping Playwright in booking client.")
    
    async def _take_screenshot(self, page: Page, name: str) -> Path:
        """Take a debug screenshot."""
        self.config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.config.SCREENSHOTS_DIR / f"{name}_{timestamp}.png"
        await page.screenshot(path=str(path))
        if self.config.DEBUG:
            print(f"Screenshot saved: {path}")
        return path
    
    async def _handle_cookie_consent(self, page: Page) -> None:
        """Handle cookie consent popup."""
        try:
            cookie_btn = page.locator(self.SELECTORS["cookie_accept"])
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click(timeout=5000)
                await page.wait_for_timeout(1000)
        except Exception:
            pass
    
    async def _ensure_authenticated(self, page: Page) -> bool:
        """Ensure we're logged in, return False if not."""
        # Check for user menu or similar logged-in indicator
        # This is heuristic - SNCF Connect's UI changes
        try:
            await page.goto(self.config.SNCF_CONNECT_BASE_URL)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await self._handle_cookie_consent(page)
            
            # Look for account/profile elements
            account_el = page.locator('[data-testid="user-menu"], [class*="account"], .user-menu')
            if await account_el.count() > 0:
                return True
            
            # Also check URL for account indicators
            if "/account" in page.url or "/profil" in page.url:
                return True
            
            return False
        except Exception:
            return False
    
    async def _select_station(self, page: Page, selector: str, station_name: str) -> None:
        """Select a station in an autocomplete input."""
        input_el = page.locator(selector).first
        await input_el.click()
        await page.wait_for_timeout(300)
        
        # Clear and type station name
        await input_el.fill("")
        await input_el.type(station_name, delay=50)
        await page.wait_for_timeout(1000)
        
        # Wait for and select from autocomplete
        suggestion = page.locator(f'.suggestion:has-text("{station_name}"), [role="option"]:has-text("{station_name}")')
        try:
            await suggestion.first.click(timeout=5000)
        except Exception:
            # Try pressing Enter if no visible suggestion
            await input_el.press("Enter")
    
    async def _select_date(self, page: Page, trip_date: date) -> None:
        """Select a date in the date picker."""
        date_trigger = page.locator(self.SELECTORS["date_input"]).first
        await date_trigger.click()
        await page.wait_for_timeout(500)
        
        # Format: DD/MM/YYYY or click on calendar
        formatted_date = trip_date.strftime("%d/%m/%Y")
        
        # Try to find date input and fill
        date_input = page.locator('input[type="date"], input[placeholder*="date"]')
        if await date_input.count() > 0:
            await date_input.fill(trip_date.strftime("%Y-%m-%d"))
        else:
            # Try clicking on calendar day
            day_selector = f'[aria-label*="{trip_date.day}"], .day:has-text("{trip_date.day}")'
            day_el = page.locator(day_selector)
            if await day_el.count() > 0:
                await day_el.first.click()
    
    async def search_and_book(self, trip: Trip) -> BookingResult:
        """Search for a specific trip and book it.
        
        Args:
            trip: The trip to book
            
        Returns:
            BookingResult with status and details
        """
        if not self._browser:
            await self._start_browser()
        
        page = await self._context.new_page()
        
        try:
            # Navigate to search page
            await page.goto(self.config.SNCF_CONNECT_SEARCH_URL)
            await page.wait_for_load_state("networkidle", timeout=self.config.BROWSER_TIMEOUT)
            await self._handle_cookie_consent(page)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "search_page")
            
            # Check if authenticated
            if not await self._ensure_authenticated(page):
                return BookingResult(
                    status=BookingStatus.AUTH_REQUIRED,
                    trip=trip,
                    message="Not authenticated. Please log in first."
                )
            
            # Fill in search form
            origin_name = trip.origin.name if hasattr(trip.origin, 'name') else str(trip.origin)
            dest_name = trip.destination.name if hasattr(trip.destination, 'name') else str(trip.destination)
            
            await self._select_station(page, self.SELECTORS["origin_input"], origin_name)
            await page.wait_for_timeout(500)
            
            await self._select_station(page, self.SELECTORS["destination_input"], dest_name)
            await page.wait_for_timeout(500)
            
            await self._select_date(page, trip.departure_date)
            await page.wait_for_timeout(500)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "search_filled")
            
            # Submit search
            search_btn = page.locator(self.SELECTORS["search_button"]).first
            await search_btn.click()
            
            # Wait for results
            await page.wait_for_load_state("networkidle", timeout=self.config.BROWSER_TIMEOUT)
            await page.wait_for_timeout(2000)  # Extra time for results to load
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "search_results")
            
            # Find the specific trip by time
            target_time = trip.departure_time.strftime("%H:%M")
            trip_cards = page.locator(self.SELECTORS["trip_card"])
            
            found_trip = None
            for i in range(await trip_cards.count()):
                card = trip_cards.nth(i)
                card_text = await card.text_content()
                
                # Check if this is our trip (match by departure time)
                if target_time in card_text:
                    # Check if TGV Max is available
                    max_badge = card.locator(self.SELECTORS["tgvmax_badge"])
                    if await max_badge.count() > 0:
                        found_trip = card
                        break
            
            if not found_trip:
                return BookingResult(
                    status=BookingStatus.NO_AVAILABILITY,
                    trip=trip,
                    message=f"No TGV Max availability found for {target_time} trip"
                )
            
            # Select the trip
            select_btn = found_trip.locator(self.SELECTORS["select_trip"])
            await select_btn.click()
            await page.wait_for_load_state("networkidle", timeout=self.config.BROWSER_TIMEOUT)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "trip_selected")
            
            # Handle passenger selection/confirmation
            # The passenger should be pre-filled for logged-in users
            await page.wait_for_timeout(2000)
            
            # Look for confirm/continue button
            confirm_btn = page.locator(self.SELECTORS["confirm_button"])
            if await confirm_btn.count() > 0:
                await confirm_btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=self.config.BROWSER_TIMEOUT)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "before_finalize")
            
            # Finalize booking
            finalize_btn = page.locator(self.SELECTORS["finalize_button"])
            if await finalize_btn.count() > 0:
                await finalize_btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=self.config.BROWSER_TIMEOUT)
                await page.wait_for_timeout(3000)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "after_finalize")
            
            # Check for success
            success_el = page.locator(self.SELECTORS["booking_success"])
            if await success_el.count() > 0:
                # Try to extract confirmation number
                ref_el = page.locator(self.SELECTORS["booking_reference"])
                confirmation = None
                if await ref_el.count() > 0:
                    confirmation = await ref_el.first.text_content()
                
                await self._take_screenshot(page, "booking_success")
                
                return BookingResult(
                    status=BookingStatus.SUCCESS,
                    trip=trip,
                    confirmation_number=confirmation,
                    message="Booking successful!"
                )
            
            # Check for errors
            error_el = page.locator(self.SELECTORS["error_message"])
            if await error_el.count() > 0:
                error_text = await error_el.first.text_content()
                
                # Check for specific error types
                if "maximum" in error_text.lower() or "limite" in error_text.lower():
                    return BookingResult(
                        status=BookingStatus.MAX_BOOKINGS_REACHED,
                        trip=trip,
                        message=error_text
                    )
                elif "déjà" in error_text.lower():
                    return BookingResult(
                        status=BookingStatus.ALREADY_BOOKED,
                        trip=trip,
                        message=error_text
                    )
                
                return BookingResult(
                    status=BookingStatus.FAILED,
                    trip=trip,
                    message=error_text
                )
            
            # Unknown state
            await self._take_screenshot(page, "unknown_state")
            return BookingResult(
                status=BookingStatus.FAILED,
                trip=trip,
                message="Booking status unclear - please check your account manually"
            )
            
        except Exception as e:
            await self._take_screenshot(page, "error")
            return BookingResult(
                status=BookingStatus.FAILED,
                trip=trip,
                message=str(e)
            )
        finally:
            await page.close()
    
    async def book_trip(self, trip: Trip) -> BookingResult:
        """Book a trip. Alias for search_and_book."""
        return await self.search_and_book(trip)
    
    async def book_multiple(self, trips: List[Trip]) -> List[BookingResult]:
        """Book multiple trips sequentially.
        
        Args:
            trips: List of trips to book
            
        Returns:
            List of BookingResults
        """
        results = []
        for trip in trips:
            result = await self.book_trip(trip)
            results.append(result)
            
            # Stop if we hit booking limits
            if result.status == BookingStatus.MAX_BOOKINGS_REACHED:
                for remaining_trip in trips[len(results):]:
                    results.append(BookingResult(
                        status=BookingStatus.MAX_BOOKINGS_REACHED,
                        trip=remaining_trip,
                        message="Skipped - booking limit reached"
                    ))
                break
            
            # Small delay between bookings
            await asyncio.sleep(2)
        
        return results


def book_sync(trip: Trip, 
              session: Optional[Session] = None,
              config: Optional[SNCFConfig] = None) -> BookingResult:
    """Synchronous wrapper for booking a trip.
    
    Args:
        trip: Trip to book
        session: Authenticated session
        config: Optional configuration
        
    Returns:
        BookingResult
    """
    async def _book():
        async with SNCFBookingClient(session=session, config=config) as client:
            return await client.book_trip(trip)
    
    return asyncio.run(_book())


async def auto_book(
    origin: str,
    destination: str,
    trip_date: date,
    credentials: Optional[UserCredentials] = None,
    preferred_time: Optional[str] = None,
    config: Optional[SNCFConfig] = None
) -> BookingResult:
    """Automatically search and book a TGV Max trip.
    
    This is a high-level convenience function that:
    1. Searches for available trips
    2. Authenticates if needed
    3. Books the best available trip
    
    Args:
        origin: Origin station
        destination: Destination station
        trip_date: Date of travel
        credentials: SNCF Connect credentials (uses config if not provided)
        preferred_time: Preferred departure time (HH:MM)
        config: Optional configuration
        
    Returns:
        BookingResult
    """
    from .client import SNCFMaxClient
    
    config = config or default_config
    
    # First, check availability using public API
    client = SNCFMaxClient(config=config)
    trips = client.search_trips(
        origin=origin,
        destination=destination,
        trip_date=trip_date,
        only_available=True
    )
    
    if not trips:
        return BookingResult(
            status=BookingStatus.NO_AVAILABILITY,
            trip=Trip(
                train_number="N/A",
                origin=origin,
                destination=destination,
                departure_date=trip_date,
                departure_time=datetime.min.time(),
                arrival_time=datetime.min.time(),
            ),
            message="No TGV Max trips available for this route and date"
        )
    
    # Select best trip (earliest by default, or closest to preferred time)
    selected_trip = trips[0]
    if preferred_time:
        target = datetime.strptime(preferred_time, "%H:%M").time()
        selected_trip = min(
            trips, 
            key=lambda t: abs(
                datetime.combine(trip_date, t.departure_time) - 
                datetime.combine(trip_date, target)
            ).total_seconds()
        )
    
    # Authenticate
    if not credentials:
        if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
            return BookingResult(
                status=BookingStatus.AUTH_REQUIRED,
                trip=selected_trip,
                message="Credentials required. Set SNCF_EMAIL and SNCF_PASSWORD env vars."
            )
        credentials = UserCredentials(
            email=config.SNCF_EMAIL,
            password=config.SNCF_PASSWORD
        )
    
    # Login
    async with SNCFAuthenticator(config) as auth:
        try:
            session = await auth.login(credentials)
        except AuthenticationError as e:
            return BookingResult(
                status=BookingStatus.AUTH_REQUIRED,
                trip=selected_trip,
                message=str(e)
            )
    
    # Book
    async with SNCFBookingClient(session=session, config=config) as booking_client:
        return await booking_client.book_trip(selected_trip)

