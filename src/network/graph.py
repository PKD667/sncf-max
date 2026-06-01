"""Backwards-compatible facade over :mod:`network.stations`.

The TGV topology used to live in a static NETEX export (``tgv_graph.json``)
with its own mixed-case name space and a hand-written API-name mapping that
drifted badly out of sync.  That whole layer is gone: the real, directed
connection graph now comes straight from the SNCF API (see
``script/build_graph.py``) and is keyed by API station names.

This module just re-exports the registry under the names existing callers
(and tests) import, so nothing else had to change its imports.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from network.stations import (
    graph,
    neighbors,
    resolve,
    all_stations,
    coords,
    rideable,
    transfer_stations,
    display_name,
)


def graph_to_api(name: str) -> str:
    """Identity-ish: graph nodes are already API names now.

    Kept so legacy callers keep working; resolves casing/alias when possible.
    """
    return resolve(name) or name


__all__ = [
    "graph",
    "neighbors",
    "resolve",
    "graph_to_api",
    "all_stations",
    "coords",
    "rideable",
    "transfer_stations",
    "display_name",
]
