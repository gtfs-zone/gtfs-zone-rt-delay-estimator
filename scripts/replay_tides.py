"""Replay a TIDES package's vehicle_locations through a prediction method.

Writes a TIDES package whose stop_time_predictions.csv holds what the method
would have predicted live, for scoring against the recorded producer.

    uv run scripts/replay_tides.py --feed columbia-county \\
        --date 2026-10-12..2026-11-20 --method gps --out out/

    uv run scripts/replay_tides.py --package path/to/pkg --gtfs gtfs.zip \\
        --method gps --params '{"depart_dist": 150}' --out out/

Each package is read from `--package`, or fetched from rt-archive into
`<out>/source/<feed>/<date>/`. The replay goes to
`<out>/<producer>/<feed>/<date>/`.
"""

import argparse
import csv
import gzip
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gtfs_zone_rt_delay_estimator.main import HISTORY_WINDOW, TRIP_UPDATE_TTL
from gtfs_zone_rt_delay_estimator.predictors import (
    PREDICTORS,
    Fix,
    Predictor,
    PredictorInput,
    PredictorParams,
    params_hash,
)
from gtfs_zone_rt_delay_estimator.trip_math import service_day_base

ARCHIVE = "https://rt-archive.gtfs.zone"
TABLES = ("trips_performed", "stop_visits", "vehicle_locations")
PREDICTION_COLUMNS = [
    "service_date",
    "trip_id_performed",
    "trip_stop_sequence",
    "producer",
    "prediction_timestamp",
    "trip_id_scheduled",
    "scheduled_stop_sequence",
    "vehicle_id",
    "stop_id",
    "predicted_arrival_time",
    "predicted_departure_time",
    "arrival_uncertainty",
    "departure_uncertainty",
    "schedule_relationship",
]


def iso(ts: int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# GTFS
# ---------------------------------------------------------------------------


@dataclass
class Gtfs:
    tz: ZoneInfo
    # trip_id to its stop_times, sorted by stop_sequence, with coordinates
    # where the stop has them.
    trips: dict[str, list[dict]]


def _zip_csv(zf: zipfile.ZipFile, name: str) -> csv.DictReader:
    return csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig"))


def load_gtfs(path: Path, trip_ids: set[str]) -> Gtfs:
    """The agency zone and the stop_times of `trip_ids` from a GTFS zip."""
    with zipfile.ZipFile(path) as zf:
        agency = next(iter(_zip_csv(zf, "agency.txt")))
        stops = {
            r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]))
            for r in _zip_csv(zf, "stops.txt")
            if r.get("stop_lat") and r.get("stop_lon")
        }
        trips: dict[str, list[dict]] = defaultdict(list)
        for r in _zip_csv(zf, "stop_times.txt"):
            if r["trip_id"] not in trip_ids:
                continue
            lat, lon = stops.get(r["stop_id"], (None, None))
            trips[r["trip_id"]].append(
                {
                    "trip_id": r["trip_id"],
                    "stop_id": r["stop_id"],
                    "arrival_time": r.get("arrival_time") or None,
                    "departure_time": r.get("departure_time") or None,
                    "stop_sequence": int(r["stop_sequence"]),
                    "stop_lat": lat,
                    "stop_lon": lon,
                }
            )
    for sts in trips.values():
        sts.sort(key=lambda s: s["stop_sequence"])
    return Gtfs(ZoneInfo(agency["agency_timezone"]), dict(trips))


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

# (predicted arrival, predicted departure); (None, None) is a withdrawal.
State = tuple[int | None, int | None]
WITHDRAWN: State = (None, None)


