"""TGV network graph.

Two graphs:

1.  **Static TGV topology** (``tgv_graph.json`` extracted from SNCF NETEX):
    which stations are physically connected by TGV lines.
    Used to bound the search space.

2.  **Dynamic free-trip graph** built from real-time API results:
    each edge is an *available* MAX trip right now, weighted by
    actual trip duration.  Dijkstra on this graph gives the
    shortest all-MAX path between any two stations.
"""

from __future__ import annotations

import heapq
import json
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Deque

from models import Trip

# ---------------------------------------------------------------------------
# Static graph (cached from NETEX)
# ---------------------------------------------------------------------------

_GRAPH: Optional[Dict[str, List[str]]] = None
_NORM: Optional[Dict[str, str]] = None


def graph() -> Dict[str, List[str]]:
    global _GRAPH
    if _GRAPH is None:
        path = Path(__file__).with_name("tgv_graph.json")
        with open(path) as f:
            _GRAPH = json.load(f)
    return _GRAPH


def _normalize() -> Dict[str, str]:
    global _NORM
    if _NORM is None:
        _NORM = {k.upper(): k for k in graph()}
    return _NORM


def resolve(name: str) -> Optional[str]:
    """Map any station name (API, alias, etc.) to a graph node."""
    from config import get_station_name

    full = get_station_name(name)
    if full != name:
        mapped = _alias_api_to_graph(full)
        if mapped is not None:
            return mapped

    upper = name.upper().strip()
    m = _normalize()
    if upper in m:
        return m[upper]
    for norm, orig in m.items():
        if upper in norm or norm in upper:
            return orig
    return None


def _alias_api_to_graph(api_name: str) -> Optional[str]:
    mapping: Dict[str, str] = {
        "PARIS (intramuros)": "Paris Gare de Lyon Hall 1 - 2",
        "PARIS GARE DE LYON": "Paris Gare de Lyon Hall 1 - 2",
        "PARIS MONTPARNASSE 1 ET 2": "Paris Montparnasse Hall 1 - 2",
        "PARIS NORD": "Paris Nord",
        "PARIS EST": "Paris Est",
        "LYON (intramuros)": "Lyon Part Dieu",
        "LYON PART DIEU": "Lyon Part Dieu",
        "MARSEILLE ST CHARLES": "Marseille Saint-Charles",
        "BORDEAUX ST JEAN": "Bordeaux Saint-Jean",
        "LILLE FLANDRES": "Lille Flandres",
        "LILLE EUROPE": "Lille Europe",
        "STRASBOURG": "Strasbourg",
        "NANTES": "Nantes",
        "RENNES": "Rennes",
        "TOULOUSE MATABIAU": "Toulouse Matabiau",
        "MONTPELLIER ST ROCH": "Montpellier Saint-Roch",
        "NICE VILLE": "Nice Ville",
        "AVIGNON TGV": "Avignon TGV",
        "DIJON VILLE": "Dijon",
        "GRENOBLE": "Grenoble",
        "NANCY": "Nancy",
        "METZ VILLE": "Metz",
        "REIMS": "Champagne-Ardenne TGV",
        "LE MANS": "Le Mans",
        "POITIERS": "Poitiers",
        "PERPIGNAN": "Perpignan",
        "NIMES": "Nimes Centre",
        "CHAMBERY CHALLES LES EAUX": "Chambery-Challes-les-Eaux",
        "ST PIERRE DES CORPS": "St-Pierre-des-Corps",
        "ARRAS": "Arras",
        "DOUAI": "Douai",
        "ANGERS ST LAUD": "Angers Saint-Laud",
        "LA ROCHELLE": "La Rochelle Ville",
        "MULHOUSE": "Mulhouse",
        "COLMAR": "Colmar",
        "BREST": "Brest",
        "BAYONNE": "Bayonne",
        "PAU": "Pau",
        "BIARRITZ": "Biarritz",
        "CALAIS": "Calais Ville",
    }
    if api_name in mapping:
        return mapping[api_name]
    for k, v in mapping.items():
        if k.upper() in api_name.upper() or api_name.upper() in k.upper():
            return v
    return None


