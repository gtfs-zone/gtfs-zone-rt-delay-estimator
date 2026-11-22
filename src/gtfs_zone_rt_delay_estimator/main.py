import asyncio
import json
import logging
import os
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from gtfs_zone_db_models.models import (
    Feed,
    GtfsStaticFeed,
    GtfsStop,
    GtfsStopTime,
    Tracker,
)
from gtfs_zone_db_models.vehicle_keys import trip_update_key
from sqlalchemy import Engine, create_engine
from sqlmodel import Session, select

from gtfs_zone_rt_delay_estimator import config
from gtfs_zone_rt_delay_estimator.cache import TTLCache
from gtfs_zone_rt_delay_estimator.predictors import Fix, PredictorInput
from gtfs_zone_rt_delay_estimator.trip_math import service_day_base

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Match rt-api's ingest TRIP_UPDATE_TTL so stale predictions expire together.
TRIP_UPDATE_TTL = 300

# Stamped on every record we write so we never overwrite a richer producer's
# trip_update (e.g. rt-pollers's per-stop prediction), only our own.
SOURCE = "rt-delay-estimator"

# How long a tracker's feed, and a trip's stop times, are reused before being
# read again, so a new tracker or a reloaded schedule is picked up.
TRACKER_TTL = 300
STOP_TIMES_TTL = 600

# How far back the per-trip fix history passed to a predictor reaches.
HISTORY_WINDOW = 600


@dataclass(frozen=True)
class TrackerInfo:
    """What a tracker id resolves to: its feed and that feed's current schedule."""

    feed_name: str
    nickname: str
    gtfs_static_feed_id: int | None
    timezone: str | None


def load_tracker(engine: Engine, tracker_id: str) -> TrackerInfo | None:
    """The tracker's feed and the static feed that feed currently serves."""
    with Session(engine) as session:
        row = session.execute(
            select(
                Feed.feed_name,
                Tracker.nickname,
                Feed.gtfs_static_feed_id,
                GtfsStaticFeed.timezone,
            )
            .join(Feed, Feed.id == Tracker.feed_id)
            .outerjoin(GtfsStaticFeed, GtfsStaticFeed.id == Feed.gtfs_static_feed_id)
            .where(Tracker.id == tracker_id)
        ).first()
    if row is None:
        return None
    return TrackerInfo(
        feed_name=row.feed_name,
        nickname=row.nickname,
        gtfs_static_feed_id=row.gtfs_static_feed_id,
        timezone=row.timezone,
    )


