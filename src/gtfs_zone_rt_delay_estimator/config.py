"""Which prediction method, at which params, each tracker uses.

Read from the YAML file named by `PREDICTORS_CONFIG`:

    default:
      method: gps
    feeds:
      columbia-county:
        method: gps
        params: {depart_dist: 200}
    trackers:
      columbia-county/buswhere:
        method: gps

`feeds` is keyed by feed name and `trackers` by `<feed name>/<nickname>`,
neither of which is a secret. A tracker entry wins over its feed's, which wins
over `default`.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gtfs_zone_rt_delay_estimator.predictors import (
    PREDICTORS,
    Predictor,
    PredictorParams,
    params_hash,
)

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """The predictors config cannot be used as written."""


class MethodChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class PredictorsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default: MethodChoice = MethodChoice(method="gps")
    feeds: dict[str, MethodChoice] = Field(default_factory=dict)
    trackers: dict[str, MethodChoice] = Field(default_factory=dict)


@dataclass(frozen=True)
class Resolved:
    """A method instance with validated params, ready to call."""

    predictor: Predictor
    params: PredictorParams
    params_hash: str

    @property
    def method(self) -> str:
        return self.predictor.name


def _resolve(where: str, choice: MethodChoice) -> Resolved:
    cls = PREDICTORS.get(choice.method)
    if cls is None:
        known = ", ".join(sorted(PREDICTORS))
        raise ConfigError(f"{where}: unknown method {choice.method!r} (known: {known})")
    try:
        params = cls.Params(**choice.params)
    except ValidationError as e:
        raise ConfigError(f"{where}: bad params for {choice.method!r}: {e}") from e
    return Resolved(cls(), params, params_hash(params))


@dataclass(frozen=True)
class Predictors:
    """Every config entry, resolved and validated up front."""

    default: Resolved
    feeds: dict[str, Resolved]
    trackers: dict[str, Resolved]

    def for_tracker(self, feed_name: str | None, nickname: str | None) -> Resolved:
        if feed_name is not None:
            tracker = self.trackers.get(f"{feed_name}/{nickname}")
            if tracker is not None:
                return tracker
            feed = self.feeds.get(feed_name)
            if feed is not None:
                return feed
        return self.default


def build(config: PredictorsConfig) -> Predictors:
    return Predictors(
        default=_resolve("default", config.default),
        feeds={k: _resolve(f"feeds.{k}", v) for k, v in config.feeds.items()},
        trackers={k: _resolve(f"trackers.{k}", v) for k, v in config.trackers.items()},
    )


def load(path: str | None) -> Predictors:
    """Load and validate the config at `path`.

    No path, or a path that does not exist, gives `gps` at default params for
    every tracker. Anything else wrong raises `ConfigError` naming the file.
    """
    if not path or not Path(path).exists():
        if path:
            log.warning("%s does not exist; using the built-in default", path)
        return build(PredictorsConfig())
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        config = PredictorsConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return build(config)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
