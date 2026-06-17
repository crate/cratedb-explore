"""
Synthetic generator for the RTIA Industrial-IoT demo dataset.

Produces the same sensor-reading stream that ships as `iot_demo_dataset.json`,
so you can regenerate it at any size / time window instead of depending on the
S3 copy. Output is deterministic for a given --seed.

The shape and distributions mirror the real dataset (measured from it):

  * 5 plants (geo from data/plants.json), 50 production lines (LINE_{A..E}{1..10})
  * 5 sensor types, each with a fixed unit and its own value range:
        temperature_sensor  C       ~N(65, 14)   [   5 .. 115]
        vibration_sensor    mm/s    ~|N(2.4,1.6)| [   0 ..  16]
        power_meter         kW      ~N(365,150)   [   0 .. 945]
        flow_meter          m3/h    ~N(218, 93)   [   0 .. 485]
        pressure_sensor     bar     ~N(4.2,1.35)  [   0 ..  10]
  * one reading per device per hour (+/- a few minutes of jitter)
  * status mix  normal 91.7% / warning 5.2% / offline 1.7% / critical 1.4%
        offline  -> metric_value forced to 0, quality ~15
        warning  -> quality ~83,  critical -> quality ~67,  normal -> quality ~95
  * each device carries stable tags: device_type, plant_id, line_id and a
    metadata block (firmware_version, model, installation_date, last_calibration);
    its geo_location is the plant location jittered by up to ~0.08 deg.

Records are built in the legacy *flat* shape and (by default) reshaped to the
Telegraf-friendly nested shape via convert_flat_to_telegraf.to_telegraf, so the
`hash_id` / tags / fields mapping has a single source of truth.

Usage:
    # ~2,000,000 rows (334 devices x ~250 days), gzip, Telegraf shape
    python generate_iot_dataset.py --output data/iot_demo_dataset.json.gz

    # small flat sample for inspection
    python generate_iot_dataset.py --devices 10 --days 2 \
        --format flat --output sample.json
"""

import argparse
import datetime as dt
import gzip
import json
import os
import random

from convert_flat_to_telegraf import to_telegraf

BASE = os.path.dirname(os.path.abspath(__file__))
PLANTS_FILE = os.path.join(BASE, "..", "data", "plants.json")

# ---------------------------------------------------------------------------
# Fixed vocabulary (measured from the real dataset)
# ---------------------------------------------------------------------------
LINES = [f"LINE_{b}{n}" for b in "ABCDE" for n in range(1, 11)]  # 50 lines

# device_type -> (unit, value mean, value std, clamp-min, clamp-max)
SENSOR_TYPES = {
    "temperature_sensor": ("C",    65.0, 14.0,   5.0, 115.0),
    "vibration_sensor":   ("mm/s",  2.4,  1.6,   0.0,  16.0),
    "power_meter":        ("kW",  365.0, 150.0,  0.0, 945.0),
    "flow_meter":         ("m3/h",218.0,  93.0,  0.0, 485.0),
    "pressure_sensor":    ("bar",   4.2,  1.35,  0.0,  10.0),
}
# relative share of each type (from the real type counts)
TYPE_WEIGHTS = {
    "temperature_sensor": 0.336,
    "vibration_sensor":   0.202,
    "power_meter":        0.198,
    "flow_meter":         0.132,
    "pressure_sensor":    0.132,
}

# status -> (probability, quality mean, quality std)
STATUS = {
    "normal":   (0.917, 95.0,  4.0),
    "warning":  (0.052, 83.0,  6.0),
    "offline":  (0.017, 15.0,  8.0),
    "critical": (0.014, 67.0, 10.0),
}
STATUS_NAMES = list(STATUS)
STATUS_WEIGHTS = [STATUS[s][0] for s in STATUS_NAMES]

READING_INTERVAL_S = 3600          # one reading per hour
READING_JITTER_S = 600             # 0..10 min past the slot (never before start)


def _firmware(rng: random.Random) -> str:
    return f"{rng.randint(1, 3)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"


def _date_between(rng: random.Random, start: dt.date, end: dt.date) -> str:
    span = (end - start).days
    return (start + dt.timedelta(days=rng.randint(0, span))).isoformat()


