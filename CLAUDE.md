# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Async Python worker that turns the live `vehicle:*` positions already in Redis into GTFS-RT Trip
Updates (also in Redis). The entire worker logic lives in `src/trip_updogger/main.py`; SQLModel models
come from the shared `railroad_club` package; delay math is in `trip_math.py`.

## Architecture

**Flow:** vehicle-poser / cafe-car `/ingest` → `vehicle:*` (Redis) → this worker → `trip_update:*` (Redis) → cafe-car

1. `run()` opens Redis and loops forever, sweeping `vehicle:*` keys every `POLL_INTERVAL` seconds. An
   in-memory `{key: timestamp}` map skips positions whose fix time hasn't advanced.
2. Each position record already carries a resolved `trip_id` (vehicle-poser resolves it from the
   schedule rules; hell-gate-bridge supplies it directly), so the worker does **not** re-resolve it.
3. `load_stop_times(trip_id)` fetches the trip's scheduled stop times (joined with stops) and the feed
   timezone from PostgreSQL.
4. `compute_delay()` in `trip_math.py` projects the vehicle position onto the stop polyline (with
   cos-lat scaling), interpolates the scheduled time at that point, and returns `(delay_seconds,
   next_stop_sequence)`.
5. Results are written to Redis as `trip_update:{trip_id}` (or `:{start_date}` when present) with a
   300-second TTL — the key cafe-car reads. A **collision guard** skips the write when a richer per-stop
   `trip_update` (hell-gate-bridge's) already exists for that trip.

The `tracker_id` in a position record is the tracker's **secret** id; it is never written into a feed.
cafe-car labels vehicles by the tracker's `nickname`.

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://.../postgres` | PostgreSQL connection URL |
| `POLL_INTERVAL` | `5` | Seconds between sweeps of the `vehicle:*` keyspace (default `5`) |

`REDIS_URL` and `DATABASE_URL` are required — the worker exits with `KeyError` if either is missing.

## Development

Dependencies are managed with `uv` (Python 3.13).

```sh
# Install dependencies
uv sync

# Run the worker locally (requires Redis with vehicle:* keys and PostgreSQL with GTFS data)
uv run python -m trip_updogger.main

# Build and push Docker image (requires clean, pushed branch)
make push
```

## Rules

- Never add Co-Authored-By trailers to commit messages.
