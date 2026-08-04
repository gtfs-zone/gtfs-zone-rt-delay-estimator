from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from trip_updogger.trip_math import (
    build_stop_time_updates,
    compute_progress,
    find_segment_and_ratio,
    parse_gtfs_time,
    service_day_base,
)

# ---------------------------------------------------------------------------
# parse_gtfs_time
# ---------------------------------------------------------------------------

def test_parse_gtfs_time_normal():
    assert parse_gtfs_time("08:30:00") == 8 * 3600 + 30 * 60

def test_parse_gtfs_time_past_midnight():
    # GTFS allows times past 24:00 for trips that run after midnight
    assert parse_gtfs_time("25:05:30") == 25 * 3600 + 5 * 60 + 30

def test_parse_gtfs_time_midnight():
    assert parse_gtfs_time("00:00:00") == 0


# ---------------------------------------------------------------------------
# find_segment_and_ratio
# ---------------------------------------------------------------------------

# Three stops along a meridian (same lon, increasing lat):
#   stop 0: lat=45.0, lon=-73.0
#   stop 1: lat=45.1, lon=-73.0
#   stop 2: lat=45.2, lon=-73.0
LATS = [45.0, 45.1, 45.2]
LONS = [-73.0, -73.0, -73.0]


def test_find_segment_midpoint_first_segment():
    seg, t = find_segment_and_ratio(45.05, -73.0, LATS, LONS)
    assert seg == 0
    assert t == pytest.approx(0.5, abs=1e-6)


def test_find_segment_midpoint_second_segment():
    seg, t = find_segment_and_ratio(45.15, -73.0, LATS, LONS)
    assert seg == 1
    assert t == pytest.approx(0.5, abs=1e-6)


def test_find_segment_at_first_stop():
    seg, t = find_segment_and_ratio(45.0, -73.0, LATS, LONS)
    assert seg == 0
    assert t == pytest.approx(0.0, abs=1e-6)


def test_find_segment_at_last_stop():
    seg, t = find_segment_and_ratio(45.2, -73.0, LATS, LONS)
    assert seg == 1
    assert t == pytest.approx(1.0, abs=1e-6)


def test_find_segment_before_first_stop_clamps():
    # Point south of all stops should clamp to segment 0, t=0
    seg, t = find_segment_and_ratio(44.9, -73.0, LATS, LONS)
    assert seg == 0
    assert t == pytest.approx(0.0, abs=1e-6)


def test_find_segment_after_last_stop_clamps():
    # Point north of all stops should clamp to segment 1, t=1
    seg, t = find_segment_and_ratio(45.3, -73.0, LATS, LONS)
    assert seg == 1
    assert t == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# compute_progress
# ---------------------------------------------------------------------------

def _stops(departure_0, arrival_1):
    """Two stops along the meridian with given schedule times."""
    return [
        {
            "stop_sequence": 1,
            "stop_id": "A",
            "stop_lat": 45.0,
            "stop_lon": -73.0,
            "arrival_time": departure_0,
            "departure_time": departure_0,
        },
        {
            "stop_sequence": 2,
            "stop_id": "B",
            "stop_lat": 45.1,
            "stop_lon": -73.0,
            "arrival_time": arrival_1,
            "departure_time": arrival_1,
        },
    ]


def test_compute_progress_on_time():
    # Vehicle exactly halfway, exactly on schedule → delay 0
    stops = _stops("08:00:00", "08:10:00")
    # Halfway = 8:05:00 local = 29100 s
    progress = compute_progress(45.05, -73.0, 8 * 3600 + 5 * 60, stops)
    assert progress.delay == 0


def test_compute_progress_late():
    stops = _stops("08:00:00", "08:10:00")
    # Vehicle halfway but 2 minutes late
    progress = compute_progress(45.05, -73.0, 8 * 3600 + 7 * 60, stops)
    assert progress.delay == 2 * 60


def test_compute_progress_early():
    stops = _stops("08:00:00", "08:10:00")
    # Vehicle halfway but 1 minute early
    progress = compute_progress(45.05, -73.0, 8 * 3600 + 4 * 60, stops)
    assert progress.delay == -60


def test_compute_progress_at_first_stop():
    # At stop 0 exactly, scheduled time = departure of stop 0 = 28800
    stops = _stops("08:00:00", "08:10:00")
    progress = compute_progress(45.0, -73.0, 8 * 3600, stops)
    assert progress.delay == 0


def test_compute_progress_at_last_stop():
    # At stop 1 exactly, scheduled time = arrival of stop 1 = 29400
    stops = _stops("08:00:00", "08:10:00")
    progress = compute_progress(45.1, -73.0, 8 * 3600 + 10 * 60, stops)
    assert progress.delay == 0


