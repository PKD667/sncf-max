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

import asyncio
import json
import secrets
import sys
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import threading

# ensure src/ is importable
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from flask import Flask, request, jsonify, send_from_directory
from network.core import search, broadcast, broadcast_to, SearchResult
from config import STATIONS, get_station_name
from network.decomposition import CompositeTrip
from network import stations as stn

app = Flask(__name__, static_folder=None)

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# SNCF Connect login sessions (memory only, no PAM needed)
# ---------------------------------------------------------------------------
# The site is public, so there is no PAM identity to key stored passwords
# on - and passwords should never be stored anyway. Instead each login
# gets a random login_id kept in the visitor's browser (localStorage);
# the server holds the resulting SNCF *session cookies* in RAM for
# LOGIN_TTL_SECONDS. Passwords are used once for the login flow and
# never written anywhere.
#
# Because SNCF ("Mon Identifiant SNCF") challenges every new device with
# a 6-digit emailed code after the password, login is two-step:
#   POST /api/auth/sncf/start {email, password}
#     -> {"status": "connected", login_id}  (no code asked), or
#        {"status": "code_required", login_id}  (check the inbox)
#   POST /api/auth/sncf/code {login_id, code}
#     -> {"status": "connected", login_id}
# Booking endpoints take {login_id, ...} and use the stored session.
# A true "open SNCF login, get a token back" redirect flow is not
# possible: SNCF's OIDC provider only issues codes/tokens to registered
# partner clients (per-client client_id + redirect_uri), and there is no
# self-serve registration.

_logins: dict[str, dict] = {}
_logins_lock = threading.Lock()

LOGIN_TTL_SECONDS = 7 * 24 * 3600   # established sessions live a week in RAM
PENDING_TTL_SECONDS = 15 * 60       # emailed codes are valid ~10 minutes
MAX_PENDING_LOGINS = 5              # bound concurrent login browsers


def _drop_login(login_id: str) -> None:
    """Forget a login, closing its browser if it still has one."""
    with _logins_lock:
        rec = _logins.pop(login_id, None)
    auth = rec.get("auth") if rec else None
    if auth is not None:
        try:
            asyncio.run(auth.close())
        except Exception:
            pass


def _purge_logins() -> None:
    """Drop expired logins (called on every auth endpoint hit)."""
    now = time.time()
    with _logins_lock:
        dead = [lid for lid, rec in _logins.items() if rec["expires"] < now]
    for lid in dead:
        _drop_login(lid)


def _session_for(login_id: str):
    """Return the stored SNCF session for a login_id, or None."""
    _purge_logins()
    with _logins_lock:
        rec = _logins.get(login_id or "")
    if rec is None or rec.get("session") is None:
        return None
    return rec["session"]


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
    """Find all free trips for a station on a given date.

    Query params: date (optional, YYYY-MM-DD), direction (optional):
      - direction=from (default): all departures. Param: origin.
      - direction=to: all arrivals (reverse hunt). Param: destination.
    """
    direction = request.args.get("direction", "from")
    date_str = request.args.get("date", "")

    trip_date: Optional[date] = None
    if date_str:
        try:
            trip_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "bad date format (use YYYY-MM-DD)"}), 400

    if direction == "to":
        destination = request.args.get("destination", "")
        if not destination:
            return jsonify({"error": "destination required for direction=to"}), 400
        trips = broadcast_to(destination=destination, trip_date=trip_date)
    else:
        origin = request.args.get("origin", "paris")
        trips = broadcast(origin=origin, trip_date=trip_date)
    return jsonify([_trip_to_dict(t) for t in trips])


# ---------------------------------------------------------------------------
# SNCF Connect Authentication Endpoints (public, login_id sessions)
# ---------------------------------------------------------------------------


@app.route("/api/auth/sncf/status")
def api_sncf_auth_status():
    """Check whether a login_id holds a live SNCF session."""
    login_id = request.args.get("login_id", "")
    session = _session_for(login_id)
    if session is None:
        return jsonify({"connected": False})
    with _logins_lock:
        email = _logins.get(login_id, {}).get("email")
    return jsonify({"connected": True, "email": email})


