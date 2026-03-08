# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Async Python service that bridges OwnTracks MQTT location events to GTFS-RT Trip Updates in Redis. On startup it reads GTFS schedule data from PostgreSQL and stores it in memory keyed by `trip_id`. The entire service logic lives in `src/trip_updogger/main.py`.

## Architecture

**Flow:** OwnTracks device → MQTT broker → bridge service → Redis

1. `main()` reads GTFS data from the PostgreSQL database via `load_from_db()`, then connects to MQTT and Redis with exponential backoff reconnection (1s → 60s max).
2. `load_from_db()` queries `gtfs_stop_time` joined with `gtfs_stop` and `gtfs_static_feed`, returning a dict of `{trip_id: [stop_time_records]}` (each record includes `stop_lat`/`stop_lon`) and a `{trip_id: timezone}` dict.
3. `process_messages()` subscribes to `owntracks/+/+`, filters for `_type=location`, computes a Trip Update from the vehicle's current position against its scheduled stop_times, then publishes to Redis.

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

# Build Docker image
docker build -t trip-updogger .

# Run bridge
docker compose up
```
