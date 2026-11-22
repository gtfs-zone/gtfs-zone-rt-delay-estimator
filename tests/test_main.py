import asyncio
import json

from gtfs_zone_rt_delay_estimator import config
from gtfs_zone_rt_delay_estimator.main import (
    HISTORY_WINDOW,
    SOURCE,
    Estimator,
    TrackerInfo,
)
from gtfs_zone_rt_delay_estimator.predictors import params_hash
from gtfs_zone_rt_delay_estimator.predictors.gps import GpsParams
from tests.test_trip_math import THREE_STOPS, _epoch

TRACKER = TrackerInfo(
    feed_name="columbia-county",
    nickname="bus-1",
    gtfs_static_feed_id=7,
    timezone="America/New_York",
)
STOPS = [
    {**s, "trip_id": "T1", "stop_lat": 45.0 + 0.1 * i, "stop_lon": -73.0}
    for i, s in enumerate(THREE_STOPS)
]


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, _ttl, value):
        self.data[key] = value


def _record(**over):
    return {
        "tracker_id": "abc123",
        "trip_id": "T1",
        "lat": 45.15,
        "lon": -73.0,
        "timestamp": _epoch(2026, 7, 24, 8, 17),
        "start_date": "20260724",
        **over,
    }


def _estimator(predictors=None, tracker=TRACKER):
    calls = {"tracker": [], "stops": []}

    def load_tracker(tracker_id):
        calls["tracker"].append(tracker_id)
        return tracker

    def load_stops(sfid, trip_id):
        calls["stops"].append((sfid, trip_id))
        return STOPS

    est = Estimator(predictors or config.load(None), load_tracker, load_stops)
    return est, calls


def _run(est, redis, record):
    asyncio.run(est.process_position(redis, record))


def test_writes_a_trip_update_with_method_and_hash():
    est, calls = _estimator()
    redis = FakeRedis()
    _run(est, redis, _record())
    ((key, raw),) = redis.data.items()
    assert key == "trip_update:abc123:T1:20260724"
    record = json.loads(raw)
    assert record["source"] == SOURCE
    assert record["method"] == "gps"
    assert record["params_hash"] == params_hash(GpsParams())
    assert record["stop_sequence"] == 3
    assert record["delay"] == 90
    assert [u["stop_sequence"] for u in record["stop_time_updates"]] == [3]
    assert calls["stops"] == [(7, "T1")]


def test_uses_the_feeds_configured_params(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "feeds:\n  columbia-county: {method: gps, params: {floor_early: false}}\n"
    )
    est, _ = _estimator(config.load(str(path)))
    redis = FakeRedis()
    _run(est, redis, _record(lat=45.05, timestamp=_epoch(2026, 7, 24, 8, 3)))
    record = json.loads(next(iter(redis.data.values())))
    assert record["params_hash"] == params_hash(GpsParams(floor_early=False))
    assert record["delay"] < 0
    assert {u["arrival_delay"] for u in record["stop_time_updates"]} == {
        record["delay"]
    }


def test_leaves_another_producers_record_alone():
    est, calls = _estimator()
    redis = FakeRedis()
    theirs = json.dumps({"source": "rt-pollers"})
    redis.data["trip_update:abc123:T1:20260724"] = theirs
    _run(est, redis, _record())
    assert redis.data["trip_update:abc123:T1:20260724"] == theirs
    assert calls["tracker"] == []


def test_unknown_tracker_writes_nothing():
    est, calls = _estimator(tracker=None)
    redis = FakeRedis()
    _run(est, redis, _record())
    _run(est, redis, _record(timestamp=_epoch(2026, 7, 24, 8, 18)))
    assert redis.data == {}
    # The miss is cached.
    assert calls["tracker"] == ["abc123"]


def test_feed_without_a_schedule_writes_nothing():
    est, calls = _estimator(
        tracker=TrackerInfo("x", "y", gtfs_static_feed_id=None, timezone=None)
    )
    redis = FakeRedis()
    _run(est, redis, _record())
    assert redis.data == {}
    assert calls["stops"] == []


def test_lookups_are_cached():
    est, calls = _estimator()
    redis = FakeRedis()
    for minute in (15, 16, 17):
        _run(est, redis, _record(timestamp=_epoch(2026, 7, 24, 8, minute)))
    assert calls["tracker"] == ["abc123"]
    assert calls["stops"] == [(7, "T1")]


def test_history_holds_earlier_fixes_inside_the_window():
    seen = []

    class Spy:
        name = "spy"

        def predict(self, inp, _params):
            seen.append(inp.history)

    est, _ = _estimator()
    est.predictors = config.Predictors(
        default=config.Resolved(Spy(), GpsParams(), "x"), feeds={}, trackers={}
    )
    redis = FakeRedis()
    t0 = _epoch(2026, 7, 24, 8, 0)
    for dt in (0, 60, HISTORY_WINDOW + 30):
        _run(est, redis, _record(timestamp=t0 + dt))
    assert [len(h) for h in seen] == [0, 1, 1]
    assert seen[2][0].timestamp == t0 + 60

    est.prune_history(t0 + 3 * HISTORY_WINDOW)
    assert est._history == {}
