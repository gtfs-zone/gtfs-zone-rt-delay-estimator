"""The interface every prediction method implements.

A method sees only a `PredictorInput`, never the database or Redis, so the live
worker and the offline replay run the same code on the same inputs.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict


class PredictorParams(BaseModel):
    """Base for a method's params: unknown keys are rejected, instances are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Fix:
    """One earlier position on the same trip."""

    timestamp: int
    lat: float
    lon: float


@dataclass(frozen=True)
class PredictorInput:
    """One fix plus everything a method needs to predict the rest of its trip.

    stops: the trip's stop_times dicts with stop_lat/stop_lon, sorted by
    stop_sequence.
    base_epoch: the service day's local midnight (`service_day_base`).
    history: earlier fixes on this trip, oldest first, not including this one.
    """

    lat: float
    lon: float
    timestamp: int
    stops: list[dict]
    base_epoch: int
    bearing: float | None = None
    speed: float | None = None
    history: tuple[Fix, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Prediction:
    """Per-stop predictions in rt-api's ingest field names.

    delay and stop_sequence are advisory copies of the head of the list.
    """

    stop_time_updates: list[dict]
    delay: int
    stop_sequence: int


class Predictor(Protocol):
    """A prediction method: a registry name, a params model and `predict`."""

    name: ClassVar[str]
    Params: ClassVar[type[PredictorParams]]

    def predict(
        self, inp: PredictorInput, params: PredictorParams
    ) -> Prediction | None: ...
