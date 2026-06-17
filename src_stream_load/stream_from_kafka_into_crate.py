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
Consume the three climate_data latitude bands from Kafka into CrateDB.

This is the read side of ``stream_load_into_kafka.py``. It consumes all three
band topics — each in its own wire format —

    climate_data_north    Avro
    climate_data_central  JSON
    climate_data_south    Protobuf

and bulk-inserts every record into the single ``demo.climate_data`` table over
CrateDB's HTTP ``_sql`` endpoint. Avro and Protobuf are decoded via a Confluent
Schema Registry, so one is always required.

The consumer drains whatever is currently in the band topics and stops. With
``--follow`` it keeps running and inserts new readings as they arrive (Ctrl-C to
stop). If ``demo.climate_data`` is missing it is created first from
``sql/german_weather_data_ddl.sql``, and the DDL is printed as it runs.

CrateDB credentials are read from the CRATEDB_USER / CRATEDB_PASSWORD environment
variables (never the command line), matching the other tools in this repo.

Usage:
    python stream_from_kafka_into_crate.py [options]

    --bootstrap-servers HOST:PORT   Kafka brokers (default: localhost:9092;
                                    env KAFKA_BOOTSTRAP_SERVERS).
    --schema-registry-url URL       Confluent Schema Registry, used by the Avro
                                    and Protobuf bands (default:
                                    http://localhost:8081; env SCHEMA_REGISTRY_URL).
    --topic-prefix PREFIX           Prepended to every topic name (default: none).
    --cratedb-url URL               CrateDB HTTP endpoint (default:
                                    http://localhost:4200; env CRATEDB_URL).
    --group-id ID                   Kafka consumer group (default: crate-loader).
    --batch-size N                  Rows per CrateDB bulk insert (default: 1000).
    --climate-limit N               Cap total records (0 = all; default: 0).
    --follow                        After draining the bands, keep consuming and
                                    inserting new readings until interrupted.
    --ddl-file PATH                 DDL used to create climate_data if missing
                                    (default: ../sql/german_weather_data_ddl.sql).

Examples:
    python stream_from_kafka_into_crate.py --cratedb-url http://localhost:4200
    python stream_from_kafka_into_crate.py --bootstrap-servers broker:9092 \
        --schema-registry-url http://registry:8081 --follow

Exit codes:
    0 — success
    1 — bad command-line argument
    2 — CrateDB was unreachable or rejected a statement
"""

import argparse
import os
import re
import sys
from pathlib import Path

import requests

import serializers
from serializers import BANDS

DEFAULT_DDL = Path(__file__).resolve().parent.parent / "sql" / "german_weather_data_ddl.sql"

CLIMATE_TABLE = "climate_data"

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


def ensure_climate_table(crate: CrateClient, ddl_path: Path) -> None:
    """Create demo.climate_data from the DDL file if it's missing, printing what it runs."""
    if CLIMATE_TABLE in crate.table_names():
        print(f"Table demo.{CLIMATE_TABLE} already exists.")
        return

    print(f"Table demo.{CLIMATE_TABLE} missing. Creating from {ddl_path.name}:\n")
    for table, stmt in _parse_ddl(ddl_path):
        if table == CLIMATE_TABLE:
            print(stmt + ";\n")
            crate.execute(stmt)
    print("Created.\n")


# --- climate_data INSERTs -----------------------------------------------------
#
# climate_data's latitude/longitude are GENERATED from geo_location, so we must
# NOT insert them — CrateDB computes them. We insert only the other three columns.

_INSERT_STMT = (
    "INSERT INTO demo.climate_data "
    "(measurement_time, geo_location, data) VALUES (?, ?, ?)"
)


def _to_row(rec: dict) -> list:
    return [rec["measurement_time"], rec["geo_location"], rec["data"]]


class TableLoader:
    """Buffers decoded records and bulk-inserts them into demo.climate_data."""

    def __init__(self, crate: CrateClient, batch_size: int):
        self._crate = crate
        self._batch_size = batch_size
        self._rows: list = []
        self.received = 0
        self.inserted = 0
        self.skipped = 0

    def add(self, rec: dict) -> bool:
        """Buffer one record. Returns True if a batch was flushed."""
        self._rows.append(_to_row(rec))
        self.received += 1
        if len(self._rows) >= self._batch_size:
            self.flush()
            return True
        return False

    def flush(self) -> None:
        if not self._rows:
            return
        res = self._crate.execute(_INSERT_STMT, bulk_args=self._rows)
        for r in res.get("results", []):
            if r.get("rowcount", -1) == _ROWCOUNT_INSERTED:
                self.inserted += 1
            else:
                self.skipped += 1
        self._rows = []


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


def consume_bands(consumer, crate, topic_formats, sr_client, batch_size,
                  follow=False, limit=0) -> dict:
    """Drain every band topic into demo.climate_data; optionally follow for more.

    ``topic_formats`` is a list of ``(topic, fmt)``. All bands are assigned to a
    single consumer and decoded per-topic, so their records interleave into one
    shared bulk-insert buffer. Offsets are committed after each flush, so a crash
    re-reads at most one batch — duplicate rows are absorbed by the table's
    primary key. With ``follow`` the consumer does not stop when the topics are
    drained but keeps consuming new records.
    """
    from confluent_kafka import TopicPartition

    decoders = {}        # topic -> bytes->dict
    assignment = []      # TopicPartition list, each with a start offset
    ends: dict = {}      # (topic, partition) -> high watermark
    pending = set()      # (topic, partition) still behind the high watermark

    for topic, fmt in topic_formats:
        md = consumer.list_topics(topic, timeout=10)
        tmd = md.topics.get(topic)
        if tmd is None or tmd.error is not None or not tmd.partitions:
            print(f"  {topic}: topic not found / empty, skipping.")
            continue
        decoders[topic] = serializers.build_value_deserializer(fmt, topic, sr_client)
        parts = [TopicPartition(topic, p) for p in tmd.partitions]
        committed = consumer.committed(parts, timeout=10)
        for tp, com in zip(parts, committed):
            low, high = consumer.get_watermark_offsets(tp, timeout=10, cached=False)
            start = com.offset if com.offset is not None and com.offset >= 0 else low
            tp.offset = start
            assignment.append(tp)
            ends[(topic, tp.partition)] = high
            if start < high:
                pending.add((topic, tp.partition))

    if not assignment:
        print("  no band topics found; nothing to consume.")
        return _stats(TableLoader(crate, batch_size))

    consumer.assign(assignment)
    loader = TableLoader(crate, batch_size)

    backlog = sum(ends[(tp.topic, tp.partition)] - tp.offset for tp in assignment)
    print(
        f"  {len(decoders)} band topic(s) -> demo.climate_data: "
        f"{backlog:,} record(s) to read"
        + (" (then following for new data, Ctrl-C to stop)" if follow else "")
    )

    try:
        while follow or pending:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                continue
            if loader.add(decoders[msg.topic()](msg.value())):
                _commit(consumer)
            key = (msg.topic(), msg.partition())
            if msg.offset() + 1 >= ends.get(key, 0):
                pending.discard(key)
            if limit and loader.received >= limit:
                break
    except KeyboardInterrupt:
        # Normal way to stop --follow; fall through to flush + commit + report.
        print("\nInterrupted — stopping.")
    finally:
        loader.flush()
        _commit(consumer)

    s = _stats(loader)
    print(
        f"  done: {s['inserted']:,} inserted, {s['skipped']:,} skipped, "
        f"{s['received']:,} read"
    )
    return s


def _stats(loader: TableLoader) -> dict:
    return {"received": loader.received, "inserted": loader.inserted,
            "skipped": loader.skipped}


# --- CLI ----------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="stream_from_kafka_into_crate.py",
        description="Consume the climate_data latitude bands from Kafka into CrateDB.",
    )
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
                   help="Cap total records (0 = all).")
    p.add_argument("--follow", action="store_true",
                   help="Keep consuming new records after the backlog is drained.")
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
    user = os.environ.get("CRATEDB_USER")
    if user:
        auth = (user, os.environ.get("CRATEDB_PASSWORD", ""))

    crate = CrateClient(args.cratedb_url, auth)
    try:
        ensure_climate_table(crate, Path(args.ddl_file))
    except (CrateError, requests.RequestException) as exc:
        print(f"ERROR: CrateDB at {args.cratedb_url}: {exc}", file=sys.stderr)
        return EXIT_CRATE

    from confluent_kafka.schema_registry import SchemaRegistryClient

    sr_client = SchemaRegistryClient({"url": args.schema_registry_url})

    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": args.group_id,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })

    topic_formats = [(f"{args.topic_prefix}{b.topic}", b.fmt) for b in BANDS]

    print(
        f"Consuming climate_data bands from Kafka at {args.bootstrap_servers} "
        f"(registry {args.schema_registry_url}) into CrateDB at {args.cratedb_url}\n"
        "  bands: "
        + ", ".join(f"{t} [{f}]" for t, f in topic_formats)
    )

    try:
        stats = consume_bands(
            consumer, crate, topic_formats, sr_client, args.batch_size,
            follow=args.follow, limit=args.climate_limit,
        )
    except (CrateError, requests.RequestException) as exc:
        print(f"ERROR: CrateDB insert failed: {exc}", file=sys.stderr)
        consumer.close()
        return EXIT_CRATE
    finally:
        consumer.close()

    print(
        f"Done. climate_data={stats['inserted']:,}/{stats['received']:,} "
        f"(skipped {stats['skipped']:,})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
