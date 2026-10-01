import math
from datetime import datetime, time, timedelta, tzinfo
from typing import NamedTuple

from shapely.geometry import LineString, Point

# How far outside a trip's scheduled span a fix may fall and still be counted as
# belonging to that service day. Generous enough for a badly delayed vehicle,
# tight enough that a 23:00-25:30 trip and the *next* day's run of the same trip
# never both match.
SERVICE_DAY_SLACK = 3 * 3600


def parse_gtfs_time(time_str: str) -> int:
    """Parse GTFS time string (HH:MM:SS, may be >24:00) to seconds since midnight."""
    h, m, s = time_str.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _scheduled(stop: dict) -> str | None:
    """The stop's schedule anchor, preferring arrival."""
    return stop.get("arrival_time") or stop.get("departure_time")


def service_day_base(fix_epoch: int, tz: tzinfo, trip_stops: list[dict]) -> int:
    """Epoch of local midnight for the service day this fix belongs to.

    GTFS times run past 24:00:00 for trips that cross midnight, so the fix's own
    calendar date is the wrong base in the small hours: a 01:00 fix on a trip
    scheduled 23:00-25:30 belongs to *yesterday's* service day, and measuring it
    against today's midnight puts the vehicle a full day early.

    Try today's midnight, then yesterday's, and take the first whose offset lands
    inside the trip's scheduled span (plus slack). Falls back to today's midnight
    when neither fits (better a base than no prediction at all).
    """
    local = datetime.fromtimestamp(fix_epoch, tz=tz)
    scheduled = [parse_gtfs_time(t) for t in (_scheduled(s) for s in trip_stops) if t]

    def midnight(days_back: int) -> int:
        date = local.date() - timedelta(days=days_back)
        return int(datetime.combine(date, time.min, tz).timestamp())

    candidates = [midnight(0), midnight(1)]
    if scheduled:
        span_start = min(scheduled) - SERVICE_DAY_SLACK
        span_end = max(scheduled) + SERVICE_DAY_SLACK
        for base in candidates:
            if span_start <= fix_epoch - base <= span_end:
                return base
    return candidates[0]


def find_segment_and_ratio(
    lat: float,
    lon: float,
    stop_lats: list[float],
    stop_lons: list[float],
) -> tuple[int, float]:
    """Find the nearest polyline segment to (lat, lon) and the fraction along it.

    Longitudes are scaled by cos(lat) for an approximate equal-area projection so
    that distances are comparable across both axes.

    Returns (segment_index, t) where segment_index is the index of the behind stop
    and t in [0, 1] is the ratio along that segment toward the ahead stop.
    """
    cos_lat = math.cos(math.radians(lat))
    coords = [
        (lon_ * cos_lat, lat_)
        for lat_, lon_ in zip(stop_lats, stop_lons, strict=True)
    ]
    line = LineString(coords)
    point = Point(lon * cos_lat, lat)

    proj_dist = line.project(point)

    cumulative = 0.0
    for i in range(len(coords) - 1):
        seg_len = math.hypot(
            coords[i + 1][0] - coords[i][0],
            coords[i + 1][1] - coords[i][1],
        )
        next_cumulative = cumulative + seg_len
        if next_cumulative >= proj_dist or i == len(coords) - 2:
            t = (proj_dist - cumulative) / seg_len if seg_len > 0 else 0.0
            return i, max(0.0, min(1.0, t))
        cumulative = next_cumulative

    return len(coords) - 2, 1.0


class TripProgress(NamedTuple):
    """Where a fix puts the vehicle on its trip.

    delay: seconds versus schedule (positive = late, negative = early).
    next_index: index into `stops` of the stop the vehicle is heading for.
    stops: the trip's stops, sorted by stop_sequence.
    """

    delay: int
    next_index: int
    stops: list[dict]


def compute_progress(
    lat: float,
    lon: float,
    seconds_since_midnight: float,
    trip_stops: list[dict],
) -> TripProgress | None:
    """Project a fix onto the trip's stop polyline and measure it against the schedule.

    trip_stops: list of dicts with keys stop_sequence (int), stop_lat (float),
    stop_lon (float), arrival_time (str | None), departure_time (str | None).
    Does not need to be pre-sorted.

    seconds_since_midnight: the fix's offset from the service day's local midnight
    (see `service_day_base`), so it may legitimately exceed 86400.

    Returns None if there is insufficient data to compute a result.
    """
    stops = sorted(trip_stops, key=lambda s: s["stop_sequence"])
    if len(stops) < 2:
        return None

    stop_lats = [s.get("stop_lat") for s in stops]
    stop_lons = [s.get("stop_lon") for s in stops]
    if any(x is None for x in stop_lats + stop_lons):
        return None

    seg_idx, t = find_segment_and_ratio(lat, lon, stop_lats, stop_lons)

    stop_behind = stops[seg_idx]
    stop_ahead = stops[seg_idx + 1]

    time_behind_str = stop_behind.get("departure_time") or stop_behind.get(
        "arrival_time"
    )
    time_ahead_str = stop_ahead.get("arrival_time") or stop_ahead.get("departure_time")
    if time_behind_str is None or time_ahead_str is None:
        return None

    scheduled = parse_gtfs_time(time_behind_str) + t * (
        parse_gtfs_time(time_ahead_str) - parse_gtfs_time(time_behind_str)
    )

    return TripProgress(
        delay=round(seconds_since_midnight - scheduled),
        next_index=seg_idx + 1,
        stops=stops,
    )


def build_stop_time_updates(
    stops: list[dict],
    next_index: int,
    base_epoch: int,
    delay: int,
) -> list[dict]:
    """Predictions for every stop from `next_index` onward.

    Stops already passed get no entry. GTFS-RT consumers read a StopTimeUpdate as
    a prediction, and there is nothing left to predict about a stop behind us.

    The vehicle is assumed to hold its current lateness for the rest of the trip:
    every downstream stop is scheduled-plus-`delay`. Without dwell or running-time
    estimates that constant-delay model is the only honest one available, and it
    is what makes the prediction *timed*. A delay alone gives a consumer no way
    to order the trip's stops, which is exactly why these updates were unusable
    before.

    Field names match rt-api's ingest/serving contract (`_fill_stop_time_update`).
    """
    updates: list[dict] = []
    for stop in stops[next_index:]:
        arrival = stop.get("arrival_time") or stop.get("departure_time")
        departure = stop.get("departure_time") or stop.get("arrival_time")

        update: dict = {"stop_sequence": stop["stop_sequence"]}
        if stop.get("stop_id") is not None:
            update["stop_id"] = stop["stop_id"]
        if arrival:
            update["arrival_time"] = base_epoch + parse_gtfs_time(arrival) + delay
            update["arrival_delay"] = delay
        if departure:
            update["departure_time"] = base_epoch + parse_gtfs_time(departure) + delay
            update["departure_delay"] = delay
        if arrival or departure:
            updates.append(update)
    return updates
