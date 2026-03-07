import asyncio
import csv
import io
import json
import logging
import os
import zipfile
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiomqtt
import httpx
import redis.asyncio as aioredis

from trip_updogger.trip_math import compute_delay

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MQTT_BROKER = os.environ["MQTT_BROKER"]
REDIS_URL = os.environ["REDIS_URL"]
GTFS_FEED_URLS_ENDPOINT = os.environ.get(
    "GTFS_FEED_URLS_ENDPOINT", "http://host.docker.internal:8000/feed_urls"
)

RECONNECT_DELAY_INITIAL = 1
RECONNECT_DELAY_MAX = 60


def parse_agency_timezone(csv_content: str) -> str | None:
    """Parse agency.txt and return the agency_timezone of the first row."""
    reader = csv.DictReader(io.StringIO(csv_content))
    for row in reader:
        tz = row.get("agency_timezone", "").strip()
        if tz:
            return tz
    return None


def parse_stops(csv_content: str) -> dict[str, dict]:
    """Parse stops.txt CSV content into a dict keyed by stop_id."""
    stops: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(csv_content))
    for row in reader:
        stop_id = row.get("stop_id", "").strip()
        if not stop_id:
            continue
        try:
            stop_lat = float(row["stop_lat"])
            stop_lon = float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        stops[stop_id] = {"stop_id": stop_id, "stop_lat": stop_lat, "stop_lon": stop_lon}
    return stops


def parse_stop_times(csv_content: str) -> dict[str, list[dict]]:
    """Parse stop_times.txt CSV content into a dict keyed by trip_id."""
    stop_times: dict[str, list[dict]] = defaultdict(list)
    reader = csv.DictReader(io.StringIO(csv_content))
    for row in reader:
        arrival_time = row.get("arrival_time", "").strip() or None
        departure_time = row.get("departure_time", "").strip() or None
        # Never create a stop_time with null departure and arrival
        if arrival_time is None and departure_time is None:
            continue
        stop_times[row["trip_id"]].append({
            "trip_id": row["trip_id"],
            "arrival_time": arrival_time,
            "departure_time": departure_time,
            "stop_id": row.get("stop_id", "").strip() or None,
            "stop_sequence": int(row["stop_sequence"]),
        })
    return dict(stop_times)


async def load_gtfs_feeds(endpoint: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Fetch feed URLs, download each zip, extract stop_times.txt, return merged stop_times by trip_id and timezone by trip_id."""
    all_stop_times: dict[str, list[dict]] = {}
    trip_timezones: dict[str, str] = {}

    async with httpx.AsyncClient() as client:
        resp = await client.get(endpoint)
        resp.raise_for_status()
        feed_urls: list[str] = resp.json()
        log.info("Fetched %d feed URLs", len(feed_urls))

        for url in feed_urls:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                log.warning("Skipping URL with unsupported scheme: %s", url)
                continue
            try:
                log.info("Downloading GTFS feed: %s", url)
                feed_resp = await client.get(url, follow_redirects=True)
                feed_resp.raise_for_status()

                with zipfile.ZipFile(io.BytesIO(feed_resp.content)) as zf:
                    namelist = zf.namelist()
                    if "stop_times.txt" not in namelist:
                        log.warning("No stop_times.txt in feed: %s", url)
                        continue
                    stop_times_content = zf.read("stop_times.txt").decode("utf-8-sig")
                    stops_content = (
                        zf.read("stops.txt").decode("utf-8-sig")
                        if "stops.txt" in namelist
                        else None
                    )
                    agency_timezone = (
                        parse_agency_timezone(zf.read("agency.txt").decode("utf-8-sig"))
                        if "agency.txt" in namelist
                        else None
                    )
                    if agency_timezone is None:
                        log.warning("No agency_timezone found in feed: %s", url)

                feed_stops = parse_stops(stops_content) if stops_content else {}
                if not feed_stops:
                    log.warning("No stops.txt (or empty) in feed: %s", url)

                feed_stop_times = parse_stop_times(stop_times_content)
                # Merge stop coordinates into each stop_time record
                for trip_stops in feed_stop_times.values():
                    for st in trip_stops:
                        stop = feed_stops.get(st["stop_id"] or "")
                        st["stop_lat"] = stop["stop_lat"] if stop else None
                        st["stop_lon"] = stop["stop_lon"] if stop else None

                log.info("Loaded %d trips from %s", len(feed_stop_times), url)
                all_stop_times.update(feed_stop_times)
                if agency_timezone:
                    for trip_id in feed_stop_times:
                        trip_timezones[trip_id] = agency_timezone
            except Exception as exc:
                log.warning("Failed to load feed %s: %s", url, exc)

    log.info("Total trips loaded: %d", len(all_stop_times))
    return all_stop_times, trip_timezones


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

        delay = compute_delay(record["lat"], record["lon"], seconds_since_midnight, trip_stops)
        if delay is None:
            log.warning("Could not compute delay for trip_id=%s", trip_id)
            continue

        log.info("trip_id=%s delay=%ds", trip_id, delay)
        await redis.set(f"trip_update:{trip_id}", json.dumps({"trip_id": trip_id, "delay": delay, "timestamp": record["timestamp"]}))


async def main() -> None:
    parsed = urlparse(MQTT_BROKER)
    host = parsed.hostname
    port = parsed.port or 1883

    redis = aioredis.from_url(REDIS_URL)

    stop_times, trip_timezones = await load_gtfs_feeds(GTFS_FEED_URLS_ENDPOINT)

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
