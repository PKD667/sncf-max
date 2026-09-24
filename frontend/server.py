"""lightweight web frontend for exploring TGV Max free trips.

Starts a local Flask server that renders a single-page app
with a map of France showing:
  - Selectable origin/destination stations
  - Free trips for the selected route
  - Fully-MAX decomposed alternatives
  - Quick filters (dead-hour, long-distance, weekend)
  - SNCF Connect integration for booking and exact prices (PAM-gated)

Usage:
    python3 frontend/server.py
    # then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import sys
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import threading

# ensure src/ is importable
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from flask import Flask, request, jsonify, redirect, send_from_directory
from network.core import search, broadcast, SearchResult
from config import STATIONS, get_station_name
from network.decomposition import CompositeTrip
from network import stations as stn

app = Flask(__name__, static_folder=None)

HERE = Path(__file__).resolve().parent
TGVMAX_ROOT = HERE / "tgvmax"

# ---------------------------------------------------------------------------
# PAM User & SNCF Connect Credential Storage
# ---------------------------------------------------------------------------

# Per-user credential storage (in-memory for now; can be backed by file/db)
# Key: PAM username (from X-Remote-User header)
# Value: {"email": "...", "password": "..."}
_user_credentials: dict[str, dict] = {}
_credentials_lock = threading.Lock()

# Data directory for persistent storage
_DATA_DIR = Path(os.environ.get("MAX_DATA_DIR", "/tmp/max-data"))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CREDS_FILE = _DATA_DIR / "sncf_credentials.json"


def _load_credentials() -> None:
    """Load credentials from disk."""
    global _user_credentials
    if _CREDS_FILE.exists():
        try:
            with open(_CREDS_FILE, "r") as f:
                _user_credentials = json.load(f)
        except Exception:
            _user_credentials = {}


def _save_credentials() -> None:
    """Save credentials to disk."""
    with _credentials_lock:
        try:
            with open(_CREDS_FILE, "w") as f:
                json.dump(_user_credentials, f)
        except Exception:
            pass


# Load on startup
_load_credentials()


def _get_pam_user() -> Optional[str]:
    """Extract PAM username from X-Remote-User header (set by nginx auth_request)."""
    return request.headers.get("X-Remote-User")


def _require_auth() -> str:
    """Require PAM authentication, return username or raise 401."""
    user = _get_pam_user()
    if not user:
        return jsonify({"error": "authentication required", "pam_required": True}), 401
    return user


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


@app.route("/tgvmax")
def tgvmax_root():
    """Keep relative links inside the bundled TGV Max site."""
    return redirect("/tgvmax/", code=308)


@app.route("/tgvmax/", defaults={"path": "index.html"})
@app.route("/tgvmax/<path:path>")
def tgvmax_site(path: str):
    """Serve the static TGV Max site bundled from the local project."""
    return send_from_directory(str(TGVMAX_ROOT), path)


@app.route("/api/stations")
def api_stations():
    """Return every real TGV Max station with a pretty label + coords.

    Shape: {"stations": [{"name", "display", "lat", "lon"}], "aliases": {...}}
    `name` is the API station name (used as the search value); `display` is
    the human-friendly label; lat/lon are present when known (for the map).
    """
    out = []
    for name in stn.all_stations():
        c = stn.coords(name)
        out.append({
            "name": name,
            "display": stn.display_name(name),
            "lat": c[0] if c else None,
            "lon": c[1] if c else None,
        })
    out.sort(key=lambda s: s["display"])
    return jsonify({"stations": out, "aliases": STATIONS})


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

    stops: dict = {}
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
# SNCF Connect Authentication Endpoints (PAM-gated)
# ---------------------------------------------------------------------------


@app.route("/api/auth/sncf/status")
def api_sncf_auth_status():
    """Check if user has SNCF Connect credentials configured."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    with _credentials_lock:
        has_creds = user in _user_credentials
        creds = _user_credentials.get(user, {})

    return jsonify({
        "authenticated": True,
        "pam_user": user,
        "sncf_connected": has_creds,
        "email": creds.get("email") if has_creds else None,
    })


@app.route("/api/auth/sncf/set", methods=["POST"])
def api_sncf_auth_set():
    """Store SNCF Connect credentials for the current PAM user."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    # Basic email validation
    if "@" not in email:
        return jsonify({"error": "invalid email"}), 400

    with _credentials_lock:
        _user_credentials[user] = {"email": email, "password": password}
        _save_credentials()

    return jsonify({"success": True, "email": email})


@app.route("/api/auth/sncf/delete", methods=["POST"])
def api_sncf_auth_delete():
    """Remove SNCF Connect credentials for the current PAM user."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    with _credentials_lock:
        if user in _user_credentials:
            del _user_credentials[user]
            _save_credentials()
            return jsonify({"success": True})
        return jsonify({"error": "no credentials stored"}), 404


