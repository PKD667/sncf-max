"""TER stop names must round-trip: a name handed out by ter.destinations /
ter.origins has to resolve back to the stop it came from, or the TER leg of
a multimodal journey is silently lost."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from network import ter, fares  # noqa: E402

pytestmark = pytest.mark.skipif(not ter.has_data(), reason="no TER timetable")


def _all_uics():
    return {stop[0] for _op, _line, seq in ter._cache()["trips"] for stop in seq}


def test_every_stop_round_trips_through_its_name():
    names, n2u = ter._uic_to_name(), ter._name_to_uic()
    bad = [u for u in _all_uics() if n2u.get(names.get(u, u)) != u]
    assert bad == []


def test_max_station_names_keep_their_uic():
    n2u = ter._name_to_uic()
    for name, uic in ter._cache()["station_uic"].items():
        assert n2u[name] == uic, name


def test_every_stop_name_has_coordinates_for_fares():
    missing = [n for n in set(ter._uic_to_name().values())
               if fares._coords_any(n) is None]
    assert missing == []


def test_a_ter_only_stop_is_reachable_by_the_name_it_is_given():
    names = ter._uic_to_name()
    ter_only = [u for u in _all_uics() if u not in set(ter._cache()["station_uic"].values())]
    uic = ter_only[0]
    assert ter._uic_of(names[uic]) == uic
