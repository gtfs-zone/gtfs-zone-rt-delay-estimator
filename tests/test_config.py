import pytest

from gtfs_zone_rt_delay_estimator import config
from gtfs_zone_rt_delay_estimator.predictors.gps import GpsParams


def _write(tmp_path, text):
    path = tmp_path / "predictors.yaml"
    path.write_text(text)
    return str(path)


def test_no_path_is_gps_at_defaults():
    predictors = config.load(None)
    assert predictors.default.method == "gps"
    assert predictors.default.params == GpsParams()
    assert predictors.feeds == {} and predictors.trackers == {}


def test_missing_file_is_gps_at_defaults(tmp_path):
    predictors = config.load(str(tmp_path / "absent.yaml"))
    assert predictors.default.params == GpsParams()


def test_default_only(tmp_path):
    path = _write(tmp_path, "default:\n  method: gps\n  params: {depart_dist: 150}\n")
    predictors = config.load(path)
    assert predictors.default.params.depart_dist == 150
    assert predictors.for_tracker("any", "one").params.depart_dist == 150


def test_per_feed_and_per_tracker(tmp_path):
    path = _write(
        tmp_path,
        """
default:
  method: gps
feeds:
  columbia-county:
    method: gps
    params: {depart_dist: 100}
trackers:
  columbia-county/bus-1:
    method: gps
    params: {floor_early: false}
""",
    )
    predictors = config.load(path)
    assert predictors.for_tracker("columbia-county", "bus-1").params == GpsParams(
        floor_early=False
    )
    assert predictors.for_tracker("columbia-county", "bus-2").params == GpsParams(
        depart_dist=100
    )
    assert predictors.for_tracker("hudsonlink", "bus-1").params == GpsParams()
    assert predictors.for_tracker(None, None).params == GpsParams()


def test_params_hash_follows_params(tmp_path):
    path = _write(
        tmp_path,
        "feeds:\n  a: {method: gps, params: {depart_dist: 100}}\n  b: {method: gps}\n",
    )
    predictors = config.load(path)
    assert predictors.feeds["a"].params_hash != predictors.feeds["b"].params_hash
    assert predictors.feeds["b"].params_hash == predictors.default.params_hash


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("default: {method: nope}\n", "unknown method 'nope'"),
        ("feeds:\n  x: {method: gps, params: {bogus: 1}}\n", "feeds.x"),
        ("default: {method: gps, params: {depart_dist: far}}\n", "depart_dist"),
        ("defualt: {method: gps}\n", "defualt"),
        ("default: [\n", "predictors.yaml"),
    ],
)
def test_bad_config_raises_with_the_path(tmp_path, text, needle):
    path = _write(tmp_path, text)
    with pytest.raises(config.ConfigError) as e:
        config.load(path)
    assert path in str(e.value)
    assert needle in str(e.value)
