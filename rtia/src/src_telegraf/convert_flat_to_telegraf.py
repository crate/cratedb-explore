#
# Licensed to Crate.io GmbH ("Crate") under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  Crate licenses
# this file to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.  You may
# obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
# However, if you have executed another commercial license agreement
# with Crate these terms will supersede the license and you may use the
# software solely pursuant to the terms of the relevant commercial agreement.

"""
Convert the legacy *flat* IoT dataset into the Telegraf-friendly *nested*
shape that `rtia.iot_data` is COPY FROM'd from.

The original demo dataset shipped one flat JSON object per line:

    {"device_id": "...", "device_type": "...", "plant_id": "...",
     "line_id": "...", "timestamp": "...", "metric_value": 71.14,
     "metric_unit": "C", "status": "normal", "quality_score": 93.1,
     "geo_location": [lon, lat],
     "metadata": {"firmware_version": "...", "model": "...",
                  "installation_date": "...", "last_calibration": "..."}}

`rtia.iot_data` (see rtia/sql/rtia_schema_create.sql) instead expects Telegraf's
metric model — `hash_id` / `timestamp` / `name` / `tags{}` / `fields{}` — so
the COPY dataset is reshaped to:

    {"hash_id": <bigint>, "timestamp": "...", "name": "iot_data",
     "tags":   {"device_id", "device_type", "plant_id", "line_id",
                "status", "metric_unit",
                "metadata_firmware_version", "metadata_model",
                "metadata_installation_date", "metadata_last_calibration"},
     "fields": {"metric_value", "quality_score", "geo_lon", "geo_lat"}}

Key points, mirroring the schema:
  * `metadata.*`        -> `tags.metadata_*`     (flattened, indexed strings)
  * `geo_location[0/1]` -> `fields.geo_lon/lat`  (the generated GEO_POINT
                                                  `geo_location` is rebuilt
                                                  server-side from these)
  * `hash_id` is a signed 64-bit FNV-1a hash of name + sorted tags + timestamp,
    so it is deterministic per (device, timestamp) row. The PK is
    (hash_id, timestamp, day), so this keeps every reading distinct.

Input and output may be plain NDJSON or gzip (detected from the .gz suffix);
both are streamed, so the multi-GB dataset never has to fit in memory.

Usage:
    python convert_flat_to_telegraf.py --input  rtia/data/iot_demo_dataset.json.gz \
                                       --output rtia/data/iot_demo_dataset.telegraf.json.gz
"""

import argparse
import gzip
import json
import sys
import time

FNV64_OFFSET = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
UINT64_MASK = 0xFFFFFFFFFFFFFFFF

NAME = "iot_data"


def hash_id(name: str, tags: dict, timestamp: str) -> int:
    """Signed 64-bit FNV-1a over name + sorted tags + timestamp.

    Telegraf's outputs.cratedb hashes name + sorted tags (a per-series id);
    we fold in the timestamp as well so the COPY-loaded rows get a per-reading
    hash_id, matching the original dataset's per-row values.
    """
    buf = bytearray()
    buf += name.encode()
    buf += b"\n"
    for key in sorted(tags):
        buf += key.encode()
        buf += b"\n"
        buf += str(tags[key]).encode()
        buf += b"\n"
    buf += timestamp.encode()

    h = FNV64_OFFSET
    for byte in buf:
        h ^= byte
        h = (h * FNV64_PRIME) & UINT64_MASK
    return h - (1 << 64) if h >= (1 << 63) else h  # to signed int64


def to_telegraf(rec: dict) -> dict:
    md = rec.get("metadata") or {}
    lon, lat = rec["geo_location"]

    tags = {
        "device_id": rec["device_id"],
        "device_type": rec["device_type"],
        "plant_id": rec["plant_id"],
        "line_id": rec["line_id"],
        "status": rec["status"],
        "metric_unit": rec["metric_unit"],
        "metadata_firmware_version": md.get("firmware_version"),
        "metadata_model": md.get("model"),
        "metadata_installation_date": md.get("installation_date"),
        "metadata_last_calibration": md.get("last_calibration"),
    }
    fields = {
        "metric_value": rec["metric_value"],
        "quality_score": rec["quality_score"],
        "geo_lon": lon,
        "geo_lat": lat,
    }
    ts = rec["timestamp"]
    return {
        "hash_id": hash_id(NAME, tags, ts),
        "timestamp": ts,
        "name": NAME,
        "tags": tags,
        "fields": fields,
    }


def _open_in(path: str):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") \
        else open(path, "rt", encoding="utf-8")


def _open_out(path: str):
    return gzip.open(path, "wt", encoding="utf-8") if path.endswith(".gz") \
        else open(path, "wt", encoding="utf-8")


def convert(inp: str, outp: str) -> None:
    n = 0
    t0 = time.time()
    with _open_in(inp) as fin, _open_out(outp) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            fout.write(json.dumps(to_telegraf(json.loads(line)),
                                  separators=(", ", ": ")))
            fout.write("\n")
            n += 1
            if n % 100000 == 0:
                rate = n / (time.time() - t0)
                print(f"  {n:>12,} records  ({rate:,.0f}/s)", end="\r")
    print(f"\n  Done: {n:,} records  ->  {outp}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="flat NDJSON (.json or .json.gz)")
    p.add_argument("--output", required=True, help="nested NDJSON (.json or .json.gz)")
    args = p.parse_args()
    if args.input == args.output:
        sys.exit("--input and --output must differ")
    convert(args.input, args.output)
