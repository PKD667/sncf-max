"""The sweep must answer exactly what the pair-by-pair queries answered.

This is the strongest check available without a proof: a fake dataset stands
in for data.sncf.com, the client is driven through the same code paths the
site uses, and every result from a station-day sweep is compared against the
result of asking for that (origin, destination) pair directly.  If the two
ever disagree, the cache is lying to the user about a train.
"""

import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import network.bus as bus
import network.ter as ter
from network.client import SNCFMaxClient, clear_caches, cache_stats
from network.dayindex import SIDE_FROM, SIDE_TO
from network.decomposition import TripDecomposer, _PAIR_CACHE

DAY = date(2026, 10, 19)

# One line of five stations, served by four trains a day.  Every train
# publishes a record for every pair of stops it makes - which is what the real
# dataset does, and what makes a station-day sweep equivalent to the pairs.
STATIONS = ["PARIS (intramuros)", "SENS", "DIJON VILLE", "MÂCON LOCHE TGV",
            "LYON (intramuros)"]


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def make_rows():
    rows = []
    for train_idx, number in enumerate(["6201", "6203", "6205", "6211"]):
        start = 6 * 60 + 45 * train_idx
        stop_times = [start + 22 * i for i in range(len(STATIONS))]
        for i in range(len(STATIONS)):
            for j in range(i + 1, len(STATIONS)):
                free = (i * 7 + j * 3 + train_idx) % 3 == 0
                rows.append({
                    "date": DAY.isoformat(),
                    "origine": STATIONS[i],
                    "destination": STATIONS[j],
                    "heure_depart": _hhmm(stop_times[i]),
                    "heure_arrivee": _hhmm(stop_times[j]),
                    "train_no": number,
                    "od_happy_card": "OUI" if free else "NON",
                    "entity": "INOUI",
                    "axe": "SUD EST",
                })
    return rows


class FakeAPI:
    """A tiny stand-in for the ODSQL records endpoint over a fixed row list."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, params):
        self.calls.append(dict(params))
        rows = self._filter(params.get("where", ""))
        if params.get("order_by") == "heure_depart":
            rows = sorted(rows, key=lambda r: r["heure_depart"])
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or 100)
        return {"total_count": len(rows), "results": rows[offset:offset + limit]}

    def _filter(self, where):
        rows = self.rows
        m = re.search(r"date=date'([\d-]+)'", where)
        if m:
            rows = [r for r in rows if r["date"] == m.group(1)]
        for field in ("origine", "destination", "train_no", "od_happy_card"):
            m = re.search(field + r'=\"([^\"]*)\"', where)
            if m:
                rows = [r for r in rows if r.get(field) == m.group(1)]
        return rows


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    """Every test starts with empty caches and no timetable extras."""
    clear_caches()
    _PAIR_CACHE.clear()
    monkeypatch.setattr(ter, "has_data", lambda: False)
    monkeypatch.setattr(bus, "has_data", lambda: False)
    yield
    clear_caches()
    _PAIR_CACHE.clear()


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI(make_rows())
    monkeypatch.setattr(SNCFMaxClient, "_request_uncached", lambda self, params: fake(params))
    return fake


@pytest.fixture
def client():
    """A client whose pages are 3 rows, so paging happens in a small fake."""
    from config import SNCFConfig
    return SNCFMaxClient(config=SNCFConfig(PAGE_SIZE=3, PAGE_WORKERS=2))


def _trips(trips):
    return sorted((t.train_number, str(t.origin), str(t.destination),
                   t.departure_time, t.arrival_time) for t in trips)


def test_sweep_pages_through_the_whole_station_day(api, client):
    expected = [r for r in api.rows if r["origine"] == "PARIS (intramuros)"]
    index = client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    assert len(index) == len(expected)
    assert len(api.calls) == -(-len(expected) // 3), "one request per page, no more"


def test_second_ask_for_the_same_day_costs_nothing(api, client):
    client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    before = len(api.calls)
    client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    assert len(api.calls) == before
    assert cache_stats()["days"]["hits"] >= 1


def test_sweep_and_pair_query_agree_for_every_pair(api, client):
    """The whole equivalence, over every ordered pair on the line, one day."""
    for side, fixed, others in (
        (SIDE_FROM, "PARIS (intramuros)", STATIONS[1:]),
        (SIDE_TO, "LYON (intramuros)", STATIONS[:-1]),
    ):
        index = client.day(fixed, side, DAY)
        for other in others:
            origin, destination = ((fixed, other) if side == SIDE_FROM
                                   else (other, fixed))
            free_pair, paid_pair = client.search_all_trips(
                origin=origin, destination=destination, trip_date=DAY)
            free_sweep, paid_sweep = client._split_rows(index.rows_to(other))
            assert _trips(free_sweep) == _trips(free_pair), (origin, destination)
            assert _trips(paid_sweep) == _trips(paid_pair), (origin, destination)


def test_decomposer_fetch_from_a_sweep_equals_the_pair_query(api, client):
    """Legs found by a download we already have must equal legs we'd ask for."""
    index = client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    decomposer = TripDecomposer()
    for destination in STATIONS[1:]:
        with_sweep = decomposer._fetch("PARIS (intramuros)", destination, DAY,
                                       only_max=False, sweeps=[index])
        without = decomposer._fetch("PARIS (intramuros)", destination, DAY,
                                    only_max=False)
        assert _trips(with_sweep) == _trips(without)


