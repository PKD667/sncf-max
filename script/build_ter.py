#!/usr/bin/env python3
"""Build a compact, date-aware TER/regional timetable from the SNCF GTFS feed.

The national GTFS (``Export_OpenData_SNCF_GTFS_NewTripId.zip``) is the only
public source of TER timetables.  Its ``stop_id`` encodes both the UIC code
and the operator (``StopPoint:OCETrain TER-87763029``), so we can pick out
regional services and key everything by UIC — the same key the MAX (tgvmax)
network can be joined on.

Output ``src/network/ter_timetable.json``:
  {
    "built": "YYYY-MM-DD",
    "stops":  {uic: [name, lat, lon]},
    "trips":  [[op, line, [[uic, dep_min, arr_min], ...]], ...],   # by index
    "by_date":{"YYYY-MM-DD": [trip_idx, ...]},        # next WINDOW_DAYS only
    "station_uic": {tgvmax_station_name: uic}         # join to the MAX network
  }

Times are minutes past midnight (can exceed 1440 for after-midnight stops).
The by_date window keeps the file small and date-accurate; rerun periodically
to roll it forward.  Run:  python3 script/build_ter.py
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

csv.field_size_limit(10 ** 7)

NETWORK_DIR = Path(__file__).resolve().parent.parent / "src" / "network"
GTFS_URL = ("https://eu.ftp.opendatasoft.com/sncf/plandata/"
            "Export_OpenData_SNCF_GTFS_NewTripId.zip")
WINDOW_DAYS = 45
# Operators we treat as regional / connector services (priced per-km as TER).
REGIONAL_OPS = {"Train TER", "Car TER", "Navette", "TramTrain"}

_STOP_RE = re.compile(r"OCE(.+?)-(\d+)")


def _op_uic(stop_id: str):
    """(operator, uic) from a GTFS stop_id, or (None, uic) for a StopArea."""
    m = _STOP_RE.search(stop_id)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _to_min(hms: str):
    if not hms:
        return None
    h, m, s = hms.split(":")
    return int(h) * 60 + int(m)


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]+", " ", n.upper()).strip()


def _tokens(name: str) -> set:
    return set(_norm(name).split())


def _open_gtfs() -> zipfile.ZipFile:
    cached = Path("/tmp/gtfs/g.zip")
    if cached.exists():
        return zipfile.ZipFile(cached)
    print("downloading GTFS ...")
    data = urllib.request.urlopen(GTFS_URL, timeout=300).read()
    return zipfile.ZipFile(io.BytesIO(data))


def _csv(z: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


def main() -> int:
    z = _open_gtfs()

    print("stops ...")
    # uic -> (name, lat, lon); prefer the StopArea label.
    stop_meta: dict = {}
    for r in _csv(z, "stops.txt"):
        op, uic = _op_uic(r["stop_id"])
        if not uic:
            continue
        is_area = r["stop_id"].startswith("StopArea")
        if uic not in stop_meta or is_area:
            try:
                lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
            except (ValueError, KeyError):
                lat = lon = None
            stop_meta[uic] = (r["stop_name"], lat, lon)

    print("routes ...")
    route_name = {}
    for r in _csv(z, "routes.txt"):
        route_name[r["route_id"]] = r.get("route_long_name") or r.get("route_short_name") or ""

    print("trips ...")
    trip_service = {}
    trip_route = {}
    for r in _csv(z, "trips.txt"):
        trip_service[r["trip_id"]] = r["service_id"]
        trip_route[r["trip_id"]] = r["route_id"]

    print("stop_times (regional only) ...")
    # trip_id -> list of (seq, uic, arr_min, dep_min, op)
    seqs: dict = defaultdict(list)
    for r in _csv(z, "stop_times.txt"):
        op, uic = _op_uic(r["stop_id"])
        if op not in REGIONAL_OPS or not uic:
            continue
        seqs[r["trip_id"]].append((
            int(r["stop_sequence"]), uic,
            _to_min(r["arrival_time"]), _to_min(r["departure_time"]), op))

    print("calendar (window only) ...")
    today = date.today()
    window = {(today + timedelta(days=i)).strftime("%Y%m%d"): (today + timedelta(days=i)).isoformat()
              for i in range(WINDOW_DAYS)}
    service_dates: dict = defaultdict(list)  # service_id -> [iso dates in window]
    for r in _csv(z, "calendar_dates.txt"):
        if r.get("exception_type") != "1":
            continue
        d = r["date"]
        if d in window:
            service_dates[r["service_id"]].append(window[d])

    print("assembling trips ...")
    trips = []           # [op, line, [[uic, dep_min, arr_min], ...]]
    by_date = defaultdict(list)
    used_uics = set()
    for trip_id, stops in seqs.items():
        if len(stops) < 2:
            continue
        svc = trip_service.get(trip_id)
        dates = service_dates.get(svc)
        if not dates:
            continue  # doesn't run in our window
        stops.sort(key=lambda s: s[0])
        op = stops[0][4]
        line = route_name.get(trip_route.get(trip_id, ""), "")
        seq = [[uic, dep, arr] for _, uic, arr, dep, _ in stops]
        idx = len(trips)
        trips.append([op, line, seq])
        for s in seq:
            used_uics.add(s[0])
        for iso in dates:
            by_date[iso].append(idx)

    stops_out = {u: [stop_meta[u][0], stop_meta[u][1], stop_meta[u][2]]
                 for u in used_uics if u in stop_meta}

    # join the MAX (tgvmax) station names to UIC via GTFS station names + coords
    print("joining MAX stations to UIC ...")
    station_uic = _join_stations(stops_out)

    out = {
        "built": today.isoformat(),
        "stops": stops_out,
        "trips": trips,
        "by_date": by_date,
        "station_uic": station_uic,
    }
    path = NETWORK_DIR / "ter_timetable.json"
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    size = path.stat().st_size / 1e6
    print(f"  {len(trips)} TER trips, {len(stops_out)} stops, "
          f"{len(by_date)} dates, {len(station_uic)} MAX stations joined")
    print(f"wrote {path} ({size:.1f} MB)")
    return 0


def _join_stations(stops_out: dict) -> dict:
    """Map each tgvmax station name to a GTFS UIC by token overlap + proximity."""
    import math
    data = json.loads((NETWORK_DIR / "stations.json").read_text())
    coords = data.get("coords", {})

    # index GTFS stops by token set
    gstops = [(u, _tokens(meta[0]), meta[1], meta[2]) for u, meta in stops_out.items()]

    def dist(a, b, lat, lon):
        if None in (a, b, lat, lon):
            return 1e9
        return math.hypot(a - lat, b - lon)

    out = {}
    for name in data["stations"]:
        toks = _tokens(name)
        if not toks:
            continue
        c = coords.get(name)
        best = None
        best_score = 0.0
        for uic, gtoks, glat, glon in gstops:
            if not gtoks:
                continue
            inter = len(toks & gtoks)
            if not inter:
                continue
            jac = inter / len(toks | gtoks)
            score = jac
            if c:  # nudge by coordinate proximity when we have it
                d = dist(c[0], c[1], glat, glon)
                if d < 0.15:        # ~15 km
                    score += 0.3
                elif d > 1.0:       # far away — probably a namesake elsewhere
                    score -= 0.3
            if score > best_score:
                best_score, best = score, uic
        if best and best_score >= 0.45:
            out[name] = best
    return out


if __name__ == "__main__":
    sys.exit(main())
