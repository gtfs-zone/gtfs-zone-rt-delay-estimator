# trip-updogger

Tiny async Python service that bridges [OwnTracks](https://owntracks.org/) location events via MQTT to GTFS-RT Trip Updates in Redis.

Part of a larger stack; see [deploy-gtfs-rt](https://git.kcfam.us/gtfs.zone/deploy-gtfs-rt) for the full deployment.

### How it fits together

```
OwnTracks app (phone)
    └─> MQTT broker
            └─> trip-updogger
                    └─> Redis (trip_update:{trip_id} keys)
                            └─> redis-gtfs-rt-api (serves GTFS-RT feeds)
```

On startup, the service fetches GTFS feeds, parses `stop_times.txt`, and stores schedule data in memory keyed by `trip_id`. It then subscribes to `owntracks/+/+`, filters for `_type=location` events, computes the current delay against the scheduled stop times, and writes a Trip Update to Redis. If the MQTT connection drops, it reconnects with exponential backoff (1s → 60s max).

---

## Payload transformation

The OwnTracks MQTT topic encodes the driver and trip:

| OwnTracks field | Redis record field | Notes |
|---|---|---|
| topic `owntracks/{user}/{device}` | `driver`, `trip_id` | split from topic; `device` is used as `trip_id` |
| `lat`, `lon`, `tst` | `lat`, `lon`, `timestamp` | passed through |
| `cog` | `bearing` | degrees |
| `vel` | `speed` | converted km/h → m/s, 4 decimal places |

**Redis key:** `trip_update:{trip_id}` — written on each location update.

**Redis value:**

```json
{"trip_id": "...", "delay": 42, "timestamp": 1234567890}
```

`delay` is in seconds (positive = late, negative = early).

---

## Environment variables

| Variable | Example | Description |
|---|---|---|
| `MQTT_BROKER` | `tcp://host.docker.internal:1883` | MQTT broker URL (tcp scheme) |
| `REDIS_URL` | `redis://host.docker.internal:6379/1` | Redis connection URL including DB number |
| `GTFS_FEED_URLS_ENDPOINT` | `http://host.docker.internal:8000/feed_urls` | HTTP endpoint returning JSON list of GTFS zip URLs |

`MQTT_BROKER` and `REDIS_URL` are required — the service exits with `KeyError` if either is missing. `GTFS_FEED_URLS_ENDPOINT` defaults to `http://host.docker.internal:8000/feed_urls`.

---

## Running

```sh
# Install dependencies (Python 3.13, uv)
uv sync

# Install git hooks (required once per clone)
uv run pre-commit install

# Run locally (requires MQTT broker, Redis, and GTFS feed server)
MQTT_BROKER=tcp://localhost:1883 REDIS_URL=redis://localhost:6379/1 \
  uv run python -m trip_updogger.main

# Run bridge (MQTT broker, Redis, and GTFS feed server must be accessible at host.docker.internal)
docker compose up --build
```

---

## Testing

```sh
# Publish a location event (vel in km/h, cog in degrees)
mosquitto_pub -t owntracks/alice/trip123 \
  -m '{"_type":"location","lat":51.5,"lon":-0.1,"tst":1700000000,"vel":36,"cog":90}'

# Verify the Redis key (use the DB set in REDIS_URL)
redis-cli -n 1 GET trip_update:trip123
```
