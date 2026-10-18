"""One day of the TGV Max dataset, seen from one station.

The upstream API answers questions one (origin, destination) pair at a time.
That is the wrong granularity for what this program asks: a single
Paris->Lyon search needs the pair answer for ~30 transfer stations in both
directions, the hunt needs it for every station in the country, and the
descentre search needs every terminus reachable from the origin.  Asking
pair-by-pair is 84-221 requests per click at ~1.1 s each.

The dataset is much smaller than that from the other side: *all* of Paris on
one day is 1051 rows, i.e. 11 requests of 100.  So fetch the station-day
once, index it by counterpart station and by train, and every pair question
this program can ask becomes a dictionary lookup.  Two sweeps (from the
origin, into the destination) cover a whole search; the hunt covers a
station with one.

A ``DayIndex`` is always complete: it is constructed only after every page
has been fetched, so an empty ``rows_to(...)`` means "no such service",
never "not loaded yet".
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from config import get_station_name

SIDE_FROM = "from"   # rows whose origine is the station: what leaves it
SIDE_TO = "to"       # rows whose destination is the station: what reaches it


def sort_key(hhmm: str) -> str:
    """Order stops by clock time, pushing after-midnight times to the end so a
    train running e.g. 22:50 -> 00:40 keeps its real stop order."""
    if not hhmm:
        return "z"
    return ("1" + hhmm) if hhmm < "04:00" else ("0" + hhmm)


def normalise(name: Optional[str]) -> str:
    """Station names as the API spells them, but comparable.

    The dataset is consistent about spacing and case, but aliases and
    hand-typed names are not, so every lookup goes through this.
    """
    return " ".join(str(name or "").upper().split())


class DayIndex:
    """Every tgvmax record touching *station* on *trip_date*, one side."""

    def __init__(
        self,
        rows: Iterable[dict],
        station: str,
        side: str,
        trip_date: date,
    ) -> None:
        if side not in (SIDE_FROM, SIDE_TO):
            raise ValueError(f"side must be {SIDE_FROM!r} or {SIDE_TO!r}, not {side!r}")
        self.station = get_station_name(station)
        self.side = side
        self.trip_date = trip_date
        # Offset paging over a dataset refreshed at midnight can repeat a row;
        # the key is the identity of a trip, so duplicates collapse.
        seen = set()
        unique: List[dict] = []
        for r in rows:
            if not r.get("origine") or not r.get("destination"):
                continue
            key = (r.get("train_no"), r.get("origine"), r.get("destination"),
                   r.get("heure_depart"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        self.rows: List[dict] = unique
        other_key = "destination" if side == SIDE_FROM else "origine"
        self._by_other: Dict[str, List[dict]] = {}
        self._by_train: Dict[str, List[dict]] = {}
        for r in rows:
            self._by_other.setdefault(normalise(r.get(other_key)), []).append(r)
            self._by_train.setdefault(str(r.get("train_no", "")), []).append(r)
        for bucket in self._by_other.values():
            bucket.sort(key=lambda r: (sort_key(r.get("heure_depart") or ""),
                                       sort_key(r.get("heure_arrivee") or "")))

    # -- pair lookups ---------------------------------------------------

    def rows_to(self, other: str) -> List[dict]:
        """Rows between this station and *other*, in departure order.

        Empty means the API has no such service that day - the index is
        complete, so it is not a "not fetched yet" answer.
        """
        return self._by_other.get(normalise(other), [])

    def counterparts(self) -> List[str]:
        """Every station reachable from (or serving) this one, API spelling."""
        key = "destination" if self.side == SIDE_FROM else "origine"
        return sorted({r[key] for r in self.rows if r.get(key)})

    def is_free(self, other: str) -> bool:
        return any((r.get("od_happy_card") or "").upper() == "OUI"
                   for r in self.rows_to(other))

    # -- train lookups --------------------------------------------------

    def has_train(self, train_no: str) -> bool:
        return str(train_no) in self._by_train

    def stops(self, train_no: str) -> List[Tuple[str, str]]:
        """The stops this side knows for a train, in travel order.

        From-side indexes know the train's stops *from this station onward*
        (every record origine=station gives the departing time here and the
        arriving time there); to-side indexes know them *up to the station*.
        Both are enough for "does this train call at X between A and B?",
        which is the only question asked of them.
        """
        first: Dict[str, str] = {}
        for r in self._by_train.get(str(train_no), ()):
            for station_key, time_key in (("origine", "heure_depart"),
                                          ("destination", "heure_arrivee")):
                s = r.get(station_key)
                hhmm = r.get(time_key) or ""
                if s and (s not in first or sort_key(hhmm) < sort_key(first[s])):
                    first[s] = hhmm
        return sorted(first.items(), key=lambda kv: sort_key(kv[1]))

    # -- introspection --------------------------------------------------

    @property
    def free_rows(self) -> List[dict]:
        return [r for r in self.rows if (r.get("od_happy_card") or "").upper() == "OUI"]

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return (f"<DayIndex {self.side} {self.station!r} {self.trip_date} "
                f"rows={len(self.rows)} trains={len(self._by_train)}>")
