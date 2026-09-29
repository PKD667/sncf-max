#!/usr/bin/env python3
"""Build a compact, date-aware coach timetable from open GTFS feeds.

Sources (both on transport.data.gouv.fr, daily updates, no fares inside):
  - BlaBlaCar Bus Europe (small, drive-hosted GTFS)
  - FlixBus Europe (30MB GTFS; FlixTrain agency trips are excluded)

Output ``src/network/bus_timetable.json``:
  {
    "built": "YYYY-MM-DD",
    "stops":  {sid: [name, lat, lon, citykey]},
    "trips":  [[op, line, [[sid, dep_min, arr_min], ...]], ...],  # by index
    "by_date": {"YYYY-MM-DD": [trip_idx, ...]},                  # next WINDOW_DAYS
    "cities": {CITYKEY: [sid, ...]},
  }

Unlike the SNCF GTFS (dates-only services), coach feeds use weekly
calendar.txt patterns, so both calendar.txt and calendar_dates.txt
exceptions are honoured. Times are minutes past midnight (can exceed
1440 for overnight coaches). Rerun periodically to roll the window
forward. Run:  python3 script/build_bus.py
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
from datetime import date, timedelta
from pathlib import Path

csv.field_size_limit(10 ** 7)

NETWORK_DIR = Path(__file__).resolve().parent.parent / "src" / "network"
WINDOW_DAYS = 45

FEEDS = {
    "BlaBlaBus": "https://www.data.gouv.fr/api/1/datasets/r/fd54f81f-4389-4e73-be75-491133d011c3",
    "FlixBus": "https://www.data.gouv.fr/api/1/datasets/r/30d94e83-48a4-4c44-8a96-c082377f5221",
}

# FlixBus also publishes rail trips under this agency: not coaches, skip.
SKIP_AGENCIES = {"FLIXTRAIN-eu", "FlixTrain-eu"}
# GTFS route_type 2 = rail. Keep road (3 = bus) and unknown.
SKIP_ROUTE_TYPES = {"2"}

OP_LABEL = {"BlaBlaCar Bus": "BlaBlaBus", "FlixBus-eu": "FlixBus"}


def _to_min(hms: str):
    if not hms:
        return None
    h, m, s = hms.split(":")
    return int(h) * 60 + int(m)


def _norm(text: str) -> str:
    n = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", n.upper())).strip()


_TRAILING = {"CITY", "CENTER", "CENTRE", "CENTRAL", "DOWNTOWN"}


def _city_key(stop_name: str) -> str:
    """Coarse city for a coach stop: 'Aix-en-Provence - Krypton P&R' -> 'AIX EN PROVENCE'."""
    head = re.split(r"\s-\s|,|;", stop_name)[0]
    head = re.sub(r"\(.*?\)", " ", head)
    toks = [t for t in _norm(head).split() if t]
    while toks and toks[-1] in _TRAILING:
        toks.pop()
    return " ".join(toks)


def _csv(z: zipfile.ZipFile, name: str):
    try:
        return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))
    except KeyError:
        return []


def _open_feed(label: str, url: str) -> zipfile.ZipFile:
    print(f"downloading {label} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "sncf-max/ter-bus-builder"})
    data = urllib.request.urlopen(req, timeout=600).read()
    return zipfile.ZipFile(io.BytesIO(data))


def _service_dates(z: zipfile.ZipFile, today: date) -> dict:
    """service_id -> {iso dates in window}, from calendar.txt +/- exceptions."""
    window = [(today + timedelta(days=i)) for i in range(WINDOW_DAYS)]
    iso_of = {d.strftime("%Y%m%d"): d.isoformat() for d in window}
    out: dict = defaultdict(set)

    for r in _csv(z, "calendar.txt"):
        days = [r.get(d, "0") == "1" for d in
                ("monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday")]
        start, end = r.get("start_date", ""), r.get("end_date", "")
        for d in window:
            ymd = d.strftime("%Y%m%d")
            if start <= ymd <= end and days[d.weekday()]:
                out[r["service_id"]].add(iso_of[ymd])

    for r in _csv(z, "calendar_dates.txt"):
        ymd = r.get("date", "")
        if ymd not in iso_of:
            continue
        if r.get("exception_type") == "1":
            out[r["service_id"]].add(iso_of[ymd])
        elif r.get("exception_type") == "2":
            out[r["service_id"]].discard(iso_of[ymd])

    return out


def _build_feed(label: str, z: zipfile.ZipFile, service_dates: dict,
                trips: list, by_date: dict, stops_out: dict,
                stop_seen: dict) -> int:
    agencies = {}
    for r in _csv(z, "agency.txt"):
        agencies[r["agency_id"]] = r.get("agency_name", r["agency_id"])
    routes = {}
    for r in _csv(z, "routes.txt"):
        if r.get("route_type") in SKIP_ROUTE_TYPES:
            continue
        if r.get("agency_id") in SKIP_AGENCIES:
            continue
        routes[r["route_id"]] = (
            r.get("agency_id", ""),
            r.get("route_short_name", "") or r.get("route_long_name", ""),
            r.get("route_long_name", "") or r.get("route_short_name", ""),
        )

    trip_svc, trip_route, trip_label = {}, {}, {}
    for r in _csv(z, "trips.txt"):
        if r.get("route_id") not in routes:
            continue
        trip_svc[r["trip_id"]] = r.get("service_id", "")
        trip_route[r["trip_id"]] = r["route_id"]
        trip_label[r["trip_id"]] = (
            r.get("trip_short_name", "") or r.get("trip_headsign", "")
        )

    seqs: dict = defaultdict(list)
    for r in _csv(z, "stop_times.txt"):
        if r.get("trip_id") not in trip_route:
            continue
        dep, arr = _to_min(r.get("departure_time", "")), _to_min(r.get("arrival_time", ""))
        if dep is None and arr is None:
            continue
        seqs[r["trip_id"]].append(
            (int(r.get("stop_sequence", 0)), r["stop_id"], dep or arr, arr or dep))

    for r in _csv(z, "stops.txt"):
        sid = r.get("stop_id", "")
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (ValueError, KeyError, TypeError):
            continue
        name = r.get("stop_name", "") or sid
        if sid not in stop_seen:
            stop_seen[sid] = [name, lat, lon, _city_key(name)]

    n = 0
    for trip_id, stops in seqs.items():
        if len(stops) < 2:
            continue
        dates = service_dates.get(trip_svc.get(trip_id, ""))
        if not dates:
            continue
        stops.sort(key=lambda s: s[0])
        agency_id, short, long = routes[trip_route[trip_id]]
        agency = agencies.get(agency_id, label)
        op = OP_LABEL.get(agency, agency)
        line = trip_label.get(trip_id, "") or short or long
        seq = [[sid, dep, arr] for _, sid, dep, arr in stops]
        idx = len(trips)
        trips.append([op, line, seq])
        for s in seq:
            sid = s[0]
            if sid in stop_seen and sid not in stops_out:
                stops_out[sid] = stop_seen[sid]
        for iso in dates:
            by_date[iso].append(idx)
        n += 1
    return n


def main() -> int:
    today = date.today()
    trips: list = []
    by_date: dict = defaultdict(list)
    stops_out: dict = {}
    stop_seen: dict = {}

    for label, url in FEEDS.items():
        z = _open_feed(label, url)
        service_dates = _service_dates(z, today)
        n = _build_feed(label, z, service_dates, trips, by_date,
                        stops_out, stop_seen)
        z.close()
        print(f"  {label}: {n} coach trips")

    cities: dict = defaultdict(list)
    for sid, meta in stops_out.items():
        cities[meta[3]].append(sid)

    out = {
        "built": today.isoformat(),
        "stops": stops_out,
        "trips": trips,
        "by_date": {k: sorted(v) for k, v in by_date.items()},
        "cities": dict(cities),
    }
    path = NETWORK_DIR / "bus_timetable.json"
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    size = path.stat().st_size / 1e6
    print(f"  {len(trips)} bus trips, {len(stops_out)} stops, "
          f"{len(by_date)} dates, {len(cities)} cities")
    print(f"wrote {path} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
