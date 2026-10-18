"""A timetable that is stale, or has no trips for the date, is reported.

A quiet page for a source with no data is the failure this guards against:
the user would read "no trains" where the truth is "we have no timetable".
"""
from datetime import date
from pathlib import Path

from network import timetables


def tt(built, days):
    return {"built": built, "trips": [[0, "x", []]],
            "by_date": {d: [0] for d in days}}


def test_fresh_and_covering_is_clean():
    t = tt("2026-10-10", ["2026-10-10", "2026-10-20"])
    assert timetables.problems(t, today=date(2026, 10, 11)) == []


def test_stale_build_is_reported():
    t = tt("2026-09-29", ["2026-10-01", "2026-10-30"])
    probs = timetables.problems(t, today=date(2026, 10, 11))
    assert any("days ago" in p for p in probs), probs


def test_window_ending_soon_is_reported():
    t = tt("2026-10-10", ["2026-10-10", "2026-10-12"])
    probs = timetables.problems(t, today=date(2026, 10, 11))
    assert any("covers only up to" in p for p in probs), probs


def test_empty_timetable_is_reported():
    assert timetables.problems({"built": "2026-10-10"}, today=date(2026, 10, 10)) == ["no trips"]


def test_bad_build_date_is_reported_not_raised():
    t = tt("not-a-date", ["2026-10-10"])
    assert timetables.problems(t, today=date(2026, 10, 10))[0].startswith("unreadable")


def test_refreshed_copy_wins_when_newer(tmp_path, monkeypatch):
    monkeypatch.setenv(timetables.DATA_DIR_ENV, str(tmp_path))
    import json
    (tmp_path / "ter_timetable.json").write_text(json.dumps(tt("2099-01-01", ["2099-01-01"])))
    assert timetables.chosen("ter_timetable.json") == tmp_path / "ter_timetable.json"


def test_packaged_copy_kept_when_refresh_is_older(tmp_path, monkeypatch):
    monkeypatch.setenv(timetables.DATA_DIR_ENV, str(tmp_path))
    import json
    (tmp_path / "ter_timetable.json").write_text(json.dumps(tt("2000-01-01", ["2000-01-01"])))
    chosen = timetables.chosen("ter_timetable.json")
    assert chosen is not None and chosen.parent == timetables._HERE


def test_unreadable_refresh_never_beats_readable_packaged(tmp_path, monkeypatch):
    monkeypatch.setenv(timetables.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "ter_timetable.json").write_text("{ not json")
    chosen = timetables.chosen("ter_timetable.json")
    assert chosen is not None and chosen.parent == timetables._HERE


def test_write_atomic_replaces_whole_file(tmp_path):
    p = tmp_path / "x.json"
    timetables.write_atomic(p, {"built": "2026-10-10", "trips": [1]})
    timetables.write_atomic(p, {"built": "2026-10-11", "trips": [2]})
    import json
    assert json.loads(p.read_text()) == {"built": "2026-10-11", "trips": [2]}
    assert not list(tmp_path.glob("*.tmp"))


def test_builder_refuses_empty_feed_and_keeps_old_file(tmp_path):
    """Runs the real TER builder on an empty GTFS: it must exit non-zero and
    leave the previous output untouched."""
    import io, json, runpy, sys, urllib.request, zipfile
    from unittest import mock
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in ["stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
                  "calendar.txt", "calendar_dates.txt"]:
            z.writestr(n, "stop_id,trip_id,route_id,service_id\n")
    old = tmp_path / "ter_timetable.json"
    old.write_text(json.dumps({"built": "2026-10-10", "trips": [[0, "x", []]]}))
    script = str(Path(__file__).resolve().parent.parent / "script" / "build_ter.py")
    argv = ["build_ter.py", "--out", str(tmp_path)]
    fake = lambda *a, **k: io.BytesIO(buf.getvalue())
    with mock.patch.object(urllib.request, "urlopen", fake), mock.patch.object(sys, "argv", argv):
        g = runpy.run_path(script, run_name="builder")
        try:
            rc = g["main"]()
        except SystemExit as e:
            rc = e.code
    assert rc == 1
    assert json.loads(old.read_text())["built"] == "2026-10-10"