def neighbors(node: str) -> List[str]:
    return graph().get(node, [])


# ---------------------------------------------------------------------------
# Dijkstra on the dynamic free-trip graph
# ---------------------------------------------------------------------------

def _normalize_station(trip: Trip) -> str:
    """Return the best graph-key for a trip's origin/destination."""
    o = str(trip.origin)
    return resolve(o) or o


def dijkstra_free_path(
    free_trips: List[Trip],
    start: str,
    target: str,
    min_connection: int = 15,
    max_connection: int = 120,
) -> Optional[List[Trip]]:
    """Dijkstra on the dynamic free-MAX trip graph.

    Nodes are stations; edges are actual free trips from the API.
    Edge weight = trip duration (minutes).
    Connection time between arrival of one trip and departure of next
    must be between *min_connection* and *max_connection*.

    Returns the shortest path (by total travel+connection time) from
    *start* to *target*, or None if no free path exists.
    """
    start_node = resolve(start)
    target_node = resolve(target)
    if not start_node or not target_node:
        return None


def find_paths(start: str, target: str, max_hops: int = 3) -> List[List[str]]:
    """BFS: find all multi-hop paths in the static TGV network graph.

    Used by the decomposition module to enumerate candidate intermediate
    stations.  Returns paths as lists of graph node names.
    """
    g = graph()
    start = resolve(start)
    target = resolve(target)
    if not start or not target or start not in g:
        return []

    paths: List[List[str]] = []
    if target in g.get(start, []):
        paths.append([start, target])

    queue: Deque[Tuple[str, List[str]]] = deque()
    queue.append((start, [start]))
    visited: Set[str] = set()

    while queue:
        node, path = queue.popleft()
        if len(path) > max_hops:
            continue
        for nb in g.get(node, []):
            if nb in path:
                continue
            new_path = path + [nb]
            if nb == target:
                paths.append(new_path)
            elif len(new_path) < max_hops + 1:
                state = f"{nb}:{len(new_path)}"
                if state not in visited:
                    visited.add(state)
                    queue.append((nb, new_path))
    return paths

    # --- Build adjacency: station -> list of (next_trip, cost_min) ---
    g: Dict[str, List[Tuple[Trip, float]]] = {}
    for t in free_trips:
        if not t.is_free:
            continue
        src = _normalize_station(t)
        if src is None:
            continue
        cost = t.duration.total_seconds() / 60.0
        g.setdefault(src, []).append((t, cost))

    # Dijkstra: (total_cost, -departure_ts, node, path)
    # We negate departure timestamp so heapq picks earlier departures for same cost.
    heap: List[Tuple[float, float, str, List[Trip]]] = []
    best: Dict[str, Tuple[float, float, List[Trip]]] = {}

    # Seed: any trip from start_node
    for trip, cost in g.get(start_node, []):
        state_key = str(trip.destination)
        heapq.heappush(heap, (cost, -trip.departure_datetime.timestamp(), state_key, [trip]))

    while heap:
        total_cost, _neg_dep, node, path = heapq.heappop(heap)

        last_arrival = path[-1].arrival_datetime
        total_duration = total_cost

        if node == target_node:
            return path

        if node not in g:
            continue

        for next_trip, next_cost in g[node]:
            conn = (next_trip.departure_datetime - last_arrival).total_seconds() / 60.0
            if conn < min_connection or conn > max_connection:
                continue

            new_cost = total_duration + conn + next_cost
            new_dep = next_trip.departure_datetime.timestamp()
            state_key = str(next_trip.destination)

            if state_key in best:
                prev_cost, prev_dep, _ = best[state_key]
                if new_cost >= prev_cost:
                    continue

            new_path = path + [next_trip]
            best[state_key] = (new_cost, new_dep, new_path)
            heapq.heappush(heap, (new_cost, -new_dep, state_key, new_path))

    return None