# ---------------------------------------------------------------------------
# Booking Endpoints (requires SNCF Connect credentials)
# ---------------------------------------------------------------------------


@app.route("/api/booking/book", methods=["POST"])
def api_booking_book():
    """Book a TGV Max trip using stored SNCF Connect credentials."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    with _credentials_lock:
        creds = _user_credentials.get(user)
    if not creds:
        return jsonify({"error": "SNCF Connect credentials not configured"}), 400

    data = request.get_json(silent=True) or {}
    trip_data = data.get("trip")
    if not trip_data:
        return jsonify({"error": "trip data required"}), 400

    # Build Trip object from request
    try:
        from models import Trip, Station
        trip = Trip(
            train_number=trip_data.get("train_number", ""),
            origin=Station(name=trip_data.get("origin", "")),
            destination=Station(name=trip_data.get("destination", "")),
            departure_date=datetime.strptime(trip_data.get("departure_date", ""), "%Y-%m-%d").date(),
            departure_time=datetime.strptime(trip_data.get("departure_time", ""), "%H:%M").time(),
            arrival_time=datetime.strptime(trip_data.get("arrival_time", ""), "%H:%M").time(),
            available_for_max=trip_data.get("available_for_max", "UNKNOWN"),
            axe=trip_data.get("axe"),
            entity=trip_data.get("entity"),
            price_cents=trip_data.get("price_cents"),
        )
    except Exception as e:
        return jsonify({"error": f"invalid trip data: {e}"}), 400

    # Attempt booking using stored credentials
    try:
        from booking.auth import load_or_login
        from booking.booking import book_sync
        from config import SNCFConfig, default_config

        config = default_config
        session = load_or_login(creds["email"], creds["password"], config)
        result = book_sync(trip, session=session, config=config)

        return jsonify({
            "success": result.is_success,
            "status": result.status.value,
            "message": result.message,
            "confirmation": result.confirmation_number,
        })
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/booking/auto", methods=["POST"])
def api_booking_auto():
    """Automatically find and book the best TGV Max trip."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    with _credentials_lock:
        creds = _user_credentials.get(user)
    if not creds:
        return jsonify({"error": "SNCF Connect credentials not configured"}), 400

    data = request.get_json(silent=True) or {}
    origin = data.get("origin", "")
    destination = data.get("destination", "")
    date_str = data.get("date", "")
    preferred_time = data.get("preferred_time", "")

    if not origin or not destination or not date_str:
        return jsonify({"error": "origin, destination, and date required"}), 400

    try:
        trip_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "bad date format (use YYYY-MM-DD)"}), 400

    try:
        from booking.booking import auto_book
        from models import UserCredentials
        from config import default_config

        credentials = UserCredentials(email=creds["email"], password=creds["password"])
        result = auto_book(
            origin=origin,
            destination=destination,
            trip_date=trip_date,
            credentials=credentials,
            preferred_time=preferred_time if preferred_time else None,
            config=default_config,
        )

        return jsonify({
            "success": result.is_success,
            "status": result.status.value,
            "message": result.message,
            "confirmation": result.confirmation_number,
            "trip": {
                "train_number": result.trip.train_number,
                "origin": str(result.trip.origin),
                "destination": str(result.trip.destination),
                "departure_time": result.trip.departure_time.strftime("%H:%M"),
                "arrival_time": result.trip.arrival_time.strftime("%H:%M"),
            } if result.trip else None,
        })
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _trip_to_dict(trip) -> dict:
    if trip.is_free:
        price_display, price_estimated = "MAX", False
    else:
        from network.fares import estimate_fare
        f = estimate_fare(str(trip.origin), str(trip.destination), trip.carrier)
        price_display, price_estimated = f.display, (not f.exact)
    return {
        "train_number": trip.train_number,
        "origin": str(trip.origin),
        "destination": str(trip.destination),
        "departure_date": trip.departure_date.isoformat(),
        "departure_time": trip.departure_time.strftime("%H:%M"),
        "arrival_time": trip.arrival_time.strftime("%H:%M"),
        "duration_min": int(trip.duration.total_seconds() // 60),
        "is_free": trip.is_free,
        "carrier": trip.carrier,
        "price_display": price_display,
        "price_estimated": price_estimated,
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
        "price_estimated": (not comp.total_fare.exact) and not comp.is_fully_max,
        "origin": comp.origin,
        "destination": comp.destination,
        "is_descentre": comp.is_descentre,
        "booked_to": comp.booked_to,
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
    # Bind address is configurable so the app can be published by a reverse
    # proxy on another host. It stays 127.0.0.1 by default, so running this
    # directly for development is unchanged and never accidentally exposed.
    import os
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", port))
    print(f"\n  TGV Max frontend: http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
