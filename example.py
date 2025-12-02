#!/usr/bin/env python3
"""Example script demonstrating the SNCF Max API.

This script shows how to:
1. Search for available TGV Max trips
2. Monitor for availability
3. Automatically book trips
4. Find alternatives with pricing (--alternatives)

Usage:
    # Basic search
    python example.py search paris lyon 2025-01-15
    
    # Search with alternatives (includes priced options)
    python example.py search paris lyon 2025-01-15 --alternatives
    
    # Deadline-based search (must arrive by time)
    python example.py deadline paris lyon "2025-01-15 14:00" --alternatives
    
    # Monitor for availability
    python example.py monitor paris lyon 2025-01-15
    
    # Auto-book (requires credentials)
    SNCF_EMAIL=your@email.com SNCF_PASSWORD=yourpass python example.py book paris lyon 2025-01-15
"""

import sys
import os
import argparse
from datetime import date, datetime, timedelta

# Add src to path for development
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sncf_max import TGVMaxAPI, TGVMaxMonitor, Trip
from sncf_max.config import get_station_name


def demo_search():
    """Demonstrate trip search functionality."""
    print("=" * 60)
    print("🚄 SNCF Max Trip Search Demo")
    print("=" * 60)
    
    api = TGVMaxAPI()
    
    # Search for trips Paris -> Lyon
    tomorrow = date.today() + timedelta(days=1)
    print(f"\nSearching for Paris → Lyon on {tomorrow}...")
    
    trips = api.search("paris", "lyon", tomorrow)
    
    if trips:
        print(f"\n✅ Found {len(trips)} available TGV Max trips:\n")
        for i, trip in enumerate(trips[:10], 1):  # Show first 10
            print(f"  {i}. Train {trip.train_number}")
            print(f"     {trip.departure_time.strftime('%H:%M')} → {trip.arrival_time.strftime('%H:%M')}")
            print(f"     {trip.origin} → {trip.destination}")
            print()
    else:
        print("\n❌ No TGV Max trips available for this route/date")
        print("   (TGV Max slots are limited - try different dates)")
    
    # Show available dates
    print("\n📅 Checking availability for next 7 days...")
    week_later = tomorrow + timedelta(days=7)
    
    trips_by_date = api.search_range(
        origin="paris",
        destination="lyon",
        start_date=tomorrow,
        end_date=week_later
    )
    
    for trip_date, trips in sorted(trips_by_date.items()):
        status = f"✅ {len(trips)} trips" if trips else "❌ No availability"
        print(f"  {trip_date.strftime('%a %d/%m')}: {status}")


def demo_stations():
    """Show available stations."""
    print("=" * 60)
    print("🚉 Available Station Aliases")
    print("=" * 60)
    
    from sncf_max.config import STATIONS
    
    for alias, full_name in sorted(STATIONS.items()):
        print(f"  {alias:20} → {full_name}")


def demo_monitor():
    """Demonstrate the monitoring system."""
    print("=" * 60)
    print("👀 SNCF Max Availability Monitor Demo")
    print("=" * 60)
    
    tomorrow = date.today() + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    
    monitor = TGVMaxMonitor()
    
    # Set up a callback
    def on_found(event):
        print(f"\n🎉 FOUND {len(event.trips)} trips!")
        for trip in event.trips:
            print(f"   {trip}")
    
    monitor.on_available(on_found)
    
    # Watch Paris -> Lyon for next 2 days
    watch_id = monitor.watch(
        origin="paris",
        destination="lyon",
        dates=[tomorrow, day_after],
        preferred_times=["08:00", "09:00", "18:00"],  # Morning and evening
        auto_book=False  # Set to True with credentials to auto-book
    )
    
    print(f"\nWatching: Paris → Lyon")
    print(f"Dates: {tomorrow}, {day_after}")
    print(f"Preferred times: 08:00, 09:00, 18:00")
    print(f"Watch ID: {watch_id}")
    
    # Check once
    print("\nChecking availability now...")
    events = monitor.check_now()
    
    if not events:
        print("No matching trips found at this time.")


def demo_booking():
    """Demonstrate booking (requires credentials)."""
    print("=" * 60)
    print("🎟️ SNCF Max Booking Demo")
    print("=" * 60)
    
    email = os.getenv("SNCF_EMAIL")
    password = os.getenv("SNCF_PASSWORD")
    
    if not email or not password:
        print("\n⚠️  To test booking, set environment variables:")
        print("   export SNCF_EMAIL=your@email.com")
        print("   export SNCF_PASSWORD=yourpassword")
        print("\nSkipping booking demo...")
        return
    
    api = TGVMaxAPI()
    
    # Search for a trip
    tomorrow = date.today() + timedelta(days=1)
    trips = api.search("paris", "lyon", tomorrow)
    
    if not trips:
        print("No trips available to book")
        return
    
    trip = trips[0]
    print(f"\nWould book: {trip}")
    print("\n⚠️  Actual booking disabled in demo - uncomment to enable")
    
    Uncomment to actually book:
    try:
        api.login(email, password)
        result = api.book(trip)
        print(f"\nResult: {result}")
    except Exception as e:
        print(f"Booking failed: {e}")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        # Default: run search demo
        demo_search()
        print("\n")
        demo_stations()
        return
    
    command = sys.argv[1].lower()
    
    if command == "search":
        if len(sys.argv) < 5:
            print("Usage: python example.py search <origin> <destination> <date>")
            print("Example: python example.py search paris lyon 2025-01-15")
            return
        
        origin = sys.argv[2]
        destination = sys.argv[3]
        trip_date = datetime.strptime(sys.argv[4], "%Y-%m-%d").date()
        
        api = TGVMaxAPI()
        trips = api.search(origin, destination, trip_date)
        
        print(f"\n🚄 Trips from {get_station_name(origin)} to {get_station_name(destination)}")
        print(f"   Date: {trip_date}\n")
        
        if trips:
            for trip in trips:
                print(f"  Train {trip.train_number}: {trip.departure_time} → {trip.arrival_time}")
        else:
            print("  No TGV Max availability")
    
    elif command == "monitor":
        if len(sys.argv) < 5:
            print("Usage: python example.py monitor <origin> <destination> <date>")
            return
        
        origin = sys.argv[2]
        destination = sys.argv[3]
        trip_date = datetime.strptime(sys.argv[4], "%Y-%m-%d").date()
        
        print(f"Monitoring {origin} → {destination} on {trip_date}")
        print("Press Ctrl+C to stop\n")
        
        monitor = TGVMaxMonitor()
        monitor.on_available(lambda e: print(f"Found {len(e.trips)} trips!"))
        monitor.watch(origin, destination, [trip_date])
        
        try:
            monitor.start(interval=60, max_checks=10)  # Check every minute, 10 times
        except KeyboardInterrupt:
            print("\nStopped.")
    
    elif command == "book":
        demo_booking()
    
    elif command == "stations":
        demo_stations()
    
    else:
        print(f"Unknown command: {command}")
        print("Available commands: search, monitor, book, stations")


if __name__ == "__main__":
    main()