@dataclass
class _Trip:
    """Replay state for one performed trip."""

    service_date: str
    trip_id: str
    # Every scheduled stop, including any without coordinates, so
    # trip_stop_sequence matches the archive's (index + 1).
    all_stops: list[dict]
    # The stops the live worker would load: only those with coordinates.
    stops: list[dict]
    actuals: dict[int, int]
    last: dict[int, State] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    history: deque[Fix] = field(default_factory=deque)
    last_write: int | None = None
    last_ts: int | None = None

    def __post_init__(self) -> None:
        self.index = {s["stop_sequence"]: i for i, s in enumerate(self.all_stops)}

    def emit(self, idx: int, ts: int, state: State, vehicle_id: str | None) -> None:
        if state == self.last.get(idx, WITHDRAWN):
            return
        if state == WITHDRAWN:
            self.last.pop(idx, None)
            actual = self.actuals.get(idx)
            # A stop that left the prediction because it was served needs no
            # withdrawal row.
            if actual is not None and ts >= actual:
                return
        else:
            self.last[idx] = state
        stop = self.all_stops[idx]
        self.rows.append(
            {
                "service_date": self.service_date,
                "trip_stop_sequence": idx + 1,
                "prediction_timestamp": iso(ts),
                "trip_id_scheduled": self.trip_id,
                "scheduled_stop_sequence": stop["stop_sequence"],
                "vehicle_id": vehicle_id or "",
                "stop_id": stop["stop_id"],
                "predicted_arrival_time": iso(state[0]),
                "predicted_departure_time": iso(state[1]),
                "schedule_relationship": "" if state == WITHDRAWN else "Scheduled",
            }
        )

    def withdraw_all(self, ts: int) -> None:
        for idx in list(self.last):
            self.emit(idx, ts, WITHDRAWN, None)

    def expire(self, now: int | None) -> None:
        """Withdraw everything if the live record would have expired by `now`."""
        if self.last_write is None:
            return
        expires = self.last_write + TRIP_UPDATE_TTL
        if now is None or now > expires:
            self.withdraw_all(expires)
            self.last_write = None


def replay(
    package: Path,
    gtfs_path: Path,
    predictor: Predictor,
    params: PredictorParams,
    producer: str,
) -> list[dict]:
    """stop_time_predictions rows for `package` as `predictor` would make them."""
    locations = read_csv(package / "vehicle_locations.csv")
    performed = {
        r["trip_id_performed"]: r for r in read_csv(package / "trips_performed.csv")
    }
    visits: dict[str, dict[int, int]] = defaultdict(dict)
    for v in read_csv(package / "stop_visits.csv"):
        actual = epoch(v.get("actual_arrival_time")) or epoch(
            v.get("actual_departure_time")
        )
        if actual is not None:
            visits[v["trip_id_performed"]][int(v["trip_stop_sequence"]) - 1] = actual

    by_trip: dict[str, list[dict]] = defaultdict(list)
    for r in locations:
        if r.get("trip_id_performed") and r.get("latitude") and r.get("longitude"):
            by_trip[r["trip_id_performed"]].append(r)
    scheduled = {
        key: (
            performed.get(key, {}).get("trip_id_scheduled")
            or fixes[0].get("trip_id_scheduled")
            or key
        )
        for key, fixes in by_trip.items()
    }
    gtfs = load_gtfs(gtfs_path, set(scheduled.values()))

    out: list[dict] = []
    for key, fixes in sorted(by_trip.items()):
        all_stops = gtfs.trips.get(scheduled[key])
        if not all_stops:
            continue
        stops = [s for s in all_stops if s["stop_lat"] is not None]
        trip = _Trip(
            service_date=fixes[0].get("service_date") or "",
            trip_id=scheduled[key],
            all_stops=all_stops,
            stops=stops,
            actuals=visits.get(key, {}),
        )
        fixes.sort(key=lambda r: r["event_timestamp"])
        for r in fixes:
            ts = epoch(r["event_timestamp"])
            if ts is None or (trip.last_ts is not None and ts <= trip.last_ts):
                continue
            trip.last_ts = ts
            trip.expire(ts)
            lat, lon = float(r["latitude"]), float(r["longitude"])
            while trip.history and trip.history[0].timestamp < ts - HISTORY_WINDOW:
                trip.history.popleft()
            inp = PredictorInput(
                lat=lat,
                lon=lon,
                timestamp=ts,
                stops=stops,
                base_epoch=service_day_base(ts, gtfs.tz, stops),
                bearing=float(r["heading"]) if r.get("heading") else None,
                speed=float(r["speed"]) if r.get("speed") else None,
                history=tuple(trip.history),
            )
            trip.history.append(Fix(ts, lat, lon))
            prediction = predictor.predict(inp, params)
            if prediction is None:
                continue
            states: dict[int, State] = {}
            for u in prediction.stop_time_updates:
                states[trip.index[u["stop_sequence"]]] = (
                    u.get("arrival_time"),
                    u.get("departure_time"),
                )
            for idx in sorted(set(trip.last) | set(states)):
                trip.emit(idx, ts, states.get(idx, WITHDRAWN), r.get("vehicle_id"))
            trip.last_write = ts
        trip.expire(None)
        for row in trip.rows:
            row.update(trip_id_performed=key, producer=producer)
        out.extend(trip.rows)
    out.sort(
        key=lambda r: (
            r["trip_id_performed"],
            r["trip_stop_sequence"],
            r["prediction_timestamp"],
        )
    )
    return out