def test_compute_progress_next_index_is_the_stop_ahead():
    stops = [
        {"stop_sequence": 1, "stop_id": "A", "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
        {"stop_sequence": 2, "stop_id": "B", "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
        {"stop_sequence": 3, "stop_id": "C", "stop_lat": 45.2, "stop_lon": -73.0,
         "arrival_time": "08:20:00", "departure_time": "08:20:00"},
    ]
    # Between stops 2 and 3 → heading for stop 3, which is index 2
    progress = compute_progress(45.15, -73.0, 8 * 3600 + 15 * 60, stops)
    assert progress.next_index == 2
    assert progress.stops[progress.next_index]["stop_sequence"] == 3


def test_compute_progress_returns_stops_sorted():
    stops = [
        {"stop_sequence": 2, "stop_id": "B", "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
        {"stop_sequence": 1, "stop_id": "A", "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    progress = compute_progress(45.05, -73.0, 8 * 3600 + 5 * 60, stops)
    assert [s["stop_sequence"] for s in progress.stops] == [1, 2]


def test_compute_progress_missing_coordinates():
    stops = [
        {"stop_sequence": 1, "stop_lat": None, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
    ]
    assert compute_progress(45.05, -73.0, 8 * 3600 + 5 * 60, stops) is None


def test_compute_progress_single_stop_returns_none():
    stops = [
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    assert compute_progress(45.0, -73.0, 8 * 3600, stops) is None


def test_compute_progress_missing_time_returns_none():
    stops = [
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": None, "departure_time": None},
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
    ]
    assert compute_progress(45.05, -73.0, 8 * 3600 + 5 * 60, stops) is None


def test_compute_progress_unsorted_stop_sequence():
    # Stops provided out of order, should still work
    stops = [
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    progress = compute_progress(45.05, -73.0, 8 * 3600 + 5 * 60, stops)
    assert progress.delay == 0


# ---------------------------------------------------------------------------
# service_day_base
# ---------------------------------------------------------------------------

TZ = ZoneInfo("America/New_York")


def _epoch(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


def _midnight(year, month, day):
    return int(datetime(year, month, day, 0, 0, tzinfo=TZ).timestamp())


def test_service_day_base_daytime_uses_own_date():
    stops = _stops("08:00:00", "08:10:00")
    fix = _epoch(2026, 7, 24, 8, 5)
    assert service_day_base(fix, TZ, stops) == _midnight(2026, 7, 24)


def test_service_day_base_after_midnight_uses_previous_date():
    # Trip runs 23:00 -> 25:30 (i.e. 01:30 the next calendar day)
    stops = _stops("23:00:00", "25:30:00")
    fix = _epoch(2026, 7, 25, 1, 0)
    assert service_day_base(fix, TZ, stops) == _midnight(2026, 7, 24)


def test_service_day_base_after_midnight_gives_a_sane_delay():
    stops = _stops("23:00:00", "25:30:00")
    # Halfway between the two stops = 24:15:00 on the service day = 00:15 local
    fix = _epoch(2026, 7, 25, 0, 15)
    base = service_day_base(fix, TZ, stops)
    progress = compute_progress(45.05, -73.0, fix - base, stops)
    assert progress.delay == 0


def test_service_day_base_falls_back_to_own_date_when_nothing_fits():
    stops = _stops("08:00:00", "08:10:00")
    # Middle of the night, nowhere near the trip's span on either candidate day
    fix = _epoch(2026, 7, 24, 3, 0)
    assert service_day_base(fix, TZ, stops) == _midnight(2026, 7, 24)


def test_service_day_base_untimed_stops_falls_back():
    stops = [
        {"stop_sequence": 1, "arrival_time": None, "departure_time": None},
        {"stop_sequence": 2, "arrival_time": None, "departure_time": None},
    ]
    fix = _epoch(2026, 7, 24, 8, 5)
    assert service_day_base(fix, TZ, stops) == _midnight(2026, 7, 24)


# ---------------------------------------------------------------------------
# build_stop_time_updates
# ---------------------------------------------------------------------------

THREE_STOPS = [
    {"stop_sequence": 1, "stop_id": "A", "arrival_time": "08:00:00", "departure_time": "08:01:00"},
    {"stop_sequence": 2, "stop_id": "B", "arrival_time": "08:10:00", "departure_time": "08:11:00"},
    {"stop_sequence": 3, "stop_id": "C", "arrival_time": "08:20:00", "departure_time": "08:21:00"},
]
BASE = _midnight(2026, 7, 24)


def test_build_stop_time_updates_starts_at_next_index():
    updates = build_stop_time_updates(THREE_STOPS, 1, BASE, 0)
    assert [u["stop_sequence"] for u in updates] == [2, 3]
    assert [u["stop_id"] for u in updates] == ["B", "C"]


def test_build_stop_time_updates_times_are_absolute_and_delayed():
    delay = 120
    updates = build_stop_time_updates(THREE_STOPS, 1, BASE, delay)
    assert updates[0]["arrival_time"] == BASE + parse_gtfs_time("08:10:00") + delay
    assert updates[0]["departure_time"] == BASE + parse_gtfs_time("08:11:00") + delay
    assert updates[0]["arrival_delay"] == delay
    assert updates[0]["departure_delay"] == delay


def test_build_stop_time_updates_times_increase_along_the_trip():
    updates = build_stop_time_updates(THREE_STOPS, 0, BASE, 60)
    times = [u["arrival_time"] for u in updates]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_build_stop_time_updates_delay_propagates_unchanged():
    updates = build_stop_time_updates(THREE_STOPS, 0, BASE, -45)
    assert {u["arrival_delay"] for u in updates} == {-45}


def test_build_stop_time_updates_arrival_only_stop():
    stops = [
        {"stop_sequence": 1, "stop_id": "A", "arrival_time": "08:00:00", "departure_time": None},
    ]
    updates = build_stop_time_updates(stops, 0, BASE, 30)
    assert updates[0]["arrival_time"] == BASE + parse_gtfs_time("08:00:00") + 30
    # Departure falls back to the arrival time rather than being dropped
    assert updates[0]["departure_time"] == updates[0]["arrival_time"]


def test_build_stop_time_updates_skips_untimed_stop():
    stops = [
        {"stop_sequence": 1, "stop_id": "A", "arrival_time": None, "departure_time": None},
        {"stop_sequence": 2, "stop_id": "B", "arrival_time": "08:10:00", "departure_time": "08:10:00"},
    ]
    updates = build_stop_time_updates(stops, 0, BASE, 0)
    assert [u["stop_sequence"] for u in updates] == [2]


def test_build_stop_time_updates_at_last_stop_is_single_entry():
    updates = build_stop_time_updates(THREE_STOPS, 2, BASE, 0)
    assert [u["stop_sequence"] for u in updates] == [3]
