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
Consume the three demo datasets back out of Kafka and load them into CrateDB.

This is the read side of ``stream_load_into_kafka.py`` and mirrors its CLI:
the same ``--format`` (JSON / Avro / Protobuf), ``--bootstrap-servers``,
``--schema-registry-url``, and ``--topic-prefix`` select what to read. Records
are deserialized (Avro/Protobuf via the Schema Registry) and bulk-inserted into
``demo.geo_points``, ``demo.german_regions``, and ``demo.climate_data`` over
CrateDB's HTTP ``_sql`` endpoint.

Like the producer, the two small reference tables (german_regions, geo_points)
are drained first and in full; the large climate_data fact stream is drained
last. With ``--follow`` the consumer does not stop at the end of climate_data —
it keeps running and inserts new readings as they arrive (Ctrl-C to stop).

If any of the three demo tables is missing it is created first from
``sql/german_weather_data_ddl.sql``, and the DDL is printed as it runs.

CrateDB credentials are read from the CRATE_USER / CRATE_PASSWORD environment
variables (never the command line), matching the other tools in this repo.

Usage:
    python stream_from_kafka_into_crate.py [options]

    --format {json,avro,protobuf}   Value encoding (default: json).
    --bootstrap-servers HOST:PORT   Kafka brokers (default: localhost:9092;
                                    env KAFKA_BOOTSTRAP_SERVERS).
    --schema-registry-url URL       Required for avro/protobuf (default:
                                    http://localhost:8081; env SCHEMA_REGISTRY_URL).
    --topic-prefix PREFIX           Prepended to every topic name (default: none).
    --cratedb-url URL               CrateDB HTTP endpoint (default:
                                    http://localhost:4200; env CRATEDB_URL).
    --group-id ID                   Kafka consumer group (default: crate-loader).
    --batch-size N                  Rows per CrateDB bulk insert (default: 1000).
    --climate-limit N               Cap climate_data records (0 = all; default: 0).
    --follow                        After draining climate_data, keep consuming
                                    and inserting new readings until interrupted.
    --ddl-file PATH                 DDL used to create missing tables (default:
                                    ../sql/german_weather_data_ddl.sql).

Examples:
    python stream_from_kafka_into_crate.py --format json --cratedb-url http://localhost:4200
    python stream_from_kafka_into_crate.py --format avro --topic-prefix avro_ \
        --bootstrap-servers broker:9092 --schema-registry-url http://registry:8081 --follow

Exit codes:
    0 — success
    1 — bad command-line argument
    2 — CrateDB was unreachable or rejected a statement
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

from serializers import CLIMATE_DATA, GEO_POINTS, GERMAN_REGIONS

DEFAULT_DDL = Path(__file__).resolve().parent.parent / "sql" / "german_weather_data_ddl.sql"

EXIT_BAD_ARG = 1
EXIT_CRATE = 2

# CrateDB's bulk response reports rowcount 1 for an inserted row; anything else
# (notably -2 for a duplicate-key conflict) is counted as skipped.
_ROWCOUNT_INSERTED = 1


# --- CrateDB HTTP client ------------------------------------------------------

class CrateError(RuntimeError):
    """A non-2xx response from the CrateDB ``_sql`` endpoint."""


class CrateClient:
    """Minimal client for CrateDB's HTTP ``_sql`` endpoint (port 4200).

    Sends a ``Default-Schema: demo`` header so unqualified names resolve to the
    demo schema; statements here fully-qualify anyway for clarity.
    """

    def __init__(self, url: str, auth: tuple | None = None):
        self._url = url.rstrip("/") + "/_sql"
        self._auth = auth
        self._session = requests.Session()
        self._session.headers["Default-Schema"] = "demo"

    def execute(self, stmt: str, args=None, bulk_args=None) -> dict:
        payload: dict = {"stmt": stmt}
        if args is not None:
            payload["args"] = args
        if bulk_args is not None:
            payload["bulk_args"] = bulk_args
        resp = self._session.post(self._url, json=payload, auth=self._auth, timeout=120)
        if resp.status_code >= 400:
            raise CrateError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    def table_names(self, schema: str = "demo") -> set:
        res = self.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            args=[schema],
        )
        return {row[0] for row in res.get("rows", [])}


# --- table bootstrap ----------------------------------------------------------

_TABLE_RE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+demo\.(\w+)", re.IGNORECASE)


def _parse_ddl(path: Path):
    """Split the DDL file into ``(table_name, statement)`` pairs."""
    text = path.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    pairs = []
    for chunk in body.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _TABLE_RE.search(chunk)
        pairs.append((m.group(1) if m else None, chunk))
    return pairs


def ensure_tables(crate: CrateClient, ddl_path: Path) -> None:
    """Create any missing demo table from the DDL file, printing what it runs."""
    needed = [CLIMATE_DATA.name, GERMAN_REGIONS.name, GEO_POINTS.name]
    existing = crate.table_names()
    missing = [t for t in needed if t not in existing]
    if not missing:
        print(f"All demo tables already exist: {', '.join(sorted(needed))}.")
        return

    print(f"Missing table(s): {', '.join(missing)}. Creating from {ddl_path.name}:\n")
    for table, stmt in _parse_ddl(ddl_path):
        if table in missing:
            print(stmt + ";\n")
            crate.execute(stmt)
    print("Created.\n")


# --- per-topic INSERTs --------------------------------------------------------
#
# geo_points stores latitude/longitude as plain PK columns, so we insert them.
# climate_data's latitude/longitude are GENERATED from geo_location, so we must
# NOT insert them — CrateDB computes them.

INSERTS = {
    GEO_POINTS.name: (
        "INSERT INTO demo.geo_points "
        "(latitude, longitude, geo_location, nearest_town) VALUES (?, ?, ?, ?)",
        lambda r: [r["latitude"], r["longitude"], r["geo_location"], r["nearest_town"]],
    ),
    GERMAN_REGIONS.name: (
        "INSERT INTO demo.german_regions "
        "(region_name, geo_coords, tourism_info, transportation, economics, "
        "introduced_species, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
        lambda r: [
            r["region_name"], r["geo_coords"], r["tourism_info"], r["transportation"],
            r["economics"], r["introduced_species"], r["embedding"],
        ],
    ),
    CLIMATE_DATA.name: (
        "INSERT INTO demo.climate_data "
        "(measurement_time, geo_location, data) VALUES (?, ?, ?)",
        lambda r: [r["measurement_time"], r["geo_location"], r["data"]],
    ),
}


class TableLoader:
    """Buffers decoded records and bulk-inserts them into one CrateDB table."""

    def __init__(self, crate: CrateClient, name: str, batch_size: int):
        self._crate = crate
        self.name = name
        self._stmt, self._to_row = INSERTS[name]
        self._batch_size = batch_size
        self._rows: list = []
        self.received = 0
        self.inserted = 0
        self.skipped = 0

    def add(self, rec: dict) -> bool:
        """Buffer one record. Returns True if a batch was flushed."""
        self._rows.append(self._to_row(rec))
        self.received += 1
        if len(self._rows) >= self._batch_size:
            self.flush()
            return True
        return False

    def flush(self) -> None:
        if not self._rows:
            return
        res = self._crate.execute(self._stmt, bulk_args=self._rows)
        for r in res.get("results", []):
            if r.get("rowcount", -1) == _ROWCOUNT_INSERTED:
                self.inserted += 1
            else:
                self.skipped += 1
        self._rows = []


# --- value decoding (reverse of serializers.build_value_serializer) -----------

def _proto_to_dict(name: str, msg) -> dict:
    """Convert a generated protobuf message back to a CrateDB-ready dict."""
    if name == GEO_POINTS.name:
        return {
            "latitude": msg.latitude,
            "longitude": msg.longitude,
            "geo_location": list(msg.geo_location),
            "nearest_town": msg.nearest_town,
        }
    if name == GERMAN_REGIONS.name:
        return {
            "region_name": msg.region_name,
            "geo_coords": msg.geo_coords,  # JSON string; normalized below
            "tourism_info": msg.tourism_info,
            "transportation": msg.transportation,
            "economics": msg.economics,
            "introduced_species": msg.introduced_species,
            "embedding": list(msg.embedding),
        }
    if name == CLIMATE_DATA.name:
        d = msg.data
        return {
            "measurement_time": msg.measurement_time,
            "geo_location": list(msg.geo_location),
            "data": {
                "temperature": d.temperature, "pressure": d.pressure,
                "u10": d.u10, "v10": d.v10,
                "latitude": d.latitude, "longitude": d.longitude,
            },
        }
    raise ValueError(f"no protobuf adapter for {name!r}")


def make_decoder(fmt: str, spec, topic: str, sr_client):
    """Return a ``value bytes -> CrateDB-ready dict`` callable for ``fmt``."""
    if fmt == "json":
        raw = json.loads
    else:
        from confluent_kafka.serialization import MessageField, SerializationContext

        ctx = SerializationContext(topic, MessageField.VALUE)
        if fmt == "avro":
            from confluent_kafka.schema_registry.avro import AvroDeserializer

            de = AvroDeserializer(sr_client)
            raw = lambda b: de(b, ctx)  # noqa: E731 — returns a dict
        elif fmt == "protobuf":
            from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer

            de = ProtobufDeserializer(spec.proto_msg, {"use.deprecated.format": False})
            raw = lambda b: _proto_to_dict(spec.name, de(b, ctx))  # noqa: E731
        else:
            raise ValueError(f"unknown format: {fmt!r}")

    # In the binary formats german_regions.geo_coords is a GeoJSON *string*;
    # CrateDB's GEO_SHAPE column wants the geometry object, so parse it back.
    if spec.name == GERMAN_REGIONS.name:
        def decode(b):
            rec = raw(b)
            gc = rec.get("geo_coords")
            if isinstance(gc, str):
                rec["geo_coords"] = json.loads(gc)
            return rec

        return decode
    return raw


# --- consuming ----------------------------------------------------------------

def _commit(consumer) -> None:
    """Synchronously commit consumed offsets, tolerating 'nothing new'.

    librdkafka raises ``_NO_OFFSET`` when ``commit()`` finds no offsets stored
    since the last commit — which happens when the previous in-loop commit
    already covered everything (e.g. a ``--climate-limit`` that lands exactly on
    a batch boundary). That is benign, so we swallow it and re-raise anything else.
    """
    from confluent_kafka import KafkaError, KafkaException

    try:
        consumer.commit(asynchronous=False)
    except KafkaException as exc:
        if exc.args[0].code() != KafkaError._NO_OFFSET:
            raise


def consume_topic(consumer, crate, fmt, spec, topic, sr_client,
                  batch_size, follow=False, limit=0) -> dict:
    """Drain ``topic`` into ``demo.<spec.name>``; optionally follow for more.

    Reads every record currently in the topic (resuming from the consumer
    group's committed offsets) and bulk-inserts it. Offsets are committed after
    each flush, so a crash re-reads at most one batch — duplicate rows are
    absorbed by the tables' primary keys. With ``follow`` it does not stop at the
    current end of the topic but keeps consuming new records.
    """
    from confluent_kafka import TopicPartition

    decode = make_decoder(fmt, spec, topic, sr_client)
    loader = TableLoader(crate, spec.name, batch_size)

    md = consumer.list_topics(topic, timeout=10)
    tmd = md.topics.get(topic)
    if tmd is None or tmd.error is not None or not tmd.partitions:
        print(f"  {topic} -> demo.{spec.name}: topic not found / empty, skipping.")
        return _stats(loader)

    parts = [TopicPartition(topic, p) for p in tmd.partitions]
    committed = consumer.committed(parts, timeout=10)
    ends: dict = {}
    pending = set()
    assignment = []
    for tp, com in zip(parts, committed):
        low, high = consumer.get_watermark_offsets(tp, timeout=10, cached=False)
        start = com.offset if com.offset is not None and com.offset >= 0 else low
        tp.offset = start
        assignment.append(tp)
        ends[tp.partition] = high
        if start < high:
            pending.add(tp.partition)
    consumer.assign(assignment)

    backlog = sum(ends[tp.partition] - tp.offset for tp in assignment)
    print(
        f"  {topic} -> demo.{spec.name}: {backlog:,} record(s) to read"
        + (" (then following for new data, Ctrl-C to stop)" if follow else "")
    )

    try:
        while follow or pending:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                continue
            if loader.add(decode(msg.value())):
                _commit(consumer)
            if msg.offset() + 1 >= ends.get(msg.partition(), 0):
                pending.discard(msg.partition())
            if limit and loader.received >= limit:
                break
    finally:
        loader.flush()
        _commit(consumer)

    s = _stats(loader)
    print(
        f"  {topic} -> demo.{spec.name}: done "
        f"({s['inserted']:,} inserted, {s['skipped']:,} skipped, {s['received']:,} read)"
    )
    return s


def _stats(loader: TableLoader) -> dict:
    return {"received": loader.received, "inserted": loader.inserted,
            "skipped": loader.skipped}


# --- CLI ----------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="stream_from_kafka_into_crate.py",
        description="Consume the demo datasets from Kafka into CrateDB.",
    )
    p.add_argument("--format", choices=("json", "avro", "protobuf"), default="json")
    p.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    )
    p.add_argument(
        "--schema-registry-url",
        default=os.environ.get("SCHEMA_REGISTRY_URL", "http://localhost:8081"),
    )
    p.add_argument("--topic-prefix", default="")
    p.add_argument(
        "--cratedb-url",
        default=os.environ.get("CRATEDB_URL", "http://localhost:4200"),
    )
    p.add_argument("--group-id", default=os.environ.get("KAFKA_GROUP_ID", "crate-loader"))
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--climate-limit", type=int, default=0,
                   help="Cap climate_data records (0 = all).")
    p.add_argument("--follow", action="store_true",
                   help="Keep consuming new climate_data after the backlog is drained.")
    p.add_argument("--ddl-file", default=str(DEFAULT_DDL))

    args = p.parse_args(argv)
    if args.batch_size <= 0:
        p.error("--batch-size must be > 0")
    if args.climate_limit < 0:
        p.error("--climate-limit must be >= 0")
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    auth = None
    user = os.environ.get("CRATE_USER")
    if user:
        auth = (user, os.environ.get("CRATE_PASSWORD", ""))

    crate = CrateClient(args.cratedb_url, auth)
    try:
        ensure_tables(crate, Path(args.ddl_file))
    except (CrateError, requests.RequestException) as exc:
        print(f"ERROR: CrateDB at {args.cratedb_url}: {exc}", file=sys.stderr)
        return EXIT_CRATE

    sr_client = None
    if args.format != "json":
        from confluent_kafka.schema_registry import SchemaRegistryClient

        sr_client = SchemaRegistryClient({"url": args.schema_registry_url})

    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": args.group_id,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })

    def topic_name(spec):
        return f"{args.topic_prefix}{spec.name}"

    print(
        f"Consuming demo data from Kafka at {args.bootstrap_servers} as {args.format} "
        f"into CrateDB at {args.cratedb_url}"
        + (f" (registry {args.schema_registry_url})" if sr_client else "")
    )

    stats = {}
    try:
        # Reference tables first, in full; then the fact stream (optionally followed).
        for spec in (GERMAN_REGIONS, GEO_POINTS):
            stats[spec.name] = consume_topic(
                consumer, crate, args.format, spec, topic_name(spec),
                sr_client, args.batch_size,
            )
        stats[CLIMATE_DATA.name] = consume_topic(
            consumer, crate, args.format, CLIMATE_DATA, topic_name(CLIMATE_DATA),
            sr_client, args.batch_size, follow=args.follow, limit=args.climate_limit,
        )
    except KeyboardInterrupt:
        print("\nInterrupted — stopping.")
    except (CrateError, requests.RequestException) as exc:
        print(f"ERROR: CrateDB insert failed: {exc}", file=sys.stderr)
        consumer.close()
        return EXIT_CRATE
    finally:
        consumer.close()

    inserted = sum(s["inserted"] for s in stats.values())
    print(
        "Done. "
        + ", ".join(
            f"{name}={s['inserted']:,}/{s['received']:,}" for name, s in stats.items()
        )
        + f", total_inserted={inserted:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
