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
Stream the three demo JSON datasets into Kafka.

The two small reference tables (german_regions, geo_points) are streamed first
and in full; the large climate_data fact stream is streamed last and may be
rate-limited. Records are encoded as JSON, Avro, or Protobuf; Avro and Protobuf
use a Confluent Schema Registry. The destination is hidden behind the
``StreamSink`` interface (see sinks.py) so a non-Kafka platform can be dropped
in later.

Usage:
    python stream_load.py [options]

    --format {json,avro,protobuf}   Value encoding (default: json).
    --bootstrap-servers HOST:PORT   Kafka brokers (default: localhost:9092;
                                    env KAFKA_BOOTSTRAP_SERVERS).
    --schema-registry-url URL       Required for avro/protobuf (default:
                                    http://localhost:8081; env SCHEMA_REGISTRY_URL).
    --climate-rate N                Max climate_data records/sec, 0 = unlimited
                                    (default: 0). Does not affect the reference tables.
    --climate-limit N               Cap climate_data records (0 = all; default: 0).
    --topic-prefix PREFIX           Prepended to every topic name (default: none).
    --geo-points-url URL            Override the geo_points source URL.
    --german-regions-url URL        Override the german_regions source URL.
    --climate-data-url URL          Override the climate_data source URL.

Examples:
    python stream_load.py --format json --climate-limit 1000 --climate-rate 200
    python stream_load.py --format avro --bootstrap-servers broker:9092 \
        --schema-registry-url http://registry:8081

Exit codes:
    0 — success
    1 — bad command-line argument
    2 — a source download failed
    3 — one or more records failed to deliver to the sink
"""

import argparse
import json
import os
import sys
import time

import requests

import serializers
from serializers import CLIMATE_DATA, GEO_POINTS, GERMAN_REGIONS
from sinks import KafkaSink

S3_BASE = "https://guided-path.s3.us-east-1.amazonaws.com"
DEFAULT_URLS = {
    GEO_POINTS.name: f"{S3_BASE}/geo_points.json",
    GERMAN_REGIONS.name: f"{S3_BASE}/german_regions.json",
    CLIMATE_DATA.name: f"{S3_BASE}/export-demo_climate_data_large_v2.json",
}

EXIT_BAD_ARG = 1
EXIT_DOWNLOAD = 2
EXIT_DELIVERY = 3

# Report progress on the big stream every this-many records.
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


def load_topic(spec, url, topic, value_serializer, key_serializer, sink,
               rate=0.0, limit=0) -> int:
    """Stream one dataset to its topic. Returns the number of records sent."""
    limiter = RateLimiter(rate)
    sent = 0
    print(f"  {topic}: streaming from {url}")
    for rec in iter_ndjson(url):
        limiter.wait()
        sink.send(
            topic,
            key=key_serializer(spec.key_of(rec)),
            value=value_serializer(rec),
        )
        sent += 1
        if sent % _PROGRESS_EVERY == 0:
            print(f"  {topic}: {sent:,} records sent")
        if limit and sent >= limit:
            break
    print(f"  {topic}: done ({sent:,} records)")
    return sent


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="stream_load.py",
        description="Stream the demo JSON datasets into Kafka (JSON/Avro/Protobuf).",
    )
    p.add_argument("--format", choices=serializers.FORMATS, default="json")
    p.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    )
    p.add_argument(
        "--schema-registry-url",
        default=os.environ.get("SCHEMA_REGISTRY_URL", "http://localhost:8081"),
    )
    p.add_argument("--climate-rate", type=float, default=0.0,
                   help="Max climate_data records/sec (0 = unlimited).")
    p.add_argument("--climate-limit", type=int, default=0,
                   help="Cap climate_data records (0 = all).")
    p.add_argument("--topic-prefix", default="")
    p.add_argument("--geo-points-url", default=DEFAULT_URLS[GEO_POINTS.name])
    p.add_argument("--german-regions-url", default=DEFAULT_URLS[GERMAN_REGIONS.name])
    p.add_argument("--climate-data-url", default=DEFAULT_URLS[CLIMATE_DATA.name])

    args = p.parse_args(argv)
    if args.climate_rate < 0:
        p.error("--climate-rate must be >= 0")
    if args.climate_limit < 0:
        p.error("--climate-limit must be >= 0")
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # The Schema Registry is only needed for the binary encodings.
    sr_client = None
    if args.format != "json":
        from confluent_kafka.schema_registry import SchemaRegistryClient

        sr_client = SchemaRegistryClient({"url": args.schema_registry_url})

    key_ser = serializers.key_serializer()

    def topic_name(spec):
        return f"{args.topic_prefix}{spec.name}"

    # Pre-build a value serializer per topic (binds the schema once).
    value_sers = {
        spec.name: serializers.build_value_serializer(
            args.format, spec, topic_name(spec), sr_client
        )
        for spec in (GEO_POINTS, GERMAN_REGIONS, CLIMATE_DATA)
    }

    print(
        f"Loading demo data into Kafka at {args.bootstrap_servers} "
        f"as {args.format}"
        + (f" (registry {args.schema_registry_url})" if sr_client else "")
    )

    counts = {}
    try:
        with KafkaSink(args.bootstrap_servers) as sink:
            # 1 & 2: reference tables first, in full, at full speed.
            for spec, url in (
                (GERMAN_REGIONS, args.german_regions_url),
                (GEO_POINTS, args.geo_points_url),
            ):
                counts[spec.name] = load_topic(
                    spec, url, topic_name(spec),
                    value_sers[spec.name], key_ser, sink,
                )

            # 3: the large fact stream last, optionally rate-limited / capped.
            counts[CLIMATE_DATA.name] = load_topic(
                CLIMATE_DATA, args.climate_data_url, topic_name(CLIMATE_DATA),
                value_sers[CLIMATE_DATA.name], key_ser, sink,
                rate=args.climate_rate, limit=args.climate_limit,
            )

            print("Flushing...")
            sink.flush()
            errors = sink.error_count
    except requests.RequestException as exc:
        print(f"ERROR: source download failed: {exc}", file=sys.stderr)
        return EXIT_DOWNLOAD

    total = sum(counts.values())
    print(
        "Done. "
        + ", ".join(f"{name}={n:,}" for name, n in counts.items())
        + f", total={total:,}, delivery_errors={errors}"
    )
    return EXIT_DELIVERY if errors else 0


if __name__ == "__main__":
    sys.exit(main())
