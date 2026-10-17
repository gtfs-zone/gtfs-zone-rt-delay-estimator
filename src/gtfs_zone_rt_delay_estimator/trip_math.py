import math
from datetime import datetime, time, timedelta, tzinfo
from typing import NamedTuple

from shapely.geometry import LineString, Point

# How far outside a trip's scheduled span a fix may fall and still be counted as
# belonging to that service day. Generous enough for a badly delayed vehicle,
# tight enough that a 23:00-25:30 trip and the *next* day's run of the same trip
# never both match.
SERVICE_DAY_SLACK = 3 * 3600

# A vehicle counts as departed from its origin once it is this far (metres) from
# the origin and this much closer to the next stop. Until then it is held at the
# origin, so a layover in a terminal lot never reads as an early departure.
DEPART_DIST = 200
# The shorter distance used when speed and bearing say it is driving off toward
# the next stop.
DEPART_DIST_MOVING = 75
MOVING_SPEED = 2.0  # metres/second
HEADING_TOLERANCE = 45  # degrees either side of the origin -> next stop bearing

EARTH_RADIUS = 6_371_000  # metres


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
        (lon_ * cos_lat, lat_) for lat_, lon_ in zip(stop_lats, stop_lons, strict=True)
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


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance in metres, fine at stop-to-stop scale."""
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dx = math.radians(lon2 - lon1) * cos_lat
    dy = math.radians(lat2 - lat1)
    return math.hypot(dx, dy) * EARTH_RADIUS


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing in degrees from point 1 to point 2."""
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dx = math.radians(lon2 - lon1) * cos_lat
    dy = math.radians(lat2 - lat1)
    return math.degrees(math.atan2(dx, dy)) % 360


def has_departed(
    lat: float,
    lon: float,
    origin: dict,
    next_stop: dict,
    bearing: float | None = None,
    speed: float | None = None,
) -> bool:
    """Whether a fix on the first segment has really left the origin.

    The vehicle must be far enough from the origin and have closed that much of
    the straight-line distance to the next stop, so circling a lot or parking
    beside the stop does not count. A short first segment lowers the bar to half
    its length. A speed and bearing pointing at the next stop lower it further.
    """
    o_lat, o_lon = origin["stop_lat"], origin["stop_lon"]
    n_lat, n_lon = next_stop["stop_lat"], next_stop["stop_lon"]
    leg = _metres(o_lat, o_lon, n_lat, n_lon)
    away = _metres(lat, lon, o_lat, o_lon)
    progress = leg - _metres(lat, lon, n_lat, n_lon)

    need = min(DEPART_DIST, leg / 2)
    if bearing is not None and speed is not None and speed >= MOVING_SPEED:
        off = abs((bearing - _bearing(o_lat, o_lon, n_lat, n_lon) + 180) % 360 - 180)
        if off <= HEADING_TOLERANCE:
            need = min(DEPART_DIST_MOVING, need)
    return away >= need and progress >= need


def _finishing_loop(seconds_since_midnight: float, stops: list[dict]) -> bool:
    """Whether a trip that ends at its origin is past its scheduled midpoint.

    Near the origin such a vehicle is more likely arriving than waiting to leave.
    """
    first, last = stops[0], stops[-1]
    if (
        _metres(
            first["stop_lat"], first["stop_lon"], last["stop_lat"], last["stop_lon"]
        )
        > DEPART_DIST
    ):
        return False
    start = first.get("departure_time") or first.get("arrival_time")
    end = last.get("arrival_time") or last.get("departure_time")
    if start is None or end is None:
        return False
    midpoint = (parse_gtfs_time(start) + parse_gtfs_time(end)) / 2
    return seconds_since_midnight > midpoint


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
    bearing: float | None = None,
    speed: float | None = None,
) -> TripProgress | None:
    """Project a fix onto the trip's stop polyline and measure it against the schedule.

    A fix on the first segment that has not left the origin (`has_departed`) is
    held there: the next stop is the origin and the delay is how far past its
    scheduled departure the vehicle still sits, never negative.

    trip_stops: list of dicts with keys stop_sequence (int), stop_lat (float),
    stop_lon (float), arrival_time (str | None), departure_time (str | None).
    Does not need to be pre-sorted.

    seconds_since_midnight: the fix's offset from the service day's local midnight
    (see `service_day_base`), so it may legitimately exceed 86400.

    bearing (degrees) and speed (metres/second) are optional and only shorten the
    distance needed to count as departed.

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

    if (
        seg_idx == 0
        and not _finishing_loop(seconds_since_midnight, stops)
        and not has_departed(lat, lon, stop_behind, stop_ahead, bearing, speed)
    ):
        late = seconds_since_midnight - parse_gtfs_time(time_behind_str)
        return TripProgress(delay=max(0, round(late)), next_index=0, stops=stops)

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

    An early vehicle keeps its earliness only for its arrival at the next stop.
    It is assumed to hold there, so that stop's departure and every later stop
    use the delay floored at 0.

    Field names match rt-api's ingest/serving contract (`_fill_stop_time_update`).
    """
    held = max(0, delay)
    updates: list[dict] = []
    for i, stop in enumerate(stops[next_index:]):
        arrival = stop.get("arrival_time") or stop.get("departure_time")
        departure = stop.get("departure_time") or stop.get("arrival_time")
        arrival_delay = delay if i == 0 else held

        update: dict = {"stop_sequence": stop["stop_sequence"]}
        if stop.get("stop_id") is not None:
            update["stop_id"] = stop["stop_id"]
        if arrival:
            arrival_epoch = base_epoch + parse_gtfs_time(arrival)
            update["arrival_time"] = arrival_epoch + arrival_delay
            update["arrival_delay"] = arrival_delay
        if departure:
            update["departure_time"] = base_epoch + parse_gtfs_time(departure) + held
            update["departure_delay"] = held
        if arrival or departure:
            updates.append(update)
    return updates