@app.route("/api/auth/sncf/start", methods=["POST"])
def api_sncf_auth_start():
    """Submit SNCF email+password. Returns connected or code_required."""
    _purge_logins()
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password or "@" not in email:
        return jsonify({"error": "valid email and password required"}), 400

    with _logins_lock:
        pending = sum(1 for r in _logins.values() if r.get("session") is None)
        if pending >= MAX_PENDING_LOGINS:
            return jsonify({"error": "too many logins in progress, try again shortly"}), 429

    from booking.auth import SNCFAuthenticator, EmailCodeRequired, AuthenticationError
    from models import UserCredentials
    from config import default_config

    # Never persisted: the session lives in server memory only.
    auth = SNCFAuthenticator(default_config, persist_session=False)
    try:
        outcome = asyncio.run(
            auth.login_start(UserCredentials(email=email, password=password))
        )
    except EmailCodeRequired as e:
        login_id = secrets.token_urlsafe(24)
        with _logins_lock:
            _logins[login_id] = {
                "auth": auth,           # live browser waiting for the code
                "session": None,
                "email": email,
                "expires": time.time() + PENDING_TTL_SECONDS,
            }
        return jsonify({"status": "code_required", "login_id": login_id,
                        "email": email, "message": str(e)})
    except AuthenticationError as e:
        try:
            asyncio.run(auth.close())
        except Exception:
            pass
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        try:
            asyncio.run(auth.close())
        except Exception:
            pass
        return jsonify({"error": f"login failed: {e}"}), 502

    # Connected without a code challenge. The login browser is done;
    # booking spawns its own browser from the stored session.
    assert outcome == "connected"
    login_id = secrets.token_urlsafe(24)
    with _logins_lock:
        _logins[login_id] = {
            "auth": None,
            "session": auth.session,
            "email": email,
            "expires": time.time() + LOGIN_TTL_SECONDS,
        }
    try:
        asyncio.run(auth.close())
    except Exception:
        pass
    return jsonify({"status": "connected", "login_id": login_id, "email": email})


@app.route("/api/auth/sncf/code", methods=["POST"])
def api_sncf_auth_code():
    """Submit the emailed 6-digit code for a pending login."""
    _purge_logins()
    data = request.get_json(silent=True) or {}
    login_id = data.get("login_id", "")
    code = (data.get("code") or "").strip()

    with _logins_lock:
        rec = _logins.get(login_id)
    if rec is None or rec.get("session") is not None:
        return jsonify({"error": "login expired or unknown, start over"}), 404
    if not code:
        return jsonify({"error": "verification code required"}), 400

    from booking.auth import AuthenticationError
    try:
        session = asyncio.run(rec["auth"].login_submit_code(code))
    except AuthenticationError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        return jsonify({"error": f"code failed: {e}"}), 502

    with _logins_lock:
        rec["session"] = session
        rec["auth"] = None
        rec["expires"] = time.time() + LOGIN_TTL_SECONDS
    return jsonify({"status": "connected", "login_id": login_id,
                    "email": rec.get("email")})


@app.route("/api/auth/sncf/logout", methods=["POST"])
def api_sncf_auth_logout():
    """Forget a login_id (and close its browser, if any)."""
    data = request.get_json(silent=True) or {}
    _drop_login(data.get("login_id", ""))
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Booking Endpoints (requires SNCF Connect credentials)
# ---------------------------------------------------------------------------


@app.route("/api/booking/book", methods=["POST"])
def api_booking_book():
    """Book a TGV Max trip using an SNCF login_id session."""
    data = request.get_json(silent=True) or {}
    session = _session_for(data.get("login_id", ""))
    if session is None:
        return jsonify({"error": "SNCF Connect login required",
                        "need_auth": True}), 401

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

    # Attempt booking with the stored SNCF session (fresh browser,
    # session cookies injected - no password involved at this point).
    try:
        from booking.booking import book_sync
        from config import default_config

        result = book_sync(trip, session=session, config=default_config)

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
    """Automatically find and book the best TGV Max trip.

    Uses the caller's SNCF login_id session. Note a fresh login always
    needs the emailed code, so unattended auto-booking only works while a
    session established interactively is still valid.
    """
    data = request.get_json(silent=True) or {}
    session = _session_for(data.get("login_id", ""))
    if session is None:
        return jsonify({"error": "SNCF Connect login required",
                        "need_auth": True}), 401

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
        from booking.booking import book_sync
        from network.client import SNCFMaxClient
        from config import default_config

        # Best free trip, optionally closest to the preferred time.
        trips = SNCFMaxClient(config=default_config).search_trips(
            origin=origin, destination=destination,
            trip_date=trip_date, only_available=True,
        )
        if not trips:
            return jsonify({"success": False,
                            "message": "no free trips for this route and date"}), 404
        best = trips[0]
        if preferred_time:
            target = datetime.strptime(preferred_time, "%H:%M").time()
            best = min(trips, key=lambda t: abs(
                (datetime.combine(trip_date, t.departure_time)
                 - datetime.combine(trip_date, target)).total_seconds()))

        result = book_sync(best, session=session, config=default_config)

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