def build_roster(n_devices: int, plants: dict, rng: random.Random) -> list[dict]:
    """Create n stable device definitions distributed over plants/lines/types."""
    plant_ids = list(plants)
    types = list(TYPE_WEIGHTS)
    weights = list(TYPE_WEIGHTS.values())
    roster = []
    for i in range(1, n_devices + 1):
        dtype = rng.choices(types, weights=weights, k=1)[0]
        plant_id = plant_ids[(i - 1) % len(plant_ids)]
        plon, plat = plants[plant_id]
        roster.append({
            "device_id":   f"DEVICE_{i:04d}",
            "device_type": dtype,
            "plant_id":    plant_id,
            "line_id":     rng.choice(LINES),
            "metric_unit": SENSOR_TYPES[dtype][0],
            "geo_location": [
                round(plon + rng.uniform(-0.08, 0.08), 4),
                round(plat + rng.uniform(-0.08, 0.08), 4),
            ],
            "metadata": {
                "firmware_version":  _firmware(rng),
                "model":             f"SX-{rng.randint(100, 499)}",
                "installation_date": _date_between(rng, dt.date(2021, 1, 1),
                                                   dt.date(2023, 12, 31)),
                "last_calibration":  _date_between(rng, dt.date(2025, 1, 1),
                                                   dt.date(2025, 8, 31)),
            },
        })
    return roster


def _reading(dev: dict, when: dt.datetime, rng: random.Random) -> dict:
    _, mean, std, lo, hi = SENSOR_TYPES[dev["device_type"]]
    status = rng.choices(STATUS_NAMES, weights=STATUS_WEIGHTS, k=1)[0]

    if status == "offline":
        value = 0.0
    else:
        value = rng.gauss(mean, std)
        # warning/critical readings drift toward the extremes
        if status in ("warning", "critical"):
            value += rng.choice((-1, 1)) * std * (1.5 if status == "critical" else 0.8)
        value = round(min(max(value, lo), hi), 2)

    _, qmean, qstd = STATUS[status]
    quality = round(min(max(rng.gauss(qmean, qstd), 0.0), 100.0), 1)

    return {
        "device_id":     dev["device_id"],
        "device_type":   dev["device_type"],
        "plant_id":      dev["plant_id"],
        "line_id":       dev["line_id"],
        "timestamp":     when.strftime("%Y-%m-%d %H:%M:%S"),
        "metric_value":  value,
        "metric_unit":   dev["metric_unit"],
        "status":        status,
        "quality_score": quality,
        "geo_location":  dev["geo_location"],
        "metadata":      dev["metadata"],
    }


def _open_out(path: str):
    return gzip.open(path, "wt", encoding="utf-8") if path.endswith(".gz") \
        else open(path, "wt", encoding="utf-8")


def generate(output: str, n_devices: int, start: dt.datetime, days: int,
             fmt: str, seed: int) -> None:
    rng = random.Random(seed)
    plants = {p["plant_id"]: p["geo_location"]
              for p in (json.loads(x) for x in open(PLANTS_FILE))}
    roster = build_roster(n_devices, plants, rng)
    n_readings = days * 24
    reshape = to_telegraf if fmt == "telegraf" else (lambda r: r)

    total = 0
    with _open_out(output) as out:
        for dev in roster:
            # each device gets its own RNG stream for reproducibility. Seed with
            # a string so it's deterministic across processes (unlike hash()).
            drng = random.Random(f"{seed}:{dev['device_id']}")
            t = start
            for _ in range(n_readings):
                when = t + dt.timedelta(
                    seconds=drng.randint(0, READING_JITTER_S))
                rec = _reading(dev, when, drng)
                out.write(json.dumps(reshape(rec), separators=(", ", ": ")))
                out.write("\n")
                total += 1
                t += dt.timedelta(seconds=READING_INTERVAL_S)
    print(f"  Generated {total:,} readings "
          f"({n_devices} devices x {n_readings} hourly)  ->  {output}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", required=True,
                   help="destination (.json or .json.gz)")
    p.add_argument("--devices", type=int, default=334,
                   help="number of devices (default: 334)")
    p.add_argument("--start", default="2025-09-01",
                   help="first reading date, YYYY-MM-DD (default: 2025-09-01)")
    p.add_argument("--days", type=int, default=250,
                   help="days of hourly readings per device (default: 250 "
                        "-> ~2,000,000 rows at 334 devices)")
    p.add_argument("--format", choices=("telegraf", "flat"), default="telegraf",
                   help="output shape (default: telegraf nested)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    args = p.parse_args()

    generate(
        output=args.output,
        n_devices=args.devices,
        start=dt.datetime.strptime(args.start, "%Y-%m-%d"),
        days=args.days,
        fmt=args.format,
        seed=args.seed,
    )
