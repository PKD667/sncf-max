"""Canonical TGV Max station registry.

Single source of truth for station data, all keyed by the **API station
names** used by data.sncf.com (e.g. ``"PARIS (intramuros)"``).  Loaded from
the committed caches that ``script/build_graph.py`` generates straight from
the API:

  - ``stations.json``     full station list + curated coordinates
  - ``routes_graph.json`` real directed adjacency (origine -> destinations)

This replaces the old NETEX ``tgv_graph.json`` + hand-written name mapping,
which used a different name space and was badly out of sync with the API.

Provides:
  - :func:`all_stations`     every API station name
  - :func:`neighbors`        direct TGV connections from a station
  - :func:`coords`           (lat, lon) for a station, if known
  - :func:`distance_km`      great-circle distance between two stations
  - :func:`on_the_way`       is X a sensible detour between O and D?
  - :func:`resolve`          alias / fuzzy name -> canonical API name
  - :func:`display_name`     pretty, human-readable label for a station
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _data() -> dict:
    with open(_HERE / "stations.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _graph() -> Dict[str, List[str]]:
    with open(_HERE / "routes_graph.json", encoding="utf-8") as f:
        return json.load(f)


def all_stations() -> List[str]:
    return list(_data()["stations"])


def graph() -> Dict[str, List[str]]:
    """Directed adjacency: API origin name -> list of API destination names."""
    return _graph()


# ---------------------------------------------------------------------------
# Name resolution (alias / fuzzy -> canonical API name)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _upper_index() -> Dict[str, str]:
    return {s.upper(): s for s in _data()["stations"]}


@lru_cache(maxsize=2048)
def resolve(name: str) -> Optional[str]:
    """Resolve any name (config alias, casing variant, substring) to the
    canonical API station name, or ``None`` if nothing plausible matches."""
    if not name:
        return None

    # config alias (e.g. "paris" -> "PARIS (intramuros)")
    from config import get_station_name
    candidate = get_station_name(name)

    idx = _upper_index()
    up = candidate.upper()
    if up in idx:
        return idx[up]

    # exact match on the raw name too
    if name.upper() in idx:
        return idx[name.upper()]

    # word-boundary match: the query must be a prefix of the station name or
    # appear as a whole token in it (avoids "cdg" alias junk like ARLES ⊂
    # CHARLES).  Prefer a prefix hit, then the shortest matching name.
    def _tokens(u: str) -> set:
        return set(re.split(r"[ ().\-]+", u))

    matches = [s for u, s in idx.items() if u.startswith(up) or up in _tokens(u)]
    if matches:
        return min(matches, key=lambda s: (not s.upper().startswith(up), len(s)))

    # last resort: match on the leading city word.  The API aggregates Paris/
    # Lyon termini into "PARIS (intramuros)" / "LYON (intramuros)", so names
    # like "PARIS GARE DE LYON" or "LYON PART DIEU" only resolve this way.
    first = up.split()[0] if up.split() else ""
    if len(first) >= 3:
        prefixed = [s for u, s in idx.items() if u.startswith(first)]
        if prefixed:
            return min(prefixed, key=len)
    return None


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def neighbors(name: str) -> List[str]:
    """Stations directly reachable by TGV from *name* (API name or alias)."""
    canon = resolve(name) or name
    return _graph().get(canon, [])


def rideable(origin: str, destination: str) -> bool:
    """Can you reach *destination* from *origin* on a single train?

    True iff some train physically stops at the origin and then the
    destination — i.e. the destination is among the origin's stop-wise
    neighbours.  This is exact (built from real train stop sequences), so it
    tells direct/descentre journeys apart from change-of-train detours
    without any geographic guessing.
    """
    o = resolve(origin) or origin
    d = resolve(destination) or destination
    return d in _graph().get(o, [])


# alias kept for older callers
has_edge = rideable


def transfer_stations(origin: str, destination: str) -> List[str]:
    """Stations where you can change trains to get from origin to destination.

    A valid transfer M is rideable from the origin *and* can ride onward to
    the destination (origin -> M -> destination, all on real stops).
    """
    o = resolve(origin) or origin
    d = resolve(destination) or destination
    g = _graph()
    from_o = set(g.get(o, []))
    return sorted(m for m in from_o
                  if m not in (o, d) and d in g.get(m, []))


# coordinates are kept only for drawing the map, never for routing
def coords(name: str) -> Optional[Tuple[float, float]]:
    canon = resolve(name) or name
    c = _data()["coords"].get(canon)
    return (c[0], c[1]) if c else None


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------

_ALLCAPS = {"TGV", "CDG", "RER", "ST", "STE"}
_FIX = {
    "Tgv": "TGV", "Cdg": "CDG", "Sncf": "SNCF",
    "St": "St", "Ste": "Ste",
}


@lru_cache(maxsize=2048)
def display_name(api_name: str) -> str:
    """Human-friendly label for an API station name.

    ``"PARIS (intramuros)"`` -> ``"Paris"``,
    ``"MARSEILLE ST CHARLES"`` -> ``"Marseille St Charles"``,
    ``"AEROPORT ROISSY CDG 2 TGV"`` -> ``"Aéroport Roissy CDG 2 TGV"``.
    """
    if not api_name:
        return ""
    name = api_name
    # drop the "(intramuros)" annotation entirely
    name = re.sub(r"\s*\(intramuros\)", "", name, flags=re.IGNORECASE)
    name = name.strip(" .")
    words = []
    for w in name.split():
        up = w.upper()
        if up in _ALLCAPS:
            words.append(up if up != "ST" and up != "STE" else up.capitalize())
        elif up.isdigit():
            words.append(up)
        else:
            words.append(w.capitalize())
    pretty = " ".join(words)
    for bad, good in _FIX.items():
        pretty = re.sub(rf"\b{bad}\b", good, pretty)
    return pretty
