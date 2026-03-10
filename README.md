# trip-updogger

Tiny async Python service that bridges [OwnTracks](https://owntracks.org/) location events via MQTT to GTFS-RT Trip Updates in Redis.

Part of a larger stack; see [deploy-gtfs-rt](https://git.kcfam.us/gtfs.zone/deploy-gtfs-rt) for the full deployment.

### How it fits together

```
OwnTracks app (phone)
    └─> MQTT broker
            └─> trip-updogger
                    └─> Redis (trip_update:{trip_id} keys)
                            └─> cafe-car (serves GTFS-RT feeds)
```

On each location event, the service queries PostgreSQL for the vehicle's stop times (resolving the MQTT device name through a `tripalias` table), computes the current delay against the schedule, and writes a Trip Update to Redis. If the MQTT connection drops, it reconnects with exponential backoff (1s → 60s max).

---

## Payload transformation

Each OwnTracks location event is decoded as follows:

| OwnTracks field | Meaning | Notes |
|---|---|---|
| topic `owntracks/{user}/{device}` | `driver`, `trip_id` | `user` maps to a `driver` row (for feed lookup); `device` is used as the alias to resolve to a real `trip_id` |
| `lat`, `lon` | vehicle position | used to compute delay against scheduled stop times |
| `tst` | timestamp | seconds since epoch; used to determine time-of-day for schedule lookup |
| `cog` | bearing | degrees; not written to Redis |
| `vel` | speed | km/h; not written to Redis |

**Redis key:** `trip_update:{device}` — written on each location update (key uses the alias/device name).

**Redis value:**

```json
{"trip_id": "...", "vehicle_id": "...", "timestamp": 1234567890, "delay": 42, "stop_sequence": 5}
```

`delay` is in seconds (positive = late, negative = early).

---

## Environment variables

| Variable | Example | Description |
|---|---|---|
| `MQTT_BROKER` | `tcp://host.docker.internal:1883` | MQTT broker URL (tcp scheme) |
| `REDIS_URL` | `redis://host.docker.internal:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://postgres:postgres@host.docker.internal:5432/postgres` | PostgreSQL connection URL |

All three are required — the service exits with `KeyError` if any is missing.

---

## Running

```sh
# Install dependencies (Python 3.13, uv)
uv sync

# Run locally (requires MQTT broker, Redis, and PostgreSQL with GTFS data)
MQTT_BROKER=tcp://localhost:1883 REDIS_URL=redis://localhost:6379/1 \
  DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/postgres \
  uv run python -m trip_updogger.main

# Build and push Docker image (requires clean, pushed branch)
make push
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
