"""Timetable caches (TER, coach): where they come from, and when they reload.

Two copies can exist:

  - the copy packaged with the code (``src/network/<name>.json``), and
  - a refreshed copy in ``$SNCF_DATA_DIR`` written by the daily refresh.

:func:`load` returns whichever is newer by its ``built`` date (the refreshed
copy wins a tie), and reloads when the file changes on disk, so a refresh
takes effect without restarting the site.  :func:`derived` memoises anything
computed from a file under the same rule.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

_HERE = Path(__file__).resolve().parent
DATA_DIR_ENV = "SNCF_DATA_DIR"

_LOADED: Dict[str, Tuple[tuple, dict]] = {}
_DERIVED: Dict[Tuple[str, str], Tuple[tuple, object]] = {}


def _candidates(name: str):
    yield _HERE / name
    d = os.environ.get(DATA_DIR_ENV)
    if d:
        yield Path(d) / name


def _stamp(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


_CHOSEN: Dict[str, Tuple[tuple, Optional[Path]]] = {}


def chosen(name: str) -> Optional[Path]:
    """The file that :func:`load` reads for *name*, or None if none exists.

    Only the stat of each candidate is read on a call; the JSON is opened
    again only when a candidate has changed, because callers ask per lookup.
    """
    paths = list(_candidates(name))
    stamps = tuple(_stamp(p) for p in paths)
    hit = _CHOSEN.get(name)
    if hit and hit[0] == stamps:
        return hit[1]
    best: Optional[Tuple[str, int, Path]] = None
    for path, st in zip(paths, stamps):
        if st is None:
            continue
        try:
            built = json.loads(path.read_text(encoding="utf-8")).get("built", "")
        except (OSError, ValueError):
            continue   # an unreadable copy never beats a readable one
        rank = (built, 1 if path.parent != _HERE else 0)   # refreshed wins a tie
        if best is None or rank > best[:2]:
            best = (built, rank[1], path)
    result = best[2] if best else None
    _CHOSEN[name] = (stamps, result)
    return result


def load(name: str) -> dict:
    """The timetable *name* (see module doc), or {} if no copy exists."""
    path = chosen(name)
    if path is None:
        return {}
    stamp = _stamp(path)
    hit = _LOADED.get(name)
    if hit and hit[0] == stamp:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _LOADED[name] = (stamp, data)
    return data


def derived(name: str, key: str, build: Callable[[dict], object]):
    """``build(load(name))``, recomputed only when the chosen file changes."""
    path = chosen(name)
    stamp = _stamp(path) if path else None
    hit = _DERIVED.get((name, key))
    if hit and hit[0] == stamp:
        return hit[1]
    value = build(load(name))
    _DERIVED[(name, key)] = (stamp, value)
    return value


def window(timetable: dict) -> Optional[Tuple[str, str]]:
    """First and last date the timetable has trips for, or None."""
    days = sorted(timetable.get("by_date", {}))
    return (days[0], days[-1]) if days else None


def problems(timetable: dict, today: date, max_age_days: int = 3,
             min_days_ahead: int = 7) -> list:
    """Reasons a timetable should not be trusted today (empty list = fine)."""
    out = []
    if not timetable.get("trips"):
        return ["no trips"]
    built = timetable.get("built")
    try:
        age = (today - date.fromisoformat(built)).days
    except (TypeError, ValueError):
        return [f"unreadable build date {built!r}"]
    if age > max_age_days:
        out.append(f"built {built}, {age} days ago")
    w = window(timetable)
    if w is None or date.fromisoformat(w[1]) < date.fromordinal(today.toordinal() + min_days_ahead):
        out.append(f"covers only up to {w[1] if w else 'nothing'}")
    return out


def write_atomic(path: Path, obj: dict) -> None:
    """Write *obj* as JSON so that readers see the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def stamp(name: str):
    """Identity of the file :func:`load` would read: changes when it changes.

    Pass it into an ``lru_cache``d function as an argument, so that memo
    entries from before a refresh are never returned after it.
    """
    path = chosen(name)
    return _stamp(path) if path else None
