import asyncio
import json
import logging
import os
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from railroad_club.models import (
    GtfsStaticFeed,
    GtfsStop,
    GtfsStopTime,
)
from sqlalchemy import create_engine
from sqlmodel import Session, select

from trip_updogger.trip_math import (
    build_stop_time_updates,
    compute_progress,
    service_day_base,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = os.environ["REDIS_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
# How often to sweep the live vehicle:* positions for new fixes.
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))

# Match cafe-car's ingest TRIP_UPDATE_TTL so stale predictions expire together.
TRIP_UPDATE_TTL = 300

# Stamped on every record we write so we never overwrite a richer producer's
# trip_update (e.g. hell-gate-bridge's per-stop prediction), only our own.
SOURCE = "trip-updogger"

_engine = create_engine(DATABASE_URL)


def load_stop_times(trip_id: str) -> tuple[list[dict], str | None]:
    """Load the scheduled stop_times (with coords) and feed timezone for a trip.

    Unlike the old OwnTracks/HTTP shim, the position record already carries the
    resolved ``trip_id`` (vehicle-poser resolves it from the schedule; hell-gate
    supplies it directly), so there is nothing to resolve here. We only fetch the
    schedule needed to project the fix onto the route and compute the delay.
    """
    with Session(_engine) as session:
        rows = session.execute(
            select(
                GtfsStopTime.stop_id,
                GtfsStopTime.arrival_time,
                GtfsStopTime.departure_time,
                GtfsStopTime.stop_sequence,
                GtfsStop.stop_lat,
                GtfsStop.stop_lon,
                GtfsStaticFeed.timezone,
            )
            .join(
                GtfsStop,
                (GtfsStop.gtfs_static_feed_id == GtfsStopTime.gtfs_static_feed_id)
                & (GtfsStop.stop_id == GtfsStopTime.stop_id),
            )
            .join(GtfsStaticFeed, GtfsStaticFeed.id == GtfsStopTime.gtfs_static_feed_id)
            .where(GtfsStopTime.trip_id == trip_id)
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
    timezone = rows[0].timezone if rows else None
    return stop_times, timezone


def _trip_update_key(trip_id: str, start_date: str | None) -> str:
    """Match cafe-car's ingest key scheme so gtfs_rt.py finds our prediction."""
    if start_date:
        return f"trip_update:{trip_id}:{start_date}"
    return f"trip_update:{trip_id}"


async def _should_write(redis: aioredis.Redis, key: str) -> bool:
    """Only write when the slot is empty or already ours.

    Any trip_update from a different producer (hell-gate-bridge's richer per-stop
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


async def process_position(redis: aioredis.Redis, record: dict) -> None:
    trip_id = record.get("trip_id")
    lat = record.get("lat")
    lon = record.get("lon")
    timestamp = record.get("timestamp")
    if not trip_id or lat is None or lon is None or timestamp is None:
        return

    # Check ownership before doing any work. Both halves of the key come
    # straight off the record, so this costs one Redis GET and saves a Postgres
    # round-trip per position we were only going to discard, the common case
    # for a producer like hell-gate-bridge, whose ~53 concurrent Amtrak vehicles
    # all share one credential and already own their trip_update keys.
    key = _trip_update_key(trip_id, record.get("start_date"))
    if not await _should_write(redis, key):
        return

    stop_times, tz_name = await asyncio.to_thread(load_stop_times, trip_id)
    if not stop_times:
        return
    if tz_name is None:
        log.warning("No timezone for trip_id=%s", trip_id)
        return

    # GTFS schedule times are offsets from the *service day's* midnight and may
    # run past 24:00, so the fix's own calendar date is not always the right base.
    fix_epoch = int(timestamp)
    base = service_day_base(fix_epoch, ZoneInfo(tz_name), stop_times)

    progress = compute_progress(lat, lon, fix_epoch - base, stop_times)
    if progress is None:
        return
    delay, next_index, stops = progress
    stop_sequence = stops[next_index]["stop_sequence"]

    # A per-stop prediction list with absolute times, not a bare delay: a consumer
    # cannot order a trip's stops from delays alone, so an untimed update tells it
    # nothing about where the vehicle is.
    stop_time_updates = build_stop_time_updates(stops, next_index, base, delay)

    log.info(
        "trip_id=%s delay=%ds seq=%s stops=%d",
        trip_id,
        delay,
        stop_sequence,
        len(stop_time_updates),
    )
    payload = {
        "trip_id": trip_id,
        # The secret credential, under its own name, never the vehicle id.
        # cafe-car publishes a trip update's `vehicle_id` verbatim as the GTFS
        # VehicleDescriptor.id, so writing the tracker id there would both leak
        # the credential and collapse every trip we own onto one id.
        "tracker_id": record.get("tracker_id"),
        "timestamp": fix_epoch,
        "stop_time_updates": stop_time_updates,
        # Advisory duplicates of the head of the prediction list. cafe-car reads
        # stop_time_updates whenever it is non-empty, so these are only for making
        # a `redis-cli GET trip_update:...` legible, and for the back-compat path
        # should an old flat record still be live under its TTL.
        "delay": delay,
        "stop_sequence": stop_sequence,
        "source": SOURCE,
    }
    # Carry the position record's public identity across so the trip update
    # names the same vehicle its position does. Omit rather than write None: an
    # absent key is what tells cafe-car to fall back to the tracker nickname,
    # which is right for single-device producers (vehicle-poser sets neither).
    for field in ("vehicle_id", "vehicle_label"):
        if record.get(field):
            payload[field] = record[field]

    await redis.setex(key, TRIP_UPDATE_TTL, json.dumps(payload))


async def run() -> None:
    redis = aioredis.from_url(REDIS_URL)
    # Only recompute a key when its fix timestamp advances. This avoids reprocessing
    # the same position every sweep.
    seen: dict[str, int] = {}
    log.info("trip-updogger watching vehicle:* every %.1fs", POLL_INTERVAL)
    try:
        while True:
            async for key in redis.scan_iter("vehicle:*"):
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
                key_str = key.decode() if isinstance(key, bytes) else key
                if seen.get(key_str) == int(ts):
                    continue
                seen[key_str] = int(ts)
                try:
                    await process_position(redis, record)
                except Exception:
                    log.exception("Failed to process %s", key_str)
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        await redis.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