def load_stop_times(engine: Engine, static_feed_id: int, trip_id: str) -> list[dict]:
    """The trip's scheduled stop_times with coordinates, sorted by stop_sequence.

    Scoped to one static feed, so two feeds sharing a trip_id never mix stops.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(
                GtfsStopTime.stop_id,
                GtfsStopTime.arrival_time,
                GtfsStopTime.departure_time,
                GtfsStopTime.stop_sequence,
                GtfsStop.stop_lat,
                GtfsStop.stop_lon,
            )
            .join(
                GtfsStop,
                (GtfsStop.gtfs_static_feed_id == GtfsStopTime.gtfs_static_feed_id)
                & (GtfsStop.stop_id == GtfsStopTime.stop_id),
            )
            .where(
                GtfsStopTime.gtfs_static_feed_id == static_feed_id,
                GtfsStopTime.trip_id == trip_id,
            )
        ).all()
    stop_times = [
        {
            "trip_id": trip_id,
            "stop_id": row.stop_id,
            "arrival_time": row.arrival_time,
            "departure_time": row.departure_time,
            "stop_sequence": row.stop_sequence,
            "stop_lat": row.stop_lat,
            "stop_lon": row.stop_lon,
        }
        for row in rows
    ]
    return sorted(stop_times, key=lambda s: s["stop_sequence"])


async def _should_write(redis: aioredis.Redis, key: str) -> bool:
    """Only write when the slot is empty or already ours.

    Any trip_update from a different producer (rt-pollers's richer per-stop
    prediction) is left untouched. Keying off our own SOURCE stamp (rather than
    the mere absence of stop_time_updates) is race-safe: once a richer producer
    owns the key we defer for the record's lifetime instead of flip-flopping with
    it every poll."""
    existing = await redis.get(key)
    if existing is None:
        return True
    try:
        data = json.loads(existing)
    except (ValueError, TypeError):
        return True
    return data.get("source") == SOURCE


class Estimator:
    """Turns position records into trip updates with the configured methods.

    The tracker and stop-time loaders are plain callables so tests can stub
    them; they run in a worker thread.
    """

    def __init__(
        self,
        predictors: config.Predictors,
        tracker_loader: Callable[[str], TrackerInfo | None],
        stop_times_loader: Callable[[int, str], list[dict]],
    ) -> None:
        self.predictors = predictors
        self._tracker_loader = tracker_loader
        self._stop_times_loader = stop_times_loader
        self._trackers: TTLCache[str, TrackerInfo | None] = TTLCache(TRACKER_TTL)
        self._stop_times: TTLCache[tuple[int, str], list[dict]] = TTLCache(
            STOP_TIMES_TTL
        )
        # Earlier fixes per trip_update key, oldest first.
        self._history: dict[str, deque[Fix]] = {}

    async def _tracker(self, tracker_id: str) -> TrackerInfo | None:
        return await asyncio.to_thread(
            self._trackers.get_or_load,
            tracker_id,
            lambda: self._tracker_loader(tracker_id),
        )

    async def _stops(self, static_feed_id: int, trip_id: str) -> list[dict]:
        return await asyncio.to_thread(
            self._stop_times.get_or_load,
            (static_feed_id, trip_id),
            lambda: self._stop_times_loader(static_feed_id, trip_id),
        )

    def _push_history(self, key: str, fix: Fix) -> tuple[Fix, ...]:
        """Record `fix` and return the fixes before it inside the window."""
        fixes = self._history.setdefault(key, deque())
        while fixes and fixes[0].timestamp < fix.timestamp - HISTORY_WINDOW:
            fixes.popleft()
        earlier = tuple(f for f in fixes if f.timestamp < fix.timestamp)
        if not fixes or fixes[-1].timestamp < fix.timestamp:
            fixes.append(fix)
        return earlier

    def prune_history(self, now: int) -> None:
        """Drop trips whose latest fix is older than the window."""
        stale = [
            k
            for k, fixes in self._history.items()
            if not fixes or fixes[-1].timestamp < now - HISTORY_WINDOW
        ]
        for k in stale:
            del self._history[k]

    async def process_position(self, redis: aioredis.Redis, record: dict) -> None:
        trip_id = record.get("trip_id")
        tracker_id = record.get("tracker_id")
        lat = record.get("lat")
        lon = record.get("lon")
        timestamp = record.get("timestamp")
        if not trip_id or lat is None or lon is None or timestamp is None:
            return
        # The key is tracker-scoped, so a record without one cannot be written
        # anywhere a reader would look for it.
        if not tracker_id:
            log.warning("Position for trip_id=%s has no tracker_id; skipped", trip_id)
            return

        # Check ownership before doing any work. Both halves of the key come
        # straight off the record, so this costs one Redis GET and saves a
        # Postgres round-trip per position we were only going to discard, the
        # common case for a producer like rt-pollers, whose ~53 concurrent
        # Amtrak vehicles all share one credential and already own their
        # trip_update keys.
        key = trip_update_key(tracker_id, trip_id, record.get("start_date"))
        if not await _should_write(redis, key):
            return

        fix_epoch = int(timestamp)
        history = self._push_history(key, Fix(fix_epoch, lat, lon))

        tracker = await self._tracker(tracker_id)
        if tracker is None:
            log.warning("Unknown tracker for trip_id=%s; skipped", trip_id)
            return
        if tracker.gtfs_static_feed_id is None or tracker.timezone is None:
            log.warning("Feed %s has no loaded schedule", tracker.feed_name)
            return
        stop_times = await self._stops(tracker.gtfs_static_feed_id, trip_id)
        if not stop_times:
            return

        # GTFS schedule times are offsets from the *service day's* midnight and
        # may run past 24:00, so the fix's own calendar date is not always the
        # right base.
        base = service_day_base(fix_epoch, ZoneInfo(tracker.timezone), stop_times)

        choice = self.predictors.for_tracker(tracker.feed_name, tracker.nickname)
        prediction = choice.predictor.predict(
            PredictorInput(
                lat=lat,
                lon=lon,
                timestamp=fix_epoch,
                stops=stop_times,
                base_epoch=base,
                bearing=record.get("bearing"),
                speed=record.get("speed"),
                history=history,
            ),
            choice.params,
        )
        if prediction is None:
            return

        log.info(
            "trip_id=%s method=%s delay=%ds seq=%s stops=%d",
            trip_id,
            choice.method,
            prediction.delay,
            prediction.stop_sequence,
            len(prediction.stop_time_updates),
        )
        payload = {
            "trip_id": trip_id,
            # The tracker id under its own name, never the vehicle id. rt-api
            # publishes a trip update's `vehicle_id` verbatim as the GTFS
            # VehicleDescriptor.id, so writing the tracker id there would
            # collapse every trip we own onto one id.
            "tracker_id": tracker_id,
            "timestamp": fix_epoch,
            # Absolute times per stop, not a bare delay: a consumer cannot order
            # a trip's stops from delays alone.
            "stop_time_updates": prediction.stop_time_updates,
            # Advisory duplicates of the head of the prediction list. rt-api
            # reads stop_time_updates whenever it is non-empty, so these are only
            # for making a `redis-cli GET trip_update:...` legible.
            "delay": prediction.delay,
            "stop_sequence": prediction.stop_sequence,
            "source": SOURCE,
            "method": choice.method,
            "params_hash": choice.params_hash,
        }
        # Carry the position record's public identity across so the trip update
        # names the same vehicle its position does. Omit rather than write None:
        # an absent key is what tells rt-api to fall back to the tracker
        # nickname, which is right for single-device producers.
        for field in ("vehicle_id", "vehicle_label"):
            if record.get(field):
                payload[field] = record[field]

        await redis.setex(key, TRIP_UPDATE_TTL, json.dumps(payload))


async def run(estimator: Estimator, redis_url: str, poll_interval: float) -> None:
    redis = aioredis.from_url(redis_url)
    # Only recompute a key when its fix timestamp advances. This avoids
    # reprocessing the same position every sweep.
    seen: dict[str, int] = {}
    log.info("rt-delay-estimator watching vehicle:* every %.1fs", poll_interval)
    try:
        while True:
            present: set[str] = set()
            newest = 0
            async for key in redis.scan_iter("vehicle:*"):
                key_str = key.decode() if isinstance(key, bytes) else key
                present.add(key_str)
                raw = await redis.get(key)
                if raw is None:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                ts = record.get("timestamp")
                if ts is None:
                    continue
                newest = max(newest, int(ts))
                if seen.get(key_str) == int(ts):
                    continue
                seen[key_str] = int(ts)
                try:
                    await estimator.process_position(redis, record)
                except Exception:
                    log.exception("Failed to process %s", key_str)
            for gone in seen.keys() - present:
                del seen[gone]
            if newest:
                estimator.prune_history(newest)
            await asyncio.sleep(poll_interval)
    finally:
        await redis.aclose()


def main() -> None:
    redis_url = os.environ["REDIS_URL"]
    engine = create_engine(os.environ["DATABASE_URL"])
    # How often to sweep the live vehicle:* positions for new fixes.
    poll_interval = float(os.environ.get("POLL_INTERVAL", "5"))
    config_path = os.environ.get("PREDICTORS_CONFIG")
    try:
        predictors = config.load(config_path)
    except config.ConfigError as e:
        log.error("Invalid predictors config: %s", e)
        sys.exit(1)
    log.info(
        "Predictors from %s: default=%s feeds=%d trackers=%d",
        config_path or "(built-in default)",
        predictors.default.method,
        len(predictors.feeds),
        len(predictors.trackers),
    )
    estimator = Estimator(
        predictors,
        tracker_loader=lambda tracker_id: load_tracker(engine, tracker_id),
        stop_times_loader=lambda sfid, trip_id: load_stop_times(engine, sfid, trip_id),
    )
    asyncio.run(run(estimator, redis_url, poll_interval))


if __name__ == "__main__":
    main()
