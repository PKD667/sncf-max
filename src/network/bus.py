"""Date-aware coach timetable, served from the GTFS-derived cache.

Covers BlaBlaCar Bus + FlixBus (open GTFS via transport.data.gouv.fr),
built by ``script/build_bus.py``. Same shape and API as ``ter.py``:

  - :func:`legs_between`   coach trips O->D on a date (as :class:`Trip`s)
  - :func:`destinations`   cities coach-reachable onward from a station/date
  - :func:`origins`        cities a destination is reachable from
  - :func:`has_data` / :func:`covers`

Joining coaches to trains is city-level (coach stops carry no UIC):
a coach stop belongs to a train station when the city tokens match
(either side's tokens contained in the other's) and, where both ends
have coordinates, the stops are within 30 km - or within 6 km on
proximity alone (covers same-metro different names, e.g. Tours vs
St-Pierre-des-Corps). Coach legs are paid trips priced per-km by the
fare layer (dynamic coach pricing is not in open data), never MAX.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from models import Trip, Station, TripStatus

_HERE = Path(__file__).resolve().parent

#: Tokens ignored for city matching (generic place words).
_CITY_STOPWORDS = {
    "TGV", "VILLE", "CENTRE", "CENTER", "CITY", "CENTRAL", "DOWNTOWN",
    "GARE", "INTRAMUROS", "ST", "SAINT", "DE", "DU", "DES", "LA", "LE",
    "LES", "SUR", "SOUS", "EN", "AU", "AUX", "ET", "D", "L",
    "AEROPORT", "AIRPORT", "AEROGARE", "TERMINAL",
}

#: Max stop distance (km) for a token-matched pair; tighter bound when
#: only proximity links two differently-named stops in one metro area.
_MATCH_KM = 30.0
_METRO_KM = 6.0


@lru_cache(maxsize=1)
def _cache() -> dict:
    path = _HERE / "bus_timetable.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _norm(text: str) -> str:
    n = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", n.upper())).strip()


def _tokens(name: str) -> Set[str]:
    core = re.split(r"\s-\s|,|;", re.sub(r"\(.*?\)", " ", name))[0]
    return {t for t in _norm(core).split() if t and t not in _CITY_STOPWORDS}


def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _train_coords(name: str) -> Optional[Tuple[float, float]]:
    try:
        from network import stations as stn
    except Exception:
        return None
    try:
        return stn.coords(name)
    except Exception:
        return None


@lru_cache(maxsize=4096)
def _station_cities(station: str) -> frozenset:
    """Bus-stop sids in the same city as a train station name."""
    tt = _tokens(station)
    if not tt:
        return frozenset()
    tc = _train_coords(station)
    out = set()
    for sid, meta in _cache().get("stops", {}).items():
        bt = _tokens(meta[0].split(" - ")[0] if " - " in meta[0] else meta[0])
        if not bt:
            continue
        if bt <= tt or tt <= bt:
            if tc is not None and meta[1] is not None:
                if _haversine(tc, (meta[1], meta[2])) > _MATCH_KM:
                    continue
            out.add(sid)
            continue
        if tc is not None and meta[1] is not None:
            if _haversine(tc, (meta[1], meta[2])) <= _METRO_KM:
                out.add(sid)
    return frozenset(out)


def has_data() -> bool:
    return bool(_cache().get("trips"))


def covers(d: date) -> bool:
    return d.isoformat() in _cache().get("by_date", {})


def _mins_to_time(m: int) -> time:
    return time((m // 60) % 24, m % 60)


def _stop_name(sid: str) -> str:
    meta = _cache().get("stops", {}).get(sid)
    return meta[0] if meta else sid


def _make_trip(sid_o: str, dep_min: int, sid_d: str, arr_min: int,
               trip_date: date, op: str, line: str) -> Trip:
    no = (line or "").strip()[:24]
    if no.isdigit():
        no = f"Bus {no}"
    return Trip(
        train_number=no or op,
        origin=Station(name=_stop_name(sid_o)),
        destination=Station(name=_stop_name(sid_d)),
        departure_date=trip_date,
        departure_time=_mins_to_time(dep_min),
        arrival_time=_mins_to_time(arr_min),
        available_for_max=TripStatus.UNAVAILABLE,   # coaches are never MAX
        entity=op,                                    # -> carrier == "BUS"
    )


def _running(trip_date: date):
    c = _cache()
    for idx in c.get("by_date", {}).get(trip_date.isoformat(), []):
        yield c["trips"][idx]


def legs_between(origin: str, destination: str, trip_date: date) -> List[Trip]:
    """All coach trips from *origin*'s city to *destination*'s city on a date."""
    if not covers(trip_date):
        return []
    so, sd = _station_cities(origin), _station_cities(destination)
    if not so or not sd:
        return []
    out: List[Trip] = []
    for op, line, seq in _running(trip_date):
        oi = di = -1
        for i, (sid, _dep, _arr) in enumerate(seq):
            if sid in so and oi < 0:
                oi = i
            elif sid in sd:
                di = i
        if 0 <= oi < di:
            # Skip city-internal hops (same stop area served twice).
            if seq[oi][0] == seq[di][0]:
                continue
            out.append(_make_trip(seq[oi][0], seq[oi][1], seq[di][0], seq[di][2],
                                  trip_date, op, line))
    # Deduplicate same (departure, arrival, operator) pairs.
    seen: Set[tuple] = set()
    uniq: List[Trip] = []
    for t in sorted(out, key=lambda t: t.departure_time):
        key = (t.departure_time, t.arrival_time, t.train_number)
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


def _city_names(sids) -> List[str]:
    names = {_stop_name(s) for s in sids}
    return sorted(names)


def destinations(origin: str, trip_date: date) -> List[str]:
    """Coach stop names reachable onward from *origin*'s city on a date."""
    so = _station_cities(origin)
    if not so or not covers(trip_date):
        return []
    out: Set[str] = set()
    for _op, _line, seq in _running(trip_date):
        sids = [s[0] for s in seq]
        for i, sid in enumerate(sids):
            if sid in so:
                for later in sids[i + 1:]:
                    if later not in so:
                        out.add(_stop_name(later))
    return sorted(out)


def origins(destination: str, trip_date: date) -> List[str]:
    """Coach stop names from which *destination*'s city is reachable."""
    sd = _station_cities(destination)
    if not sd or not covers(trip_date):
        return []
    out: Set[str] = set()
    for _op, _line, seq in _running(trip_date):
        sids = [s[0] for s in seq]
        for i, sid in enumerate(sids):
            if sid in sd:
                for earlier in sids[:i]:
                    if earlier not in sd:
                        out.add(_stop_name(earlier))
    return sorted(out)


def stop_coords(name: str) -> Optional[Tuple[float, float]]:
    """Coordinates of a coach stop by exact name (for fare distances)."""
    for _sid, meta in _cache().get("stops", {}).items():
        if meta[0] == name and meta[1] is not None:
            return (meta[1], meta[2])
    return None
