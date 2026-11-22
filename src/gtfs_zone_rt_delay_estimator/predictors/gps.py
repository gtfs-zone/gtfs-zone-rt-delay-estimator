"""Schedule plus the delay measured from the latest GPS fix, held constant."""

from typing import ClassVar

from gtfs_zone_rt_delay_estimator.predictors.base import (
    Prediction,
    PredictorInput,
    PredictorParams,
)
from gtfs_zone_rt_delay_estimator.trip_math import (
    DEPART_DIST,
    DEPART_DIST_MOVING,
    HEADING_TOLERANCE,
    MOVING_SPEED,
    build_stop_time_updates,
    compute_progress,
)


class GpsParams(PredictorParams):
    # Metres from the origin (and that much closer to the next stop) to count
    # as departed.
    depart_dist: float = DEPART_DIST
    # The shorter distance used when speed and bearing point at the next stop.
    depart_dist_moving: float = DEPART_DIST_MOVING
    # Metres/second at or above which a fix counts as moving.
    moving_speed: float = MOVING_SPEED
    # Degrees either side of the origin -> next stop bearing.
    heading_tolerance: float = HEADING_TOLERANCE
    # Floor the delay at 0 after the next arrival, so early vehicles hold.
    floor_early: bool = True


class Gps:
    name: ClassVar[str] = "gps"
    Params: ClassVar[type[GpsParams]] = GpsParams

    def predict(self, inp: PredictorInput, params: GpsParams) -> Prediction | None:
        progress = compute_progress(
            inp.lat,
            inp.lon,
            inp.timestamp - inp.base_epoch,
            inp.stops,
            inp.bearing,
            inp.speed,
            depart_dist=params.depart_dist,
            depart_dist_moving=params.depart_dist_moving,
            moving_speed=params.moving_speed,
            heading_tolerance=params.heading_tolerance,
        )
        if progress is None:
            return None
        delay, next_index, stops = progress
        updates = build_stop_time_updates(
            stops, next_index, inp.base_epoch, delay, floor_early=params.floor_early
        )
        return Prediction(
            stop_time_updates=updates,
            delay=delay,
            stop_sequence=stops[next_index]["stop_sequence"],
        )
