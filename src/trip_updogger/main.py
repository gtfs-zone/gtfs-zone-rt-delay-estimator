import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiomqtt
import redis.asyncio as aioredis
from sqlalchemy import create_engine
from sqlmodel import Session, select

from trip_updogger.models import GtfsStaticFeed, GtfsStop, GtfsStopTime
from trip_updogger.trip_math import compute_delay

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MQTT_BROKER = os.environ["MQTT_BROKER"]
REDIS_URL = os.environ["REDIS_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]

RECONNECT_DELAY_INITIAL = 1
RECONNECT_DELAY_MAX = 60


def load_from_db() -> tuple[dict, dict]:
    engine = create_engine(DATABASE_URL)
    with Session(engine) as session:
        rows = session.execute(
            select(
                GtfsStopTime.trip_id,
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
        ).all()

    stop_times: dict[str, list[dict]] = defaultdict(list)
    trip_timezones: dict[str, str] = {}
    for row in rows:
        stop_times[row.trip_id].append(
            {
                "trip_id": row.trip_id,
                "stop_id": row.stop_id,
                "arrival_time": row.arrival_time,
                "departure_time": row.departure_time,
                "stop_sequence": row.stop_sequence,
                "stop_lat": row.stop_lat,
                "stop_lon": row.stop_lon,
            }
        )
        if row.timezone:
            trip_timezones[row.trip_id] = row.timezone

    log.info("Loaded %d trips from DB", len(stop_times))
    return dict(stop_times), trip_timezones


async def process_messages(
    client: aiomqtt.Client,
    redis: aioredis.Redis,
    stop_times: dict[str, list[dict]],
    trip_timezones: dict[str, str],
) -> None:
    await client.subscribe("owntracks/+/+")
    async for message in client.messages:
        try:
            payload = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            log.warning("Failed to decode JSON from topic %s", message.topic)
            continue

        if payload.get("_type") != "location":
            continue

        parts = str(message.topic).split("/")
        user = parts[1]
        device = parts[2]
        record = {
            "driver": user,
            "trip_id": device,
            "lat": payload.get("lat"),
            "lon": payload.get("lon"),
            "bearing": payload.get("cog"),
            "speed": round(payload["vel"] / 3.6, 4) if payload.get("vel") is not None else None,
            "timestamp": payload.get("tst"),
        }
        log.info(
            "Received location update user=%s trip_id=%s lat=%s lon=%s",
            user,
            device,
            record["lat"],
            record["lon"],
        )

        trip_id = record["trip_id"]
        trip_stops = stop_times.get(trip_id)
        if not trip_stops:
            log.warning("No stop_times for trip_id=%s", trip_id)
            continue

        tz_name = trip_timezones.get(trip_id)
        if tz_name is None:
            log.warning("No timezone for trip_id=%s", trip_id)
            continue
        dt = datetime.fromtimestamp(record["timestamp"], tz=ZoneInfo(tz_name))
        seconds_since_midnight = dt.hour * 3600 + dt.minute * 60 + dt.second

        result = compute_delay(record["lat"], record["lon"], seconds_since_midnight, trip_stops)
        if result is None:
            log.warning("Could not compute delay for trip_id=%s", trip_id)
            continue

        delay, stop_sequence = result
        log.info("trip_id=%s delay=%ds", trip_id, delay)
        await redis.set(f"trip_update:{trip_id}", json.dumps({
            "trip_id": trip_id,
            "vehicle_id": user,
            "timestamp": record["timestamp"],
            "delay": delay,
            "stop_sequence": stop_sequence,
        }))


async def main() -> None:
    parsed = urlparse(MQTT_BROKER)
    host = parsed.hostname
    port = parsed.port or 1883

    redis = aioredis.from_url(REDIS_URL)

    stop_times, trip_timezones = await asyncio.to_thread(load_from_db)

    delay = RECONNECT_DELAY_INITIAL

    while True:
        try:
            async with aiomqtt.Client(hostname=host, port=port) as client:
                log.info("Connected to MQTT broker %s:%s", host, port)
                delay = RECONNECT_DELAY_INITIAL
                await process_messages(client, redis, stop_times, trip_timezones)
        except aiomqtt.MqttError as exc:
            log.warning("MQTT error: %s — reconnecting in %ss", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
        except asyncio.CancelledError:
            log.info("Shutting down")
            break

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
