"""lightweight web frontend for exploring TGV Max free trips.

Starts a local Flask server that renders a single-page app
with a map of France showing:
  - Selectable origin/destination stations
  - Free trips for the selected route
  - Fully-MAX decomposed alternatives
  - Quick filters (dead-hour, long-distance, weekend)

Usage:
    python3 frontend/server.py
    # then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ensure src/ is importable
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from flask import Flask, request, jsonify, send_from_directory
from network.core import search, broadcast, SearchResult
from config import STATIONS, get_station_name
from network.decomposition import CompositeTrip

app = Flask(__name__, static_folder=None)

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/style.css")
def style():
    return send_from_directory(str(HERE), "style.css", mimetype="text/css")


@app.route("/app.js")
def app_js():
    return send_from_directory(str(HERE), "app.js", mimetype="application/javascript")


@app.route("/")
def index() -> str:
    """Serve the single-page frontend."""
    template = HERE / "index.html"
    return template.read_text()


@app.route("/api/stations")
def api_stations():
    """Return known station aliases."""
    return jsonify(STATIONS)


@app.route("/api/train_stops")
def api_train_stops():
    """Return all stops for a given train on a date."""
    train_no = request.args.get("train", "")
    date_str = request.args.get("date", "")
    if not train_no:
        return jsonify({"error": "missing train param"}), 400

    from network.client import SNCFMaxClient
    client = SNCFMaxClient()

    trip_date = None
    if date_str:
        try:
            trip_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    response = client.get_trips_raw(
        trip_date=trip_date, only_available=False, limit=200,
        train_no=train_no,
    )

    stops: Dict[str, dict] = {}
    for r in response.get("results", []):
        o = r.get("origine", "")
        d = r.get("destination", "")
        dep = r.get("heure_depart", "")
        arr = r.get("heure_arrivee", "")
        if o and dep:
            stops[o] = {"station": o, "time": dep, "type": "departure"}
        if d and arr:
            stops[d] = {"station": d, "time": arr, "type": "arrival"}

    # sort by time
    sorted_stops = sorted(stops.values(), key=lambda s: s["time"])

    return jsonify({
        "train_no": train_no,
        "date": date_str,
        "stops": sorted_stops,
    })


@app.route("/api/search")
def api_search():
    """Search for free trips between two stations.

    Query params: origin, destination, date (optional, YYYY-MM-DD)
                  decompose (optional, default 1)
                  departure_after (optional, HH:MM)
                  arrival_before (optional, HH:MM)
    """
    origin = request.args.get("origin", "paris")
    destination = request.args.get("destination", "lyon")
    date_str = request.args.get("date", "")
    decompose = request.args.get("decompose", "1") == "1"
    dep_after = request.args.get("departure_after", "")
    arr_before = request.args.get("arrival_before", "")

    trip_date: Optional[date] = None
    if date_str:
        try:
            trip_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "bad date format (use YYYY-MM-DD)"}), 400

    from datetime import time as dt_time
    dep_after_t = None
    arr_before_t = None
    if dep_after:
        try:
            dep_after_t = datetime.strptime(dep_after, "%H:%M").time()
        except ValueError:
            pass
    if arr_before:
        try:
            arr_before_t = datetime.strptime(arr_before, "%H:%M").time()
        except ValueError:
            pass

    result = search(
        origin=origin,
        destination=destination,
        trip_date=trip_date,
        decompose=decompose,
        departure_after=dep_after_t,
        arrival_before=arr_before_t,
    )
    return jsonify(_serialize_result(result))


@app.route("/api/broadcast")
def api_broadcast():
    """Find all free trips from a station on a given date.

    Query params: origin, date (optional, YYYY-MM-DD)
    """
    origin = request.args.get("origin", "paris")
    date_str = request.args.get("date", "")

    trip_date: Optional[date] = None
    if date_str:
        try:
            trip_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "bad date format (use YYYY-MM-DD)"}), 400

    trips = broadcast(origin=origin, trip_date=trip_date)
    return jsonify([_trip_to_dict(t) for t in trips])


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _trip_to_dict(trip) -> dict:
    return {
        "train_number": trip.train_number,
        "origin": str(trip.origin),
        "destination": str(trip.destination),
        "departure_date": trip.departure_date.isoformat(),
        "departure_time": trip.departure_time.strftime("%H:%M"),
        "arrival_time": trip.arrival_time.strftime("%H:%M"),
        "duration_min": int(trip.duration.total_seconds() // 60),
        "is_free": trip.is_free,
        "price_display": trip.price_display,
        "axe": trip.axe,
        "entity": trip.entity,
    }


def _composite_to_dict(comp: CompositeTrip) -> dict:
    return {
        "legs": [_trip_to_dict(leg.trip) for leg in comp.legs],
        "is_fully_max": comp.is_fully_max,
        "total_duration_min": int(comp.total_duration.total_seconds() // 60),
        "connection_min": int(comp.connection_time.total_seconds() // 60),
        "max_legs": comp.max_legs,
        "paid_legs": comp.paid_legs,
        "departure_time": comp.departure_time.strftime("%H:%M"),
        "arrival_time": comp.arrival_time.strftime("%H:%M"),
        "price_display": comp.price_display,
        "origin": comp.origin,
        "destination": comp.destination,
    }


def _serialize_result(result: SearchResult) -> dict:
    return {
        "origin": result.origin,
        "destination": result.destination,
        "trip_date": result.trip_date.isoformat(),
        "direct_free": [_trip_to_dict(t) for t in result.direct_free],
        "direct_paid": [_trip_to_dict(t) for t in result.direct_paid],
        "decompositions": [_composite_to_dict(c) for c in result.decompositions],
        "descentres": [_composite_to_dict(c) for c in result.descentres],
        "has_any_free": result.has_any_free,
        "count_direct_free": len(result.direct_free),
        "count_direct_paid": len(result.direct_paid),
        "count_decomposed_free": sum(1 for c in result.decompositions if c.is_fully_free),
        "count_decomposed_paid": sum(1 for c in result.decompositions if not c.is_fully_free),
        "count_descentres": len(result.descentres),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(port: int = 5000, debug: bool = False):
    print(f"\n  TGV Max frontend: http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=debug)


if __name__ == "__main__":
    main()
