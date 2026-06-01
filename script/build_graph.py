#!/usr/bin/env python3
"""Build the station list + stop-wise ride graph straight from the SNCF API.

The graph is **stop-wise, not trip-wise**: we reconstruct each train's real
ordered stop sequence (by grouping a day's records by train_no and sorting
the origine/destination stops by time), then add an edge A->B for every pair
of stops where A comes before B on the same train.  So ``B in graph[A]``
means "a single train physically stops at A and then B" — which is exactly
what tells a direct/descentre ride apart from a change-of-train detour, with
no geographic guessing.

Writes (committed so the app needs no API round-trip at runtime):
  - src/network/stations.json     {"stations": [...], "coords": {NAME: [lat,lon]}}
  - src/network/routes_graph.json {station: [stations rideable on one train, ...]}

Run from anywhere:  python3 script/build_graph.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests

NETWORK_DIR = Path(__file__).resolve().parent.parent / "src" / "network"
BASE = "https://data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax"
RECORDS_URL = f"{BASE}/records"
EXPORT_URL = f"{BASE}/exports/json"

# Days ahead to sample when reconstructing train stop sequences.  A few
# weekdays unioned together catch trains that don't run every day.
SAMPLE_DAYS = [10, 11, 12, 13]

# Curated coordinates keyed by a substring that uniquely identifies a city in
# the API station names.  Every real station whose name contains the keyword
# inherits these coords (so LILLE FLANDRES / LILLE EUROPE / LILLE (intramuros)
# all resolve to Lille).  Used only for geographic detour pruning + the map;
# stations without a match simply aren't used as detour hubs.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "PARIS": (48.86, 2.35),
    "MASSY": (48.73, 2.26),
    "MARNE LA VALLEE": (48.87, 2.78),
    "AEROPORT ROISSY": (48.99, 2.57),
    "AEROPORT CHARLES": (48.99, 2.57),
    "LYON ST EXUPERY": (45.72, 5.08),
    "LYON": (45.76, 4.84),
    "MARSEILLE": (43.30, 5.38),
    "BORDEAUX": (44.83, -0.56),
    "TOULOUSE": (43.61, 1.45),
    "LILLE": (50.64, 3.07),
    "NICE": (43.70, 7.26),
    "NANTES": (47.22, -1.54),
    "STRASBOURG": (48.59, 7.73),
    "MONTPELLIER": (43.60, 3.88),
    "RENNES": (48.11, -1.67),
    "AVIGNON": (43.92, 4.79),
    "AIX EN PROVENCE": (43.46, 5.32),
    "AIX LES BAINS": (45.69, 5.91),
    "GRENOBLE": (45.19, 5.71),
    "DIJON": (47.32, 5.04),
    "ANGERS": (47.47, -0.56),
    "ST PIERRE DES CORPS": (47.39, 0.72),
    "TOURS": (47.39, 0.69),
    "LE MANS": (47.99, 0.19),
    "POITIERS": (46.58, 0.35),
    "METZ": (49.11, 6.18),
    "NANCY": (48.69, 6.17),
    "CHAMPAGNE ARDENNE": (49.26, 4.03),
    "REIMS": (49.26, 4.03),
    "LE CREUSOT": (46.81, 4.44),
    "MACON": (46.30, 4.82),
    "CHAMBERY": (45.57, 5.92),
    "ARRAS": (50.29, 2.78),
    "DOUAI": (50.37, 3.08),
    "BREST": (48.39, -4.48),
    "PERPIGNAN": (42.70, 2.88),
    "NIMES": (43.83, 4.37),
    "PAU": (43.29, -0.37),
    "BAYONNE": (43.50, -1.47),
    "BIARRITZ": (43.46, -1.54),
    "DAX": (43.72, -1.07),
    "LA ROCHELLE": (46.15, -1.15),
    "MULHOUSE": (47.75, 7.34),
    "COLMAR": (48.07, 7.36),
    "CALAIS": (50.95, 1.85),
    "BOULOGNE": (50.73, 1.61),
    "VANNES": (47.66, -2.76),
    "LORIENT": (47.75, -3.36),
    "QUIMPER": (48.00, -4.09),
    "ST MALO": (48.65, -2.00),
    "ST BRIEUC": (48.51, -2.77),
    "LAVAL": (48.07, -0.77),
    "ANGOULEME": (45.65, 0.16),
    "LIBOURNE": (44.91, -0.24),
    "AGEN": (44.20, 0.62),
    "MONTAUBAN": (44.02, 1.35),
    "BESANCON": (47.31, 5.95),
    "BELFORT": (47.59, 6.89),
    "BEZIERS": (43.34, 3.22),
    "SETE": (43.41, 3.70),
    "NARBONNE": (43.19, 3.01),
    "AGDE": (43.31, 3.47),
    "ANNECY": (45.90, 6.12),
    "ANNEMASSE": (46.19, 6.24),
    "ANTIBES": (43.58, 7.12),
    "CANNES": (43.55, 7.02),
    "ST RAPHAEL": (43.42, 6.77),
    "TOULON": (43.13, 5.93),
    "HYERES": (43.12, 6.13),
    "LES ARCS": (43.46, 6.48),
    "VALENCE": (44.99, 4.98),
    "VIENNE": (45.52, 4.87),
    "ORANGE": (44.14, 4.81),
    "MIRAMAS": (43.58, 5.00),
    "NIORT": (46.32, -0.46),
    "SURGERES": (46.10, -0.75),
    "ST NAZAIRE": (47.28, -2.21),
    "LE CROISIC": (47.29, -2.51),
    "LA BAULE": (47.29, -2.39),
    "REDON": (47.65, -2.08),
    "MONTLUCON": (46.34, 2.60),
    "ORLEANS": (47.91, 1.91),
    "LES AUBRAIS": (47.93, 1.91),
    "BLOIS": (47.59, 1.33),
    "VENDOME": (47.82, 1.06),
    "CHATELLERAULT": (46.82, 0.55),
    "LIMOGES": (45.84, 1.27),
    "BRIVE": (45.16, 1.53),
    "CAHORS": (44.45, 1.44),
    "AMIENS": (49.89, 2.30),
    "ROUEN": (49.45, 1.09),
    "LE HAVRE": (49.49, 0.11),
    "CAEN": (49.18, -0.35),
    "TROYES": (48.30, 4.08),
    "MULHOUSE": (47.75, 7.34),
    "THIONVILLE": (49.36, 6.17),
    "LUXEMBOURG": (49.60, 6.13),
    "SEDAN": (49.70, 4.94),
    "CHARLEVILLE": (49.77, 4.72),
    "EPINAL": (48.18, 6.45),
    "REMIREMONT": (48.02, 6.59),
    "ST DIE": (48.28, 6.95),
    "SAINT ETIENNE": (45.44, 4.39),
    "ST ETIENNE": (45.44, 4.39),
    "ROANNE": (46.04, 4.07),
    "BOURG EN BRESSE": (46.20, 5.22),
    "BOURG ST MAURICE": (45.62, 6.77),
    "MOUTIERS": (45.49, 6.53),
    "ALBERTVILLE": (45.68, 6.39),
    "CLERMONT": (45.78, 3.10),
    "VICHY": (46.13, 3.43),
    "NEVERS": (46.99, 3.16),
    "CARCASSONNE": (43.21, 2.35),
    "ALBI": (43.93, 2.15),
    "CASTRES": (43.60, 2.24),
    "TARBES": (43.23, 0.07),
    "LOURDES": (43.10, -0.05),
    "ARCACHON": (44.66, -1.16),
    "PERIGUEUX": (45.18, 0.71),
    "BERGERAC": (44.85, 0.49),
    "SAINT JEAN DE LUZ": (43.39, -1.66),
    "HENDAYE": (43.36, -1.78),
    "MENTON": (43.78, 7.50),
    "MONACO": (43.74, 7.42),
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (sncf-max graph builder)"})
    return s


def _paginate(session: requests.Session, select: str, group_by: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params = {"select": select, "group_by": group_by, "limit": 100, "offset": offset}
        r = session.get(RECORDS_URL, params=params, timeout=30)
        r.raise_for_status()
        res = r.json().get("results", [])
        out.extend(res)
        if len(res) < 100:
            break
        offset += 100
        if offset > 20000:  # safety
            break
        time.sleep(0.05)
    return out


def _export_day(session: requests.Session, date_str: str) -> list[dict]:
    """All records for one date via the exports endpoint (no offset cap)."""
    r = session.get(EXPORT_URL, params={"where": f"date=date'{date_str}'"}, timeout=180)
    r.raise_for_status()
    return r.json()


def _sort_key(hhmm: str) -> str:
    """Order stops by time, pushing after-midnight stops to the end so a
    train running e.g. 22:50 -> 00:40 keeps its real stop order."""
    if not hhmm:
        return "z"
    return ("1" + hhmm) if hhmm < "04:00" else ("0" + hhmm)


def _train_stops(records: list[dict]) -> list[str]:
    """Reconstruct one train's ordered stop list from its O/D records."""
    first_seen: dict[str, str] = {}
    for rec in records:
        o, d = rec.get("origine"), rec.get("destination")
        dep, arr = rec.get("heure_depart"), rec.get("heure_arrivee")
        if o and (o not in first_seen or _sort_key(dep) < _sort_key(first_seen[o])):
            first_seen[o] = dep or ""
        if d and (d not in first_seen or _sort_key(arr) < _sort_key(first_seen[d])):
            first_seen[d] = arr or ""
    return [s for s, _ in sorted(first_seen.items(), key=lambda kv: _sort_key(kv[1]))]


