"""The per-departure dedupe must not depend on the order results arrive in."""
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from models import Trip
from network.decomposition import CompositeTrip, TripLeg, _best_per_departure


def leg(train, o, d, dep, arr):
    return Trip.from_api_response({
        "date": "2026-10-24", "origine": o, "destination": d,
        "heure_depart": dep, "heure_arrivee": arr, "train_no": train,
        "od_happy_card": "OUI", "entity": "INOUI", "axe": "X"})


def journey(t1, t2):
    return CompositeTrip(legs=[TripLeg(trip=t1, is_max=True),
                               TripLeg(trip=t2, is_max=True)])


def tied_pair():
    """Two journeys with identical times, legs and score; only the train differs."""
    a = journey(leg("5260", "BORDEAUX ST JEAN", "MASSY TGV", "05:56", "08:53"),
                leg("5276", "MASSY TGV", "LILLE (intramuros)", "10:24", "12:09"))
    b = journey(leg("12560", "BORDEAUX ST JEAN", "MASSY TGV", "05:56", "08:53"),
                leg("5276", "MASSY TGV", "LILLE (intramuros)", "10:24", "12:09"))
    assert a.score == b.score
    return a, b


def key(alts):
    return [tuple((l.trip.train_number, str(l.trip.departure_time)) for l in c.legs)
            for c in alts]


def test_the_survivor_does_not_depend_on_arrival_order():
    a, b = tied_pair()
    results = {tuple(key(_best_per_departure(random.Random(s).sample([a, b], 2))))
               for s in range(20)}
    assert len(results) == 1, "a tie was broken by input order"


def test_the_survivor_is_the_smallest_train_number_on_a_tie():
    a, b = tied_pair()
    (winner,) = _best_per_departure([b, a])
    assert winner.legs[0].trip.train_number == "12560"  # "12560" < "5260" as strings


def test_distinct_departures_are_all_kept():
    a, _ = tied_pair()
    c = journey(leg("5300", "BORDEAUX ST JEAN", "MASSY TGV", "07:00", "10:00"),
                leg("5276", "MASSY TGV", "LILLE (intramuros)", "10:30", "12:09"))
    assert len(_best_per_departure([a, c])) == 2
