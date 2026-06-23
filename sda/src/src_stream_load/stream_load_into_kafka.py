#!/usr/bin/env python3
#
# Copyright 2026 Crate.io
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Stream the demo climate_data dataset into Kafka, split by latitude.

Every record is routed to one of three latitude **bands**, each shipped in a
different wire format (so a single run exercises all three encodings):

    northern band  -> Avro      (topic climate_data_north)
    central band   -> JSON      (topic climate_data_central)
    southern band  -> Protobuf  (topic climate_data_south)

The Avro and Protobuf bands register their schemas with a Confluent Schema
Registry, so one is always required. The whole stream honours --climate-rate
and --climate-limit. The destination is hidden behind the ``StreamSink``
interface (see sinks.py) so a non-Kafka platform can be dropped in later.

Usage:
    python stream_load_into_kafka.py [options]

    --bootstrap-servers HOST:PORT   Kafka brokers (default: localhost:9092;
                                    env KAFKA_BOOTSTRAP_SERVERS).
    --schema-registry-url URL       Confluent Schema Registry, used by the Avro
                                    and Protobuf bands (default:
                                    http://localhost:8081; env SCHEMA_REGISTRY_URL).
    --climate-rate N                Max records/sec, 0 = unlimited (default: 0).
    --climate-limit N               Cap total records (0 = all; default: 0).
    --south-max-lat L               Latitudes below L go south  (default: 50.0).
    --north-min-lat L               Latitudes >= L go north     (default: 52.0).
    --topic-prefix PREFIX           Prepended to every topic name (default: none).
    --source-url URL                Override the climate_data source URL.

Examples:
    # Smoke test against a local broker + registry with a small cap:
    python stream_load_into_kafka.py --climate-limit 3000 --climate-rate 500
    # Against named hosts:
    python stream_load_into_kafka.py --bootstrap-servers broker:9092 \
        --schema-registry-url http://registry:8081

Exit codes:
    0 — success
    1 — bad command-line argument
    2 — the source download failed
    3 — one or more records failed to deliver to the sink
"""

import argparse
import json
import os
import sys
import time

import requests

import serializers
from serializers import (
    BANDS,
    DEFAULT_NORTH_MIN_LAT,
    DEFAULT_SOUTH_MAX_LAT,
    band_for_latitude,
    climate_key,
    latitude_of,
)
from sinks import KafkaSink

S3_BASE = "https://guided-path.s3.us-east-1.amazonaws.com"
DEFAULT_SOURCE_URL = f"{S3_BASE}/export-demo_climate_data_large_v2.json"

EXIT_BAD_ARG = 1
EXIT_DOWNLOAD = 2
EXIT_DELIVERY = 3

# Report progress every this-many records.
_PROGRESS_EVERY = 50_000


class RateLimiter:
    """Paces a loop to at most ``rate`` events per second. 0 disables pacing."""

    def __init__(self, rate: float):
        self._interval = 1.0 / rate if rate and rate > 0 else 0.0
        self._next = None

    def wait(self) -> None:
        if not self._interval:
            return
        now = time.monotonic()
        if self._next is None:
            self._next = now
        sleep_for = self._next - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._next += self._interval


def iter_ndjson(url: str):
    """Yield one parsed object per line of a newline-delimited JSON document.

    Streams the response so large files are never held in memory at once.
    """
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        resp.encoding = "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            if line:
                yield json.loads(line)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="stream_load_into_kafka.py",
        description="Stream demo climate_data into Kafka, split by latitude into "
                    "Avro (north) / JSON (central) / Protobuf (south) bands.",
    )
    p.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    )
    p.add_argument(
        "--schema-registry-url",
        default=os.environ.get("SCHEMA_REGISTRY_URL", "http://localhost:8081"),
    )
    p.add_argument("--climate-rate", type=float, default=0.0,
                   help="Max records/sec (0 = unlimited).")
    p.add_argument("--climate-limit", type=int, default=0,
                   help="Cap total records (0 = all).")
    p.add_argument("--south-max-lat", type=float, default=DEFAULT_SOUTH_MAX_LAT,
                   help="Latitudes below this go to the southern band.")
    p.add_argument("--north-min-lat", type=float, default=DEFAULT_NORTH_MIN_LAT,
                   help="Latitudes at or above this go to the northern band.")
    p.add_argument("--topic-prefix", default="")
    p.add_argument("--source-url", default=DEFAULT_SOURCE_URL)

    args = p.parse_args(argv)
    if args.climate_rate < 0:
        p.error("--climate-rate must be >= 0")
    if args.climate_limit < 0:
        p.error("--climate-limit must be >= 0")
    if args.south_max_lat > args.north_min_lat:
        p.error("--south-max-lat must be <= --north-min-lat")
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    from confluent_kafka.schema_registry import SchemaRegistryClient

    sr_client = SchemaRegistryClient({"url": args.schema_registry_url})
    key_ser = serializers.key_serializer()

    # Pre-build one value serializer + topic name per band (binds each schema once).
    topics = {b.name: f"{args.topic_prefix}{b.topic}" for b in BANDS}
    value_sers = {
        b.name: serializers.build_value_serializer(b.fmt, topics[b.name], sr_client)
        for b in BANDS
    }

    print(
        f"Loading climate_data into Kafka at {args.bootstrap_servers} "
        f"(registry {args.schema_registry_url})\n"
        f"  bands: "
        + ", ".join(f"{b.name}={topics[b.name]} [{b.fmt}]" for b in BANDS)
        + f"\n  cuts:  lat < {args.south_max_lat} south, "
          f"{args.south_max_lat} <= central < {args.north_min_lat}, "
          f"north >= {args.north_min_lat}"
    )

    counts = {b.name: 0 for b in BANDS}
    limiter = RateLimiter(args.climate_rate)
    total = 0
    print(f"  streaming from {args.source_url}")
    try:
        with KafkaSink(args.bootstrap_servers) as sink:
            for rec in iter_ndjson(args.source_url):
                limiter.wait()
                band = band_for_latitude(
                    latitude_of(rec), args.south_max_lat, args.north_min_lat
                )
                sink.send(
                    topics[band.name],
                    key=key_ser(climate_key(rec)),
                    value=value_sers[band.name](rec),
                )
                counts[band.name] += 1
                total += 1
                if total % _PROGRESS_EVERY == 0:
                    print(f"  {total:,} records sent")
                if args.climate_limit and total >= args.climate_limit:
                    break

            print("Flushing...")
            sink.flush()
            errors = sink.error_count
    except requests.RequestException as exc:
        print(f"ERROR: source download failed: {exc}", file=sys.stderr)
        return EXIT_DOWNLOAD

    print(
        "Done. "
        + ", ".join(f"{name}={n:,}" for name, n in counts.items())
        + f", total={total:,}, delivery_errors={errors}"
    )
    return EXIT_DELIVERY if errors else 0


if __name__ == "__main__":
    sys.exit(main())