def _attach_coords(stations: list[str]) -> dict[str, list[float]]:
    """Map each station to coords if its name contains a known city keyword.

    Longer keywords win (so "AEROPORT ROISSY" beats nothing, "LE CREUSOT"
    beats a bare "LE").
    """
    keywords = sorted(CITY_COORDS, key=len, reverse=True)
    coords: dict[str, list[float]] = {}
    for name in stations:
        up = name.upper()
        for kw in keywords:
            if kw in up:
                lat, lon = CITY_COORDS[kw]
                coords[name] = [lat, lon]
                break
    return coords


def main() -> int:
    session = _session()

    print("fetching station list ...")
    station_rows = _paginate(session, "origine", "origine")
    stations = sorted({r["origine"] for r in station_rows if r.get("origine")})
    print(f"  {len(stations)} stations")

    print("reconstructing train stop sequences (stop-wise graph) ...")
    adj: dict[str, set[str]] = defaultdict(set)
    n_trains = 0
    for day in SAMPLE_DAYS:
        date_str = (date.today() + timedelta(days=day)).isoformat()
        rows = _export_day(session, date_str)
        by_train: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            tn = r.get("train_no")
            if tn:
                by_train[tn].append(r)
        for recs in by_train.values():
            stops = _train_stops(recs)
            n_trains += 1
            # every forward pair of stops on this train is rideable
            for i in range(len(stops)):
                for j in range(i + 1, len(stops)):
                    if stops[i] != stops[j]:
                        adj[stops[i]].add(stops[j])
        print(f"  {date_str}: {len(rows)} records, {len(by_train)} trains")
        time.sleep(0.1)
    graph = {o: sorted(dests) for o, dests in sorted(adj.items())}
    n_edges = sum(len(v) for v in graph.values())
    print(f"  {len(graph)} stations, {n_edges} stop-wise ride edges "
          f"(from {n_trains} train-days)")

    coords = _attach_coords(stations)
    print(f"  {len(coords)}/{len(stations)} stations have coordinates")

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    (NETWORK_DIR / "stations.json").write_text(
        json.dumps({"stations": stations, "coords": coords}, ensure_ascii=False, indent=0)
    )
    (NETWORK_DIR / "routes_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=0)
    )
    print(f"wrote {NETWORK_DIR/'stations.json'} and {NETWORK_DIR/'routes_graph.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
