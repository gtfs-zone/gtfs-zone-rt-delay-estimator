# trip-updogger

Tiny async Python worker that turns live vehicle positions into GTFS-RT Trip Updates in Redis.

Part of a larger stack; see [music-student](https://git.kcfam.us/gtfs.zone/music-student) for the full deployment.

### How it fits together

```
Traccar Client app (phone) ─┐
                            ├─> vehicle:{tracker_id}:* (Redis, DB 1)
hell-gate-bridge ───────────┘        │
   (via cafe-car /ingest)            │ watch (poll every POLL_INTERVAL)
                                trip-updogger (this worker)
                                     │ compute schedule delay
                                     └─> trip_update:{trip_id} (300s TTL)
                                              └─> cafe-car (serves GTFS-RT)
```

Positions are already in Redis: vehicle-poser writes them for the Traccar path, and cafe-car's
`/ingest/position` writes them for hell-gate-bridge. This worker sweeps those `vehicle:*` keys, and for
each new fix it loads the trip's scheduled stop times from PostgreSQL, computes the current delay
against the schedule with `compute_delay`, and writes a Trip Update to Redis with a 300-second TTL.

The `trip_id` is taken straight from the position record — vehicle-poser resolves it from the schedule
rules and hell-gate supplies it directly, so this worker never re-resolves it.

**Collision guard:** if a `trip_update:*` for the trip already carries per-stop `stop_time_updates`
(hell-gate-bridge's richer prediction), the worker leaves it untouched — it only fills the gap for
positions that have no richer trip-update.

---

## Redis contract

**Reads** `vehicle:{tracker_id}:{trip_slug}` records (written by vehicle-poser / cafe-car `/ingest`):

```json
{"tracker_id": "...", "trip_id": "...", "lat": 51.5, "lon": -0.1, "timestamp": 1234567890, "start_date": "20260724"}
```

`tracker_id` is the device's **secret** id; it is never exposed in a feed. cafe-car labels vehicles by
the tracker's `nickname`.

**Writes** `trip_update:{trip_id}` (or `trip_update:{trip_id}:{start_date}` when the position carries a
`start_date`, matching cafe-car's ingest key scheme):

```json
{"trip_id": "...", "vehicle_id": "...", "timestamp": 1234567890, "delay": 42, "stop_sequence": 5}
```

`delay` is in seconds (positive = late, negative = early). `vehicle_id` holds the tracker id for
internal reference only — cafe-car labels the vehicle in the public feed by the tracker's `nickname`.

---

## Environment variables

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://.../postgres` | PostgreSQL connection URL |
| `POLL_INTERVAL` | `5` | Seconds between sweeps of the `vehicle:*` keyspace (default `5`) |

`REDIS_URL` and `DATABASE_URL` are required — the worker exits with `KeyError` if either is missing.

---

## Running

```sh
# Install dependencies (Python 3.13, uv)
uv sync

# Run locally (requires Redis with vehicle:* keys and PostgreSQL with GTFS data)
REDIS_URL=redis://localhost:6379/1 \
  DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/postgres \
  uv run python -m trip_updogger.main

# Build and push Docker image (requires clean, pushed branch)
make push
```

---

## Testing

```sh
# Seed a position the worker will pick up (trip_id must have scheduled stop_times)
redis-cli -n 1 SET vehicle:gently-tender-oyster:TRIP1 \
  '{"tracker_id":"gently-tender-oyster","trip_id":"TRIP1","lat":51.5,"lon":-0.1,"timestamp":1784808000}'

# After one poll interval, verify the computed Trip Update
redis-cli -n 1 GET trip_update:TRIP1
```
