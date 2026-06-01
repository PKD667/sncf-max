"""Date-aware TER / regional timetable, served from the GTFS-derived cache.

Loads ``ter_timetable.json`` (built by ``script/build_ter.py``) and answers
the questions trip composition needs:

  - :func:`legs_between`   real TER trips O->D on a date (as :class:`Trip`s)
  - :func:`destinations`   stations TER-reachable onward from a station/date
  - :func:`has_data`       is the cache present and does it cover this date?

Everything is keyed by UIC internally; stations are exposed by name (the
tgvmax name when the station is part of the MAX network, otherwise the GTFS
label, e.g. "Valence Ville").  TER legs are paid trips priced per-km by the
fare layer, never MAX.
"""

from __future__ import annotations

import json
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from models import Trip, Station, TripStatus

_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _cache() -> dict:
    path = _HERE / "ter_timetable.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _uic_to_name() -> Dict[str, str]:
    """UIC -> display station name (tgvmax name preferred, else GTFS label)."""
    c = _cache()
    out = {u: meta[0] for u, meta in c.get("stops", {}).items()}
    for name, uic in c.get("station_uic", {}).items():
        out[uic] = name  # prefer the MAX-network name where we have it
    return out


@lru_cache(maxsize=1)
def _name_to_uic() -> Dict[str, str]:
    return dict(_cache().get("station_uic", {}))


def has_data() -> bool:
    return bool(_cache().get("trips"))


def covers(d: date) -> bool:
    return d.isoformat() in _cache().get("by_date", {})


def _uic_of(name: str) -> Optional[str]:
    from network import stations as stn
    canon = stn.resolve(name) or name
    return _name_to_uic().get(canon) or _name_to_uic().get(name)


def _mins_to_time(m: int) -> time:
    return time((m // 60) % 24, m % 60)


def _make_trip(uic_o: str, dep_min: int, uic_d: str, arr_min: int,
               trip_date: date, op: str, line: str) -> Trip:
    names = _uic_to_name()
    return Trip(
        train_number=(line or "TER")[:24],
        origin=Station(name=names.get(uic_o, uic_o)),
        destination=Station(name=names.get(uic_d, uic_d)),
        departure_date=trip_date,
        departure_time=_mins_to_time(dep_min),
        arrival_time=_mins_to_time(arr_min),
        available_for_max=TripStatus.UNAVAILABLE,   # TER is never MAX
        entity=op,                                    # -> carrier == "TER"
    )


def _running(trip_date: date):
    c = _cache()
    for idx in c.get("by_date", {}).get(trip_date.isoformat(), []):
        yield c["trips"][idx]


def legs_between(origin: str, destination: str, trip_date: date) -> List[Trip]:
    """All TER trips that ride from *origin* to *destination* on *trip_date*."""
    uo, ud = _uic_of(origin), _uic_of(destination)
    if not uo or not ud or not covers(trip_date):
        return []
    out: List[Trip] = []
    for op, line, seq in _running(trip_date):
        oi = di = -1
        for i, (uic, _dep, _arr) in enumerate(seq):
            if uic == uo and oi < 0:
                oi = i
            elif uic == ud:
                di = i
        if 0 <= oi < di:
            out.append(_make_trip(seq[oi][0], seq[oi][1], seq[di][0], seq[di][2],
                                  trip_date, op, line))
    out.sort(key=lambda t: t.departure_time)
    return out


def destinations(origin: str, trip_date: date) -> List[str]:
    """Station names reachable from *origin* by a single TER trip on a date."""
    uo = _uic_of(origin)
    if not uo or not covers(trip_date):
        return []
    names = _uic_to_name()
    out: set = set()
    for _op, _line, seq in _running(trip_date):
        uics = [s[0] for s in seq]
        if uo in uics:
            i = uics.index(uo)
            for uic in uics[i + 1:]:
                out.add(names.get(uic, uic))
    return sorted(out)


def origins(destination: str, trip_date: date) -> List[str]:
    """Station names from which *destination* is reachable by one TER trip."""
    ud = _uic_of(destination)
    if not ud or not covers(trip_date):
        return []
    names = _uic_to_name()
    out: set = set()
    for _op, _line, seq in _running(trip_date):
        uics = [s[0] for s in seq]
        if ud in uics:
            i = len(uics) - 1 - uics[::-1].index(ud)
            for uic in uics[:i]:
                out.add(names.get(uic, uic))
    return sorted(out)
