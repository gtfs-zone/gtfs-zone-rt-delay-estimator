# CLAUDE.md

## Overview

Async Python worker that turns the live `vehicle:*` positions already in Redis into GTFS-RT Trip
Updates (also in Redis). The entire worker logic lives in `src/gtfs_zone_rt_delay_estimator/main.py`; SQLModel models
come from the shared `gtfs_zone_db_models` package; the schedule/geometry math is in `trip_math.py`.

## Architecture

**Flow:** rt-traccar-receiver / rt-api `/ingest` → `vehicle:*` (Redis) → this worker → `trip_update:*` (Redis) → rt-api

1. `run()` opens Redis and loops forever, sweeping `vehicle:*` keys every `POLL_INTERVAL` seconds. An
   in-memory `{key: timestamp}` map skips positions whose fix time hasn't advanced.
2. Each position record already carries a resolved `trip_id` (rt-traccar-receiver resolves it from the
   schedule rules; rt-pollers supplies it directly), so the worker does **not** re-resolve it.
3. `load_stop_times(trip_id)` fetches the trip's scheduled stop times (joined with stops) and the feed
   timezone from PostgreSQL.
4. `service_day_base()` picks the local midnight the trip's schedule is measured from. GTFS times run
   past `24:00:00`, so a 01:00 fix on a 23:00–25:30 trip belongs to *yesterday's* service day; taking
   the fix's own calendar date there would report the vehicle a full day early.
5. `compute_progress()` in `trip_math.py` projects the vehicle position onto the stop polyline (with
   cos-lat scaling), interpolates the scheduled time at that point, and returns the delay, the index of
   the stop ahead, and the sorted stops.
6. `build_stop_time_updates()` turns that into a prediction for every stop from the one ahead to the end
   of the trip, each with an **absolute epoch** arrival/departure (`service day midnight + scheduled +
   delay`) alongside the delay. The vehicle is assumed to hold its current lateness for the rest of the
   trip. With no dwell or running-time model, constant-delay propagation is the only honest option.
7. Results are written to Redis as `trip_update:{trip_id}` (or `:{start_date}` when present) with a
   300-second TTL, the key rt-api reads. A **collision guard** (`_should_write`) skips the write
   unless the slot is empty or already carries our own `source` stamp, so a richer producer's record
   (rt-pollers's) is never clobbered.

**Why the times matter:** an update carrying only a delay is unusable to a consumer trying to work out
*where* a vehicle is. Vehicles from the Traccar path have no `current_stop_sequence` and no `stop_id`
(rt-traccar-receiver doesn't compute them), so a consumer's only remaining option is to infer the current stop
from the trip's soonest still-future prediction, which requires the prediction to have a time. Emitting
delay alone is what left those vehicles unplaceable.

The `tracker_id` in a position record is the tracker's **secret** id; it is never written into a feed.
rt-api labels vehicles by the tracker's `nickname`.

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://.../postgres` | PostgreSQL connection URL |
| `POLL_INTERVAL` | `5` | Seconds between sweeps of the `vehicle:*` keyspace (default `5`) |

`REDIS_URL` and `DATABASE_URL` are required. The worker exits with `KeyError` if either is missing.

## Development

Dependencies are managed with `uv` (Python 3.13).

```sh
# Install dependencies
uv sync

# Run the worker locally (requires Redis with vehicle:* keys and PostgreSQL with GTFS data)
uv run python -m gtfs_zone_rt_delay_estimator.main

# Build and push are CI's job: pushing to main publishes :latest and :<short-sha>.
# `make cp` copies that short sha for the gtfs-zone-infra manifest bump.
make cp
```

## Rules

- Module loggers are named `log`, never `logger`: `log = logging.getLogger(__name__)`
