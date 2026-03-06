# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Async Python service that bridges OwnTracks MQTT location events to GTFS-RT Trip Updates in Redis. On startup it fetches GTFS feeds, parses `stop_times.txt`, and stores schedule data in memory keyed by `trip_id`. The entire service logic lives in `src/trip_updogger/main.py`.

## Architecture

**Flow:** OwnTracks device → MQTT broker → bridge service → Redis

1. `main()` fetches GTFS feed URLs from `GTFS_FEED_URLS_ENDPOINT`, downloads and parses each feed, then connects to MQTT and Redis with exponential backoff reconnection (1s → 60s max).
2. `load_gtfs_feeds()` fetches a JSON list of GTFS zip URLs, downloads each, extracts `stop_times.txt`, and returns a dict of `{trip_id: [stop_time_records]}`.
3. `parse_stop_times()` parses `stop_times.txt` CSV, keeping: `trip_id`, `arrival_time`, `departure_time`, `stop_id`, `stop_sequence`. Rows where both `arrival_time` and `departure_time` are empty are dropped.
4. `process_messages()` subscribes to `owntracks/+/+`, filters for `_type=location`, and (TODO) computes a Trip Update from the vehicle's current position against its scheduled stop_times, then publishes to Redis.

## Environment Variables

| Variable | Example | Description |
|---|---|---|
| `MQTT_BROKER` | `tcp://host.docker.internal:1883` | MQTT broker URL (tcp scheme, host, port) |
| `REDIS_URL` | `redis://host.docker.internal:6379/1` | Redis connection URL including DB number |
| `GTFS_FEED_URLS_ENDPOINT` | `http://host.docker.internal:8000/feed_urls` | HTTP endpoint returning JSON list of GTFS zip URLs |

`MQTT_BROKER` and `REDIS_URL` are required — the service exits with `KeyError` if either is missing. `GTFS_FEED_URLS_ENDPOINT` defaults to `http://host.docker.internal:8000/feed_urls`.

## Development

Dependencies are managed with `uv` (Python 3.13).

```sh
# Install dependencies
uv sync

# Run the service locally (requires MQTT broker, Redis, and GTFS feed server)
uv run python -m trip_updogger.main

# Build Docker image
docker build -t trip-updogger .

# Run bridge
docker compose up
```
