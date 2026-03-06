import pytest
from trip_updogger.trip_math import compute_delay, find_segment_and_ratio, parse_gtfs_time


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
# compute_delay
# ---------------------------------------------------------------------------

def _stops(departure_0, arrival_1):
    """Two stops along the meridian with given schedule times."""
    return [
        {
            "stop_sequence": 1,
            "stop_lat": 45.0,
            "stop_lon": -73.0,
            "arrival_time": departure_0,
            "departure_time": departure_0,
        },
        {
            "stop_sequence": 2,
            "stop_lat": 45.1,
            "stop_lon": -73.0,
            "arrival_time": arrival_1,
            "departure_time": arrival_1,
        },
    ]


def test_compute_delay_on_time():
    # Vehicle exactly halfway, exactly on schedule → delay 0
    stops = _stops("08:00:00", "08:10:00")
    # Halfway = 8:05:00 local = 29100 s
    delay = compute_delay(45.05, -73.0, 8 * 3600 + 5 * 60, stops)
    assert delay == 0


def test_compute_delay_late():
    stops = _stops("08:00:00", "08:10:00")
    # Vehicle halfway but 2 minutes late
    delay = compute_delay(45.05, -73.0, 8 * 3600 + 7 * 60, stops)
    assert delay == 2 * 60


def test_compute_delay_early():
    stops = _stops("08:00:00", "08:10:00")
    # Vehicle halfway but 1 minute early
    delay = compute_delay(45.05, -73.0, 8 * 3600 + 4 * 60, stops)
    assert delay == -60


def test_compute_delay_at_first_stop():
    # At stop 0 exactly — scheduled time = departure of stop 0 = 28800
    stops = _stops("08:00:00", "08:10:00")
    delay = compute_delay(45.0, -73.0, 8 * 3600, stops)
    assert delay == 0


def test_compute_delay_at_last_stop():
    # At stop 1 exactly — scheduled time = arrival of stop 1 = 29400
    stops = _stops("08:00:00", "08:10:00")
    delay = compute_delay(45.1, -73.0, 8 * 3600 + 10 * 60, stops)
    assert delay == 0


def test_compute_delay_missing_coordinates():
    stops = [
        {"stop_sequence": 1, "stop_lat": None, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
    ]
    assert compute_delay(45.05, -73.0, 8 * 3600 + 5 * 60, stops) is None


def test_compute_delay_single_stop_returns_none():
    stops = [
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    assert compute_delay(45.0, -73.0, 8 * 3600, stops) is None


def test_compute_delay_missing_time_returns_none():
    stops = [
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": None, "departure_time": None},
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
    ]
    assert compute_delay(45.05, -73.0, 8 * 3600 + 5 * 60, stops) is None


def test_compute_delay_unsorted_stop_sequence():
    # Stops provided out of order — should still work
    stops = [
        {"stop_sequence": 2, "stop_lat": 45.1, "stop_lon": -73.0,
         "arrival_time": "08:10:00", "departure_time": "08:10:00"},
        {"stop_sequence": 1, "stop_lat": 45.0, "stop_lon": -73.0,
         "arrival_time": "08:00:00", "departure_time": "08:00:00"},
    ]
    delay = compute_delay(45.05, -73.0, 8 * 3600 + 5 * 60, stops)
    assert delay == 0
