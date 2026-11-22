import csv
import importlib.util
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gtfs_zone_rt_delay_estimator.main import TRIP_UPDATE_TTL
from gtfs_zone_rt_delay_estimator.predictors import params_hash
from gtfs_zone_rt_delay_estimator.predictors.gps import GpsParams

_spec = importlib.util.spec_from_file_location(
    "replay_tides", Path(__file__).parent.parent / "scripts" / "replay_tides.py"
)
replay_tides = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_tides)

D = "2026-07-24"
# Stops 0.1 degrees of latitude apart, scheduled 10 min apart (UTC zone).
LATS = [45.0, 45.1, 45.2]


def _iso(hh, mm, ss=0):
    return f"{D}T{hh:02d}:{mm:02d}:{ss:02d}Z"


def _epoch(hh, mm, ss=0):
    return int(datetime(2026, 7, 24, hh, mm, ss, tzinfo=UTC).timestamp())


def _write(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _gtfs(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "A,A,https://example.com,UTC\n",
        )
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            + "".join(f"S{i},S{i},{lat},-73.0\n" for i, lat in enumerate(LATS)),
        )
        lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
        for trip, hour in (("T1", 8), ("T2", 9)):
            for i in range(3):
                t = f"{hour:02d}:{10 * i:02d}:00"
                lines.append(f"{trip},{t},{t},S{i},{i + 1}")
        zf.writestr("stop_times.txt", "\n".join(lines) + "\n")


def _package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _gtfs(tmp_path / "gtfs.zip")
    _write(
        pkg / "trips_performed.csv",
        [
            {"service_date": D, "trip_id_performed": t, "trip_id_scheduled": t}
            for t in ("T1", "T2")
        ],
    )
    # T1 visits stop 2 at 08:12; T2 has no actuals.
    _write(
        pkg / "stop_visits.csv",
        [
            {
                "service_date": D,
                "trip_id_performed": "T1",
                "trip_stop_sequence": i + 1,
                "actual_arrival_time": _iso(8, 12) if i == 1 else "",
                "actual_departure_time": _iso(8, 12) if i == 1 else "",
            }
            for i in range(3)
        ],
    )
    fixes = [
        # T1: halfway to stop 2 two minutes late, then past it.
        ("T1", 8, 7, 45.05),
        ("T1", 8, 7, 45.05),  # duplicate timestamp, skipped
        ("T1", 8, 8, 45.06),
        ("T1", 8, 17, 45.15),
        # T2: one fix, then a gap longer than the trip update TTL.
        ("T2", 9, 5, 45.05),
        ("T2", 9, 15, 45.15),
    ]
    _write(
        pkg / "vehicle_locations.csv",
        [
            {
                "location_ping_id": f"v:{n}",
                "service_date": D,
                "event_timestamp": _iso(hh, mm),
                "trip_id_performed": trip,
                "trip_id_scheduled": trip,
                "vehicle_id": "v",
                "latitude": lat,
                "longitude": -73.0,
                "heading": "",
                "speed": "",
            }
            for n, (trip, hh, mm, lat) in enumerate(fixes)
        ],
    )
    (pkg / "datapackage.json").write_text(
        json.dumps({"name": "f-" + D, "gtfs_zone": {"counts": {}}})
    )
    return pkg


@pytest.fixture
def rows(tmp_path):
    pkg = _package(tmp_path)
    return replay_tides.replay(
        pkg,
        tmp_path / "gtfs.zip",
        replay_tides.PREDICTORS["gps"](),
        GpsParams(),
        "estimator-gps",
    )


def _stop(rows, trip, seq):
    return [
        r
        for r in rows
        if r["trip_id_performed"] == trip and r["trip_stop_sequence"] == seq
    ]


def test_rows_carry_the_producer_and_keys(rows):
    assert {r["producer"] for r in rows} == {"estimator-gps"}
    assert {r["trip_id_performed"] for r in rows} == {"T1", "T2"}
    first = _stop(rows, "T1", 2)[0]
    assert first["prediction_timestamp"] == _iso(8, 7)
    assert first["scheduled_stop_sequence"] == 2
    assert first["stop_id"] == "S1"
    assert first["predicted_arrival_time"] == _iso(8, 12)


def test_a_row_only_when_the_prediction_changes(rows):
    # 08:08 at 45.06 is still 2 min late: stop 3 keeps its 08:22 prediction.
    stop3 = _stop(rows, "T1", 3)
    assert [r["predicted_arrival_time"] for r in stop3][:1] == [_iso(8, 22)]
    assert [r["prediction_timestamp"] for r in stop3].count(_iso(8, 8)) == 0


def test_a_served_stop_gets_no_withdrawal(rows):
    # Stop 2 left the prediction at 08:17, after its 08:12 actual.
    assert all(r["predicted_arrival_time"] for r in _stop(rows, "T1", 2))


def test_an_unserved_stop_is_withdrawn_when_it_leaves(rows):
    # T2 has no actuals, so stop 2 leaving the prediction is a withdrawal.
    stop2 = _stop(rows, "T2", 2)
    assert stop2[-1]["predicted_arrival_time"] == ""
    assert stop2[-1]["schedule_relationship"] == ""


def test_a_gap_past_the_ttl_withdraws_at_expiry(rows):
    stop3 = _stop(rows, "T2", 3)
    expiry = replay_tides.iso(_epoch(9, 5) + TRIP_UPDATE_TTL)
    assert [r["prediction_timestamp"] for r in stop3] == [
        _iso(9, 5),
        expiry,
        _iso(9, 15),
        replay_tides.iso(_epoch(9, 15) + TRIP_UPDATE_TTL),
    ]
    assert stop3[1]["predicted_arrival_time"] == ""


def test_write_package(tmp_path, rows):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "out"
    params = GpsParams(depart_dist=150)
    replay_tides.write_package(pkg, dest, rows, "gps", params, "estimator-gps")
    assert (dest / "vehicle_locations.csv").read_text() == (
        pkg / "vehicle_locations.csv"
    ).read_text()
    with (dest / "stop_time_predictions.csv").open() as f:
        written = list(csv.DictReader(f))
    assert len(written) == len(rows)
    assert list(written[0]) == replay_tides.PREDICTION_COLUMNS
    meta = json.loads((dest / "datapackage.json").read_text())["gtfs_zone"]
    assert meta["method"] == "gps"
    assert meta["params"]["depart_dist"] == 150
    assert meta["params_hash"] == params_hash(params)
    assert meta["counts"]["stop_time_predictions"] == len(rows)


def test_parse_dates():
    days = replay_tides.parse_dates("2026-10-30..2026-11-02")
    assert [d.isoformat() for d in days] == [
        "2026-10-30",
        "2026-10-31",
        "2026-11-01",
        "2026-11-02",
    ]
    assert len(replay_tides.parse_dates("2026-10-30")) == 1


def test_parse_params(tmp_path):
    assert replay_tides.parse_params('{"depart_dist": 1}') == {"depart_dist": 1}
    path = tmp_path / "p.json"
    path.write_text('{"floor_early": false}')
    assert replay_tides.parse_params(str(path)) == {"floor_early": False}