def test_decomposer_prefers_the_sweep_and_makes_no_train_queries(api, client):
    index = client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    before = len(api.calls)
    decomposer = TripDecomposer()
    trip = decomposer._fetch("PARIS (intramuros)", "LYON (intramuros)", DAY,
                             only_max=False, sweeps=[index])[0]
    stops = decomposer._ordered_stops(trip.train_number, DAY, [index])
    assert stops[0] == ("PARIS (intramuros)", trip.departure_time.strftime("%H:%M"))
    assert len(api.calls) == before, "the sweep already knows this train's stops"


def test_a_sweep_only_knows_stops_from_where_it_was_taken(api, client):
    index = client.day("SENS", SIDE_FROM, DAY)
    decomposer = TripDecomposer()
    stops = decomposer._ordered_stops("6201", DAY, [index])
    assert stops[0][0] == "SENS"


def test_without_a_sweep_the_train_is_asked_for_directly(api, client):
    decomposer = TripDecomposer()
    before = len(api.calls)
    stops = decomposer._ordered_stops("6201", DAY)
    assert len(api.calls) == before + 1
    assert stops[0] == ("PARIS (intramuros)", "06:00")
    assert stops[-1] == ("LYON (intramuros)", "07:28")


def test_broadcast_is_the_whole_day_not_a_capped_scan(api, client):
    """The hunt used to stop after 150 destinations; a sweep has no cap."""
    index = client.day("PARIS (intramuros)", SIDE_FROM, DAY)
    assert set(index.counterparts()) == set(STATIONS[1:])


def test_search_parts_are_independent_and_named(api, client):
    from network.core import search

    direct = search("paris", "lyon", DAY, parts=["direct"])
    assert direct.parts == ["direct"]
    assert direct.direct_free or direct.direct_paid
    assert direct.decompositions == [] and direct.descentres == []

    detours = search("paris", "lyon", DAY, parts=["detours"])
    assert detours.parts == ["detours"]
    assert detours.direct_free == [] and detours.descentres == []

    everything = search("paris", "lyon", DAY)
    assert everything.parts == ["descentres", "detours", "direct"]


def test_search_rejects_unknown_parts(api, client):
    from network.core import search

    with pytest.raises(ValueError):
        search("paris", "lyon", DAY, parts=["direct", "vibes"])


def test_decompose_false_means_direct_only(api, client):
    from network.core import search

    result = search("paris", "lyon", DAY, decompose=False,
                    parts=["direct", "detours"])
    assert result.parts == ["direct"]


def test_train_stops_page_past_one_hundred_pairs(api, client):
    """A train publishes a row per origin/destination pair: >100 on a long run."""
    stops = client.train_stops("6201", DAY)
    assert [s for s, _ in stops] == STATIONS
    assert [t for _, t in stops] == ["06:00", "06:22", "06:44", "07:06", "07:28"]
    assert len(api.calls) == -(-10 // 3), "10 pairs at 3 per page is 4 requests"


def test_train_stops_are_remembered(api, client):
    client.train_stops("6201", DAY)
    before = len(api.calls)
    client.train_stops("6201", DAY)
    assert len(api.calls) == before
