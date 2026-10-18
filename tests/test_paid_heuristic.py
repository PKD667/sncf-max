"""A detour may not spend longer on paid legs than the fastest paid direct."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from models import Trip
from network.decomposition import CompositeTrip, TripLeg, drop_slower_paid, paid_time


def trip(train, o, d, dep, arr, free):
    return Trip.from_api_response({
        "date": "2026-10-24", "origine": o, "destination": d,
        "heure_depart": dep, "heure_arrivee": arr, "train_no": train,
        "od_happy_card": "OUI" if free else "NON", "entity": "INOUI", "axe": "X"})


def journey(*legs):
    return CompositeTrip(legs=[TripLeg(trip=t, is_max=t.is_free) for t in legs])


# paid direct: fastest is 3h
PAID_DIRECT = [trip("1", "A", "Z", "08:00", "11:00", free=False),   # 3h
               trip("2", "A", "Z", "09:00", "13:00", free=False)]   # 4h

FREE_LEG = trip("F", "A", "M", "07:00", "08:00", free=True)


def test_paid_time_sums_only_paid_legs():
    paid_leg = trip("P", "M", "Z", "08:30", "09:30", free=False)     # 1h paid
    c = journey(FREE_LEG, paid_leg)
    assert paid_time(c).total_seconds() == 3600


def test_detour_whose_paid_part_is_slower_than_paid_direct_is_dropped():
    slow = trip("S", "M", "Z", "08:30", "12:00", free=False)        # 3h30 paid
    assert drop_slower_paid([journey(FREE_LEG, slow)], PAID_DIRECT) == []


def test_detour_whose_paid_part_is_no_slower_is_kept():
    quick = trip("Q", "M", "Z", "08:30", "11:00", free=False)       # 2h30 paid
    c = journey(FREE_LEG, quick)
    assert drop_slower_paid([c], PAID_DIRECT) == [c]


def test_fully_max_detours_are_never_dropped():
    m1 = trip("M1", "A", "M", "07:00", "08:00", free=True)
    m2 = trip("M2", "M", "Z", "08:30", "15:00", free=True)          # long, but free
    c = journey(m1, m2)
    assert drop_slower_paid([c], PAID_DIRECT) == [c]


def test_no_paid_direct_means_nothing_to_compare_with():
    slow = trip("S", "M", "Z", "08:30", "12:00", free=False)
    c = journey(FREE_LEG, slow)
    assert drop_slower_paid([c], []) == [c]


def test_bar_is_the_fastest_paid_direct_not_the_first():
    # listed slowest first; the bar must still be 3h
    paid_direct = list(reversed(PAID_DIRECT))
    just_over = trip("O", "M", "Z", "08:30", "11:31", free=False)   # 3h01
    assert drop_slower_paid([journey(FREE_LEG, just_over)], paid_direct) == []
