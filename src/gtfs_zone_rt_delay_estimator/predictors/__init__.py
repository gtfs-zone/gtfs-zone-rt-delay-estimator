"""Registry of prediction methods, by the name a config selects them with."""

import hashlib
import json

from gtfs_zone_rt_delay_estimator.predictors.base import (
    Fix,
    Prediction,
    Predictor,
    PredictorInput,
    PredictorParams,
)
from gtfs_zone_rt_delay_estimator.predictors.gps import Gps

PREDICTORS: dict[str, type[Predictor]] = {Gps.name: Gps}


def params_hash(params: PredictorParams) -> str:
    """First 8 hex chars of the sha256 of the params' canonical JSON."""
    canonical = json.dumps(params.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


__all__ = [
    "PREDICTORS",
    "Fix",
    "Prediction",
    "Predictor",
    "PredictorInput",
    "PredictorParams",
    "params_hash",
]
