"""Integration tests hitting the real data.sncf.com API.

Run:  nix develop --command pytest tests/test_integration.py -v
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _tomorrow(n: int = 3) -> date:
    return date.today() + timedelta(days=n)


# ---------------------------------------------------------------------------
# test_client
# ---------------------------------------------------------------------------

def test_client_search_all_trips_returns_lists():
    from network.client import SNCFMaxClient
    c = SNCFMaxClient()
    free, paid = c.search_all_trips("paris", "lyon", _tomorrow())
    assert isinstance(free, list)
    assert isinstance(paid, list)


def test_client_free_trips_are_actually_free():
    from network.client import SNCFMaxClient
    c = SNCFMaxClient()
    free, paid = c.search_all_trips("paris", "lyon", _tomorrow())
    assert all(t.is_free for t in free)


def test_client_paid_trips_are_not_free():
    from network.client import SNCFMaxClient
    c = SNCFMaxClient()
    free, paid = c.search_all_trips("paris", "lyon", _tomorrow())
    assert all(not t.is_free for t in paid)


def test_client_trips_have_train_numbers():
    from network.client import SNCFMaxClient
    c = SNCFMaxClient()
    free, paid = c.search_all_trips("paris", "lyon", _tomorrow())
    all_t = free + paid
    assert all(len(t.train_number) > 0 for t in all_t)
    assert any(t.train_number != "" for t in all_t)


# ---------------------------------------------------------------------------
# test_core_search
# ---------------------------------------------------------------------------

def test_core_search_returns_result():
    from network.core import search
    r = search("paris", "lyon", _tomorrow(), decompose=True)
    assert "PARIS" in r.origin.upper()
    assert "LYON" in r.destination.upper()
    assert isinstance(r.direct_free, list)
    assert isinstance(r.decompositions, list)
    assert len(r.summary()) > 0


def test_core_search_resolves_aliases():
    from network.core import search
    r = search("lyon", "nice", _tomorrow(), decompose=False)
    assert "LYON" in r.origin.upper()
    assert "NICE" in r.destination.upper()


# ---------------------------------------------------------------------------
# test_decomposition
# ---------------------------------------------------------------------------

def test_decomposition_returns_list():
    from network.decomposition import TripDecomposer
    d = TripDecomposer()
    combos = d.find_max_only_combos("paris", "lyon", _tomorrow())
    assert isinstance(combos, list)


def test_decomposition_combos_are_fully_max():
    from network.decomposition import TripDecomposer
    d = TripDecomposer()
    combos = d.find_max_only_combos("paris", "lyon", _tomorrow())
    if combos:
        assert all(c.is_fully_max for c in combos)
        assert all(isinstance(c.score, (int, float)) for c in combos)


# ---------------------------------------------------------------------------
# test_finder_broadcast
# ---------------------------------------------------------------------------

def test_finder_returns_report():
    from network.finder import hunt
    report = hunt("paris", _tomorrow())
    assert report.total_free >= 0
    assert len(report.buckets) > 0
    assert "Free trips from" in report.summary()


def test_finder_all_free_trips_are_free():
    from network.finder import hunt
    report = hunt("paris", _tomorrow())
    trips = report.all_free_trips
    assert all(t.is_free for t in trips)


def test_finder_buckets_have_names():
    from network.finder import hunt
    report = hunt("paris", _tomorrow())
    for b in report.buckets:
        assert len(b.label) > 0


# ---------------------------------------------------------------------------
# test_graph
# ---------------------------------------------------------------------------

def test_graph_loads():
    from network.graph import graph, resolve, neighbors
    g = graph()
    assert len(g) > 50
    assert len(g) < 500


def test_graph_resolve():
    from network.graph import resolve
    assert resolve("PARIS GARE DE LYON") is not None
    assert resolve("LYON PART DIEU") is not None
    assert resolve("STRASBOURG") is not None


def test_graph_neighbors():
    from network.graph import neighbors, resolve
    node = resolve("PARIS GARE DE LYON")
    nbs = neighbors(node)
    assert len(nbs) >= 5


# ---------------------------------------------------------------------------
# test_models
# ---------------------------------------------------------------------------

def test_model_trip_from_api():
    from models import Trip, TripStatus
    data = {
        "date": "2025-06-15", "heure_depart": "18:04", "heure_arrivee": "20:10",
        "train_no": "6655", "origine": "PARIS GARE DE LYON", "destination": "LYON PART DIEU",
        "od_happy_card": "OUI", "entity": "INOUI",
    }
    t = Trip.from_api_response(data)
    assert t.train_number == "6655"
    assert t.is_free
    assert "MAX" in t.price_display
    assert t.duration.total_seconds() > 0


def test_model_trip_not_available():
    from models import Trip, TripStatus
    data = {
        "date": "2025-06-15", "heure_depart": "08:00", "heure_arrivee": "10:00",
        "train_no": "6001", "origine": "PARIS", "destination": "LYON",
        "od_happy_card": "NON", "entity": "INOUI",
    }
    t = Trip.from_api_response(data)
    assert not t.is_free
    assert t.available_for_max == TripStatus.UNAVAILABLE


def test_model_trip_with_price():
    from models import Trip
    data = {
        "date": "2025-06-15", "heure_depart": "10:00", "heure_arrivee": "12:00",
        "train_no": "6100", "origine": "PARIS", "destination": "LYON",
        "od_happy_card": "NON", "entity": "INOUI", "prix_2nde": 55.0,
    }
    t = Trip.from_api_response(data)
    assert t.price_cents == 5500
    assert t.price == 55.0
    assert "55.00" in t.price_display


def test_model_trip_key():
    from models import Trip
    data = {
        "date": "2025-06-15", "heure_depart": "18:04", "heure_arrivee": "20:10",
        "train_no": "6655", "origine": "PARIS", "destination": "LYON",
        "od_happy_card": "OUI", "entity": "INOUI",
    }
    t = Trip.from_api_response(data)
    k = t.trip_key
    assert "6655" in k and "2025-06-15" in k


def test_model_station_equality():
    from models import Station
    assert Station(name="LYON (intramuros)") == Station(name="lyon (intramuros)")


def test_composite_trip_all_max():
    from models import Trip
    from network.decomposition import TripLeg, CompositeTrip
    t1 = Trip.from_api_response({
        "date": "2025-06-15", "heure_depart": "12:00", "heure_arrivee": "13:30",
        "train_no": "6601", "origine": "PARIS", "destination": "LE CREUSOT",
        "od_happy_card": "OUI", "entity": "INOUI",
    })
    t2 = Trip.from_api_response({
        "date": "2025-06-15", "heure_depart": "14:00", "heure_arrivee": "14:50",
        "train_no": "6602", "origine": "LE CREUSOT", "destination": "LYON",
        "od_happy_card": "OUI", "entity": "INOUI",
    })
    c = CompositeTrip(legs=[TripLeg(trip=t1, is_max=True), TripLeg(trip=t2, is_max=True)])
    assert c.is_fully_max
    assert c.max_legs == 2
    assert c.paid_legs == 0
    assert c.total_duration.total_seconds() > 0


# ---------------------------------------------------------------------------
# test_config
# ---------------------------------------------------------------------------

def test_config_station_resolution():
    from config import get_station_name
    assert "PARIS" in get_station_name("paris").upper()
    assert get_station_name("nonexistent") == "nonexistent"


def test_config_stations_count():
    from config import STATIONS
    assert len(STATIONS) > 20


# ---------------------------------------------------------------------------
# test_api_facade
# ---------------------------------------------------------------------------

def test_api_facade_search():
    from api import TGVMaxAPI
    from network.core import SearchResult
    api = TGVMaxAPI()
    r = api.search("paris", "lyon", _tomorrow())
    assert isinstance(r, SearchResult)