def write_package(
    source: Path,
    dest: Path,
    rows: list[dict],
    method: str,
    params: PredictorParams,
    producer: str,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        if (source / f"{table}.csv").exists():
            shutil.copyfile(source / f"{table}.csv", dest / f"{table}.csv")
    with (dest / "stop_time_predictions.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f, PREDICTION_COLUMNS, restval="", extrasaction="raise", lineterminator="\n"
        )
        w.writeheader()
        w.writerows(rows)

    dp = json.loads((source / "datapackage.json").read_text())
    dp["name"] = f"{dp.get('name', source.name)}-{producer}"
    dp["description"] = (
        f"stop_time_predictions replayed through the {method!r} method of "
        "gtfs-zone-rt-delay-estimator from this package's vehicle_locations."
    )
    meta = dp.setdefault("gtfs_zone", {})
    meta.setdefault("counts", {})["stop_time_predictions"] = len(rows)
    meta["producer"] = producer
    meta["method"] = method
    meta["params"] = params.model_dump(mode="json")
    meta["params_hash"] = params_hash(params)
    (dest / "datapackage.json").write_text(json.dumps(dp, indent=1) + "\n")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _get(url: str) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "gzip", "User-Agent": "replay-tides"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_package(feed: str, d: date, dest: Path) -> Path | None:
    """Download one archived package into `dest`, reusing a complete copy."""
    if (dest / "datapackage.json").exists():
        return dest
    base = f"{ARCHIVE}/tides/{feed}/{d.isoformat()}"
    dp = _get(f"{base}/datapackage.json")
    if dp is None:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    for res in json.loads(dp)["resources"]:
        data = _get(f"{base}/{res['path']}")
        if data is not None:
            (dest / res["path"]).write_bytes(data)
    (dest / "datapackage.json").write_bytes(dp)
    return dest


def fetch_gtfs(package: Path, cache: Path) -> Path:
    url = json.loads((package / "datapackage.json").read_text())["gtfs_zone"]["gtfs"]
    path = cache / url.removeprefix(f"{ARCHIVE}/")
    if not path.exists():
        data = _get(url)
        if data is None:
            raise SystemExit(f"{url}: not found")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path


def parse_dates(spec: str) -> list[date]:
    """`YYYY-MM-DD` or an inclusive `YYYY-MM-DD..YYYY-MM-DD` range."""
    first, _, last = spec.partition("..")
    start = date.fromisoformat(first)
    end = date.fromisoformat(last) if last else start
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_params(spec: str | None) -> dict:
    if not spec:
        return {}
    path = Path(spec)
    return json.loads(path.read_text() if path.exists() else spec)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--package", type=Path, help="a TIDES package directory")
    src.add_argument("--feed", help="an rt-archive feed name, with --date")
    ap.add_argument("--date", help="YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD")
    ap.add_argument("--gtfs", type=Path, help="GTFS zip (default: the package's)")
    ap.add_argument("--method", default="gps", choices=sorted(PREDICTORS))
    ap.add_argument("--params", help="JSON object, or a path to a JSON file")
    ap.add_argument(
        "--producer", help="producer column value (default: estimator-<method>)"
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    cls = PREDICTORS[args.method]
    params = cls.Params(**parse_params(args.params))
    producer = args.producer or f"estimator-{args.method}"

    jobs: list[tuple[Path, Path]] = []
    if args.package:
        jobs.append((args.package, args.out / producer / args.package.name))
    else:
        if not args.date:
            ap.error("--feed needs --date")
        for d in parse_dates(args.date):
            pkg = fetch_package(
                args.feed, d, args.out / "source" / args.feed / d.isoformat()
            )
            if pkg is None:
                print(f"{args.feed} {d}: no package", file=sys.stderr)
                continue
            jobs.append((pkg, args.out / producer / args.feed / d.isoformat()))

    for pkg, dest in jobs:
        gtfs = args.gtfs or fetch_gtfs(pkg, args.out)
        rows = replay(pkg, gtfs, cls(), params, producer)
        write_package(pkg, dest, rows, args.method, params, producer)
        print(f"{dest}: {len(rows)} predictions", file=sys.stderr)


if __name__ == "__main__":
    main()
