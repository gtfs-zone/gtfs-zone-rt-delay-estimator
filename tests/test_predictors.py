from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from gtfs_zone_rt_delay_estimator.predictors import (
    PREDICTORS,
    PredictorInput,
    params_hash,
)
from gtfs_zone_rt_delay_estimator.predictors.gps import Gps, GpsParams
from gtfs_zone_rt_delay_estimator.trip_math import (
    DEPART_DIST_MOVING,
    build_stop_time_updates,
    compute_progress,
    service_day_base,
)
from tests.test_trip_math import EIGHT, THREE_STOPS, _epoch, _north, _stops

TZ = ZoneInfo("America/New_York")

# Coordinates for THREE_STOPS so they can be projected onto.
THREE = [
    {**s, "stop_lat": 45.0 + 0.1 * i, "stop_lon": -73.0}
    for i, s in enumerate(THREE_STOPS)
]

# (lat, seconds after 08:00, bearing, speed, stops)
CASES = [
    (45.05, 5 * 60, None, None, _stops("08:00:00", "08:10:00")),
    (45.05, 7 * 60, None, None, _stops("08:00:00", "08:10:00")),
    (45.05, 4 * 60, None, None, _stops("08:00:00", "08:10:00")),
    (45.0, -5 * 60, None, None, _stops("08:00:00", "08:10:00")),
    (_north(50), 3 * 60, None, None, _stops("08:00:00", "08:10:00")),
    (_north(DEPART_DIST_MOVING + 10), 0, 0.0, 8.0, _stops("08:00:00", "08:10:00")),
    (_north(DEPART_DIST_MOVING + 10), 0, 180.0, 8.0, _stops("08:00:00", "08:10:00")),
    (45.15, 13 * 60, None, None, THREE),
    (45.15, 17 * 60, None, None, THREE),
]


def _today(lat, offset, bearing, speed, stops):
    fix = _epoch(2026, 7, 24, 8) + offset
    base = service_day_base(fix, TZ, stops)
    progress = compute_progress(lat, -73.0, fix - base, stops, bearing, speed)
    if progress is None:
        return None, fix, base
    delay, idx, sorted_stops = progress
    return (
        (
            build_stop_time_updates(sorted_stops, idx, base, delay),
            delay,
            sorted_stops[idx]["stop_sequence"],
        ),
        fix,
        base,
    )


@pytest.mark.parametrize("case", CASES)
def test_gps_defaults_match_trip_math(case):
    lat, _offset, bearing, speed, stops = case
    expected, fix, base = _today(*case)
    inp = PredictorInput(
        lat=lat,
        lon=-73.0,
        timestamp=fix,
        stops=sorted(stops, key=lambda s: s["stop_sequence"]),
        base_epoch=base,
        bearing=bearing,
        speed=speed,
    )
    got = Gps().predict(inp, GpsParams())
    assert got is not None
    assert (got.stop_time_updates, got.delay, got.stop_sequence) == expected


def test_gps_returns_none_without_enough_stops():
    inp = PredictorInput(
        lat=45.0, lon=-73.0, timestamp=EIGHT, stops=THREE[:1], base_epoch=0
    )
    assert Gps().predict(inp, GpsParams()) is None


def test_gps_depart_dist_param_changes_the_hold():
    stops = _stops("08:00:00", "08:10:00")
    inp = PredictorInput(
        lat=_north(150), lon=-73.0, timestamp=EIGHT, stops=stops, base_epoch=0
    )
    assert Gps().predict(inp, GpsParams()).stop_sequence == 1
    assert Gps().predict(inp, GpsParams(depart_dist=100)).stop_sequence == 2


def test_gps_floor_early_off_carries_earliness():
    inp = PredictorInput(
        lat=45.15, lon=-73.0, timestamp=EIGHT + 13 * 60, stops=THREE, base_epoch=0
    )
    floored = Gps().predict(inp, GpsParams())
    carried = Gps().predict(inp, GpsParams(floor_early=False))
    assert floored.stop_time_updates[0]["departure_delay"] == 0
    assert floored.delay == carried.delay < 0
    assert {u["arrival_delay"] for u in carried.stop_time_updates} == {carried.delay}


@pytest.mark.parametrize(
    "bad", [{"depart_dist": "far"}, {"nope": 1}, {"floor_early": "maybe"}]
)
def test_gps_bad_param_raises(bad):
    with pytest.raises(ValidationError):
        GpsParams(**bad)


def test_registry_has_gps():
    assert PREDICTORS["gps"] is Gps


def test_params_hash_is_stable_and_sensitive():
    assert params_hash(GpsParams()) == params_hash(GpsParams())
    assert len(params_hash(GpsParams())) == 8
    assert params_hash(GpsParams()) != params_hash(GpsParams(depart_dist=150))
