"""Client for the SNCF Max public API - Trip Discovery."""

import requests
from typing import Dict, List, Any, Optional, Iterator
from urllib.parse import urljoin
from datetime import date, datetime, timedelta
import time

from models import Trip, Station, TripStatus, SearchCriteria
from config import SNCFConfig, default_config, get_station_name


class SNCFMaxClient:
    """Client for the SNCF Max public API (trip discovery).
    
    This client queries the public data.sncf.com API to find TGV Max eligible trips.
    For booking, see the SNCFBookingClient.
    """
    
    def __init__(self, config: Optional[SNCFConfig] = None):
        """Initialize the SNCF Max client.
        
        Args:
            config: Optional configuration object
        """
        self.config = config or default_config
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://data.sncf.com/explore/dataset/tgvmax/api/',
            'DNT': '1',
            'Connection': 'keep-alive',
        })
        self._last_request_time = 0.0
    
    def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.config.REQUEST_DELAY:
            time.sleep(self.config.REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()
    
    def _build_where_clause(self, 
                            origin: Optional[str] = None,
                            destination: Optional[str] = None,
                            trip_date: Optional[date] = None,
                            only_available: bool = True,
                            **extra_filters) -> str:
        """Build the WHERE clause for the API query."""
        conditions = []
        
        if origin:
            station = get_station_name(origin)
            conditions.append(f'origine="{station}"')
            
        if destination:
            station = get_station_name(destination)
            conditions.append(f'destination="{station}"')
            
        if trip_date:
            # API expects date'YYYY-MM-DD' format for date comparisons
            date_str = trip_date.strftime("%Y-%m-%d")
            conditions.append(f"date=date'{date_str}'")
        
        if only_available:
            conditions.append('od_happy_card="OUI"')
        
        for field, value in extra_filters.items():
            conditions.append(f'{field}="{value}"')
        
        return " and ".join(conditions)
    
    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make a request to the API with rate limiting."""
        self._rate_limit()
        
        url = urljoin(
            self.config.PUBLIC_API_BASE_URL, 
            f"{self.config.TGVMAX_DATASET}/records"
        )
        
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()
    
    def get_trips_raw(self, 
                      limit: int = 100, 
                      offset: int = 0,
                      origin: Optional[str] = None,
                      destination: Optional[str] = None,
                      trip_date: Optional[date] = None,
                      only_available: bool = True,
                      order_by: str = "heure_depart",
                      **extra_filters) -> Dict[str, Any]:
        """Get raw trip data from the API.
        
        Args:
            limit: Maximum number of results (max 100 per request)
            offset: Offset for pagination
            origin: Origin station (name or alias)
            destination: Destination station (name or alias)
            trip_date: Date of the trip
            only_available: Only return TGV Max available trips
            order_by: Field to order results by
            **extra_filters: Additional filters (e.g., axe="SUD EST")
            
        Returns:
            Raw API response
        """
        params = {
            "limit": min(limit, 100),
            "offset": offset,
            "order_by": order_by,
        }
        
        where = self._build_where_clause(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=only_available,
            **extra_filters
        )
        
        if where:
            params["where"] = where
        
        return self._request(params)
    
    def search_trips(self,
                     origin: str,
                     destination: str,
                     trip_date: Optional[date] = None,
                     only_available: bool = True,
                     limit: int = 100,
                     **filters) -> List[Trip]:
        """Search for trips and return Trip objects.
        
        Args:
            origin: Origin station (name or alias like "paris", "lyon")
            destination: Destination station
            trip_date: Specific date to search
            only_available: Only return MAX-eligible trips
            limit: Maximum results to return
            **filters: Additional filters
            
        Returns:
            List of Trip objects
        """
        response = self.get_trips_raw(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=only_available,
            limit=limit,
            **filters
        )
        
        trips = []
        for record in response.get("results", []):
            try:
                trip = Trip.from_api_response(record)
                trips.append(trip)
            except Exception as e:
                if self.config.DEBUG:
                    print(f"Warning: Failed to parse trip: {e}")
        
        return trips
    
    def search_trips_range(self,
                           origin: str,
                           destination: str,
                           start_date: date,
                           end_date: date,
                           only_available: bool = True) -> Dict[date, List[Trip]]:
        """Search for trips across a date range.
        
        Args:
            origin: Origin station
            destination: Destination station  
            start_date: Start of date range
            end_date: End of date range (inclusive)
            only_available: Only return MAX-eligible trips
            
        Returns:
            Dictionary mapping dates to lists of trips
        """
        results = {}
        current = start_date
        
        while current <= end_date:
            trips = self.search_trips(
                origin=origin,
                destination=destination,
                trip_date=current,
                only_available=only_available
            )
            results[current] = trips
            current += timedelta(days=1)
        
        return results
    
    def iter_all_trips(self,
                       origin: Optional[str] = None,
                       destination: Optional[str] = None,
                       trip_date: Optional[date] = None,
                       only_available: bool = True,
                       **filters) -> Iterator[Trip]:
        """Iterate over all matching trips with automatic pagination.
        
        Yields:
            Trip objects one at a time
        """
        offset = 0
        limit = 100
        
        while True:
            response = self.get_trips_raw(
                origin=origin,
                destination=destination,
                trip_date=trip_date,
                only_available=only_available,
                limit=limit,
                offset=offset,
                **filters
            )
            
            results = response.get("results", [])
            if not results:
                break
            
            for record in results:
                try:
                    yield Trip.from_api_response(record)
                except Exception:
                    continue
            
            if len(results) < limit:
                break
            
            offset += limit
    
    def find_available_dates(self,
                             origin: str,
                             destination: str,
                             start_date: Optional[date] = None,
                             days_ahead: int = 30) -> List[date]:
        """Find dates with available TGV Max trips.
        
        Args:
            origin: Origin station
            destination: Destination station
            start_date: Start date (defaults to today)
            days_ahead: Number of days to search ahead
            
        Returns:
            List of dates with available trips
        """
        if start_date is None:
            start_date = date.today()
        
        end_date = start_date + timedelta(days=days_ahead)
        
        trips_by_date = self.search_trips_range(
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            only_available=True
        )
        
        return [d for d, trips in trips_by_date.items() if trips]
    
    def get_cheapest_trip(self,
                          origin: str,
                          destination: str,
                          trip_date: date) -> Optional[Trip]:
        """Get the earliest available trip for a date (earliest = more chances).
        
        For TGV Max, all trips are "free" so we return the first available.
        """
        trips = self.search_trips(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            only_available=True,
            limit=1
        )
        return trips[0] if trips else None
    
    def get_stations(self) -> List[str]:
        """Get list of all unique station names from recent data."""
        params = {
            "select": "origine",
            "group_by": "origine",
            "limit": 100,
        }
        
        url = urljoin(
            self.config.PUBLIC_API_BASE_URL,
            f"{self.config.TGVMAX_DATASET}/records"
        )
        
        self._rate_limit()
        response = self.session.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        stations = [r.get("origine") for r in data.get("results", []) if r.get("origine")]
        return sorted(set(stations))
    
    def get_routes(self) -> List[tuple]:
        """Get list of all origin-destination pairs."""
        params = {
            "select": "origine, destination",
            "group_by": "origine, destination", 
            "limit": 100,
        }
        
        url = urljoin(
            self.config.PUBLIC_API_BASE_URL,
            f"{self.config.TGVMAX_DATASET}/records"
        )
        
        self._rate_limit()
        response = self.session.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        routes = [
            (r.get("origine"), r.get("destination"))
            for r in data.get("results", [])
            if r.get("origine") and r.get("destination")
        ]
        return sorted(set(routes))
    
    # Backward compatibility with old API
    def get_trips(self, 
                  limit: int = 40, 
                  offset: int = 0,
                  date: Optional[str] = None,
                  origine: Optional[str] = None,
                  destination: Optional[str] = None,
                  day_of_week: Optional[str] = None,
                  **filters) -> Dict[str, Any]:
        """Legacy method for backward compatibility.
        
        Prefer using search_trips() for new code.
        """
        params = {"limit": limit, "offset": offset}
        where_conditions = []
        
        if origine:
            where_conditions.append(f'origine="{origine}"')
        if destination:
            where_conditions.append(f'destination="{destination}"')
        if date:
            parts = date.split('-')
            if len(parts) == 3:
                date_formatted = f"{parts[2]}-{parts[1]}-{parts[0]}"
                where_conditions.append(f"date_format(date, 'dd-MM-YYYY') = '{date_formatted}'")
        if day_of_week:
            where_conditions.append(f'date_format(date, "EEEE") = "{day_of_week}"')
        
        for field, value in filters.items():
            where_conditions.append(f'{field}="{value}"')
        
        if where_conditions:
            params["where"] = " and ".join(where_conditions)
        
        url = urljoin(self.config.PUBLIC_API_BASE_URL, f"{self.config.TGVMAX_DATASET}/records")
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()
