"""Unit tests for DayIndex: the station-day answer every search reads from.

The rows here are hand-written and small on purpose - the point of these
tests is the bookkeeping (sides, normalisation, ordering, completeness), not
the network.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from network.dayindex import DayIndex, SIDE_FROM, SIDE_TO, sort_key

DAY = date(2026, 10, 19)


def row(o, d, dep, arr, train, free=False):
    return {
        "date": DAY.isoformat(),
        "origine": o,
        "destination": d,
        "heure_depart": dep,
        "heure_arrivee": arr,
        "train_no": train,
        "od_happy_card": "OUI" if free else "NON",
        "entity": "INOUI",
        "axe": "SUD EST",
    }


ROWS = [
    row("PARIS (intramuros)", "LYON (intramuros)", "08:00", "10:00", "6201", free=True),
    row("PARIS (intramuros)", "MARSEILLE ST CHARLES", "08:00", "11:20", "6201"),
    row("PARIS (intramuros)", "AVIGNON TGV", "08:00", "10:40", "6201", free=True),
    row("PARIS (intramuros)", "DIJON VILLE", "09:15", "11:00", "6301"),
]


def test_from_side_indexes_by_destination():
    index = DayIndex(ROWS, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert [r["destination"] for r in index.rows_to("LYON (intramuros)")] == [
        "LYON (intramuros)"]
    assert len(index.rows_to("AVIGNON TGV")) == 1
    assert index.rows_to("NICE VILLE") == []


def test_lookups_ignore_case_and_spacing():
    index = DayIndex(ROWS, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert index.rows_to("lyon (intramuros)") == index.rows_to(" LYON   (intramuros) ")
    assert len(index.rows_to("lyon (intramuros)")) == 1


def test_counterparts_are_the_stations_reached():
    index = DayIndex(ROWS, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert index.counterparts() == [
        "AVIGNON TGV", "DIJON VILLE", "LYON (intramuros)", "MARSEILLE ST CHARLES"]


def test_to_side_indexes_by_origin_and_answers_the_mirror_question():
    rows = [row("PARIS (intramuros)", "LYON (intramuros)", "08:00", "10:00", "6201"),
            row("DIJON VILLE", "LYON (intramuros)", "09:00", "10:30", "6301", free=True)]
    index = DayIndex(rows, "LYON (intramuros)", SIDE_TO, DAY)
    assert [r["origine"] for r in index.rows_to("DIJON VILLE")] == ["DIJON VILLE"]
    assert [r["origine"] for r in index.rows_to("PARIS (intramuros)")] == [
        "PARIS (intramuros)"]


def test_duplicate_rows_from_offset_paging_collapse():
    index = DayIndex(ROWS + ROWS[:2], "PARIS (intramuros)", SIDE_FROM, DAY)
    assert len(index) == len(ROWS)


def test_rows_without_both_ends_are_dropped():
    index = DayIndex(ROWS + [{"origine": "X", "destination": ""}],
                     "PARIS (intramuros)", SIDE_FROM, DAY)
    assert len(index) == len(ROWS)


def test_stops_come_back_in_travel_order():
    index = DayIndex(ROWS, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert index.stops("6201") == [
        ("PARIS (intramuros)", "08:00"),
        ("LYON (intramuros)", "10:00"),
        ("AVIGNON TGV", "10:40"),
        ("MARSEILLE ST CHARLES", "11:20"),
    ]


def test_after_midnight_stops_sort_last():
    rows = [row("PARIS (intramuros)", "NANTES", "22:50", "00:40", "5501"),
            row("PARIS (intramuros)", "LE MANS", "22:50", "23:40", "5501")]
    index = DayIndex(rows, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert [s for s, _ in index.stops("5501")] == [
        "PARIS (intramuros)", "LE MANS", "NANTES"]
    assert sort_key("00:40") > sort_key("23:40")


def test_has_train_and_free_rows():
    index = DayIndex(ROWS, "PARIS (intramuros)", SIDE_FROM, DAY)
    assert index.has_train("6201") and not index.has_train("9999")
    assert {r["destination"] for r in index.free_rows} == {
        "LYON (intramuros)", "AVIGNON TGV"}


def test_bad_side_is_a_programming_error():
    with pytest.raises(ValueError):
        DayIndex(ROWS, "PARIS (intramuros)", "sideways", DAY)


def test_empty_day_is_complete_not_unknown():
    """An empty index must mean "no service", because _fetch relies on it."""
    index = DayIndex([], "QUIMPER", SIDE_FROM, DAY)
    assert index.rows_to("PARIS (intramuros)") == []
    assert index.counterparts() == []
