# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Async Python service that bridges OwnTracks MQTT location events to GTFS-RT Trip Updates in Redis. The entire service logic lives in `src/trip_updogger/main.py`. SQLModel models are in `models.py`; delay math is in `trip_math.py`.

## Architecture

**Flow:** OwnTracks device → MQTT broker → bridge service → Redis

1. `main()` connects to MQTT and Redis with exponential backoff reconnection (1s → 60s max).
2. `process_messages()` subscribes to `owntracks/+/+`, filters for `_type=location`, then calls `query_db()` on each message.
3. `query_db(user, alias)` looks up the MQTT username in the `driver` table to get a `feed_id`, resolves the device name through the `tripalias` table to a real `trip_id`, then fetches stop times joined with stops and the feed timezone from PostgreSQL.
4. `compute_delay()` in `trip_math.py` projects the vehicle position onto the stop polyline (with cos-lat scaling), interpolates the scheduled time at that point, and returns `(delay_seconds, next_stop_sequence)`.
5. Results are written to Redis as `trip_update:{alias}` (key uses the alias, not the resolved trip_id).

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `MQTT_BROKER` | `tcp://host.docker.internal:1883` | MQTT broker URL (tcp scheme, host, port) |
| `REDIS_URL` | `redis://host.docker.internal:6379/1` | Redis connection URL including DB number |
| `DATABASE_URL` | `postgresql+psycopg2://postgres:postgres@host.docker.internal:5432/postgres` | PostgreSQL connection URL |

All three are required — the service exits with `KeyError` if any is missing.

## Development

Dependencies are managed with `uv` (Python 3.13).

```sh
# Install dependencies
uv sync

# Run the service locally (requires MQTT broker, Redis, and PostgreSQL with GTFS data)
uv run python -m trip_updogger.main

# Build and push Docker image (requires clean, pushed branch)
make push
```
