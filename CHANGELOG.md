## v0.4.0 (2026-11-22)

### Feat

- **scripts**: replay TIDES packages through a prediction method
- pluggable prediction methods chosen per feed or tracker

### Fix

- **scripts**: give the replay scorer a 10 min test timeout
- **deps**: upgrade packages with published security advisories

## v0.3.1 (2026-10-17)

## v0.3.0 (2026-10-17)

### Feat

- **trip-math**: hold at the origin and never predict early

## v0.2.2 (2026-10-02)

## v0.2.1 (2026-10-01)

## v0.2.0 (2026-10-01)

### BREAKING CHANGE

- the module is now gtfs_zone_rt_delay_estimator

### Feat

- **keys**: write tracker-scoped trip_update keys
- resolve "auto" device via driver rule before alias lookup
- support MQTT username and password
- use trip aliases
- use the database for static gtfs

### Fix

- **deps**: add tzdata so zoneinfo resolves on alpine
- publish timed per-stop predictions, not a bare delay
- stop publishing the tracker credential as vehicle_id
- specify next stop
- account for feed tz

### Refactor

- rename the package to gtfs-zone-rt-delay-estimator
- turn trip-updogger into a Redis->Redis worker
- rename api to cafe-car
- use railroad-club
- rm compose

### Perf

- check trip_update ownership before querying the schedule
