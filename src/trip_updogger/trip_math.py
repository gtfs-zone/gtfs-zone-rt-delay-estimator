import math

from shapely.geometry import LineString, Point


def parse_gtfs_time(time_str: str) -> int:
    """Parse GTFS time string (HH:MM:SS, may be >24:00) to seconds since midnight."""
    h, m, s = time_str.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def find_segment_and_ratio(
    lat: float,
    lon: float,
    stop_lats: list[float],
    stop_lons: list[float],
) -> tuple[int, float]:
    """Find which polyline segment is nearest to (lat, lon) and the fractional position on it.

    Longitudes are scaled by cos(lat) for an approximate equal-area projection so
    that distances are comparable across both axes.

    Returns (segment_index, t) where segment_index is the index of the behind stop
    and t in [0, 1] is the ratio along that segment toward the ahead stop.
    """
    cos_lat = math.cos(math.radians(lat))
    coords = [(lon_ * cos_lat, lat_) for lat_, lon_ in zip(stop_lats, stop_lons)]
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


def compute_delay(
    lat: float,
    lon: float,
    seconds_since_midnight: float,
    trip_stops: list[dict],
) -> tuple[int, int] | None:
    """Compute vehicle delay in seconds versus the GTFS schedule.

    trip_stops: list of dicts with keys stop_sequence (int), stop_lat (float),
    stop_lon (float), arrival_time (str | None), departure_time (str | None).
    Does not need to be pre-sorted.

    seconds_since_midnight: local time of the location fix expressed as seconds
    since local midnight (e.g. 8*3600 + 5*60 for 08:05:00 local).

    Returns (delay, stop_sequence) where delay is in seconds (positive = late,
    negative = early) and stop_sequence is the sequence number of the next stop,
    or None if there is insufficient data to compute a result.
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

    time_behind_str = stop_behind.get("departure_time") or stop_behind.get("arrival_time")
    time_ahead_str = stop_ahead.get("arrival_time") or stop_ahead.get("departure_time")
    if time_behind_str is None or time_ahead_str is None:
        return None

    scheduled = parse_gtfs_time(time_behind_str) + t * (
        parse_gtfs_time(time_ahead_str) - parse_gtfs_time(time_behind_str)
    )

    return round(seconds_since_midnight - scheduled), stop_ahead["stop_sequence"]
