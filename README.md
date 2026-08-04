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
                                     │ project onto the schedule, predict every stop ahead
                                     └─> trip_update:{trip_id} (300s TTL)
                                              └─> cafe-car (serves GTFS-RT)
```

Positions are already in Redis: vehicle-poser writes them for the Traccar path, and cafe-car's
`/ingest/position` writes them for hell-gate-bridge. This worker sweeps those `vehicle:*` keys, and for
each new fix it loads the trip's scheduled stop times from PostgreSQL, projects the fix onto the stop
polyline to measure the vehicle's delay against the schedule, and writes a Trip Update to Redis with a
300-second TTL.

The prediction covers **every stop from the one ahead to the end of the trip**, each with an absolute
epoch arrival/departure time as well as a delay. The vehicle is assumed to hold its current lateness for
the rest of the trip; without dwell or running-time estimates that constant-delay model is the only
honest one available. The absolute times are the point: a consumer cannot order a trip's stops from
delays alone, so an untimed update says nothing about where the vehicle is, and vehicles from the
Traccar path carry no `current_stop_sequence` or `stop_id` for it to fall back on.

Schedule times are resolved against the **service day**, not the fix's calendar date. GTFS times run
past `24:00:00`, so a 01:00 fix on a trip scheduled 23:00–25:30 belongs to the previous day's run.

The `trip_id` is taken straight from the position record. Vehicle-poser resolves it from the schedule
rules and hell-gate supplies it directly, so this worker never re-resolves it.

**Collision guard:** the worker writes only when the `trip_update:*` slot is empty or already carries
its own `source` stamp. A record from any other producer (hell-gate-bridge's richer per-stop prediction,
built from real observed ETAs rather than propagated schedule delay) is left untouched. Keying off the
`source` stamp rather than the shape of the record means that once a richer producer owns a key, this
worker defers for the record's lifetime instead of flip-flopping with it every poll.

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
{
  "trip_id": "...",
  "tracker_id": "...",
  "timestamp": 1234567890,
  "stop_time_updates": [
    {"stop_sequence": 5, "stop_id": "...", "arrival_time": 1234568000, "arrival_delay": 42,
     "departure_time": 1234568060, "departure_delay": 42}
  ],
  "delay": 42,
  "stop_sequence": 5,
  "source": "trip-updogger"
}
```

`stop_time_updates` is the payload, one entry per stop from the one ahead to the end of the trip, with
absolute epoch times. Its field names match cafe-car's ingest contract, the same one hell-gate-bridge
writes. Top-level `delay` and `stop_sequence` duplicate the head of that list; they are advisory, kept
so a `redis-cli GET` stays readable, and cafe-car reads the list whenever it is non-empty.

`delay` is in seconds (positive = late, negative = early). `tracker_id` is the device's **secret**
credential, held for internal reference only. It is never published. cafe-car labels the vehicle in the
public feed by the producer's `vehicle_id` if the position record carried one, else the tracker's
`nickname`.

---

## Environment variables

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://.../postgres` | PostgreSQL connection URL |
| `POLL_INTERVAL` | `5` | Seconds between sweeps of the `vehicle:*` keyspace (default `5`) |

`REDIS_URL` and `DATABASE_URL` are required. The worker exits with `KeyError` if either is missing.

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

The schedule math (projection, service-day resolution, prediction building) is pure and unit-tested:

```sh
uv run pytest
```
