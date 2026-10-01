# AGENTS.md

Async Python worker that turns the `vehicle:*` positions in Redis into GTFS-RT
Trip Updates (`trip_update:*`, also in Redis) for rt-api. A `v*` tag
publishes the image; gtfs-zone-infra pins that tag.

## Architecture

The loop is `run()` in `src/gtfs_zone_rt_delay_estimator/main.py`; the
projection and interpolation math is in `trip_math.py`. The flow, the Redis
contract and the env vars are in [README.md](README.md).

- Position records already carry a resolved `trip_id`. Do not re-resolve it.
- A trip's service day comes from `service_day_base()`, never the fix's own
  calendar date: GTFS times run past `24:00:00`.
- Every prediction carries an **absolute epoch** arrival/departure alongside the
  delay. Traccar vehicles have no `stop_id`, so consumers place them from the
  soonest future prediction time; a delay alone leaves them unplaceable.
- Delay is propagated as a constant for the rest of the trip.
- `_should_write` only overwrites an empty slot or one carrying our own
  `source` stamp, so a richer producer (rt-pollers) is never clobbered.
- `tracker_id` is the tracker's **secret** id; never write it into a feed.

## Conventions

- **Commits**: Conventional Commits, enforced by the `commit-msg` hook. Setup,
  release and `gtfs-zone-db-models` changes are in [CONTRIBUTING.md](CONTRIBUTING.md).
- **Logging**: module loggers are named `log`, never `logger`.
- **Plans**: write plans to `CURRENT_PLAN.md` at the repo root as a
  checklist (`- [ ]`), ticked off as work lands. It is neither tracked nor
  gitignored: never stage or commit it.
