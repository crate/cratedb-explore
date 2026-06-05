# src_stream_load — Stream climate data through Kafka, split by latitude

This example uses the existing tables we loaded earlier. Instead of 
using [COPY FROM](https://cratedb.com/docs/crate/reference/en/latest/sql/statements/copy-from.html) and 
an S3 file, we use [Kafka Connect](https://kafka.apache.org/documentation/#connect) to encode the 
data into three different formats, and then load the weather data table again,
by reading from three different Kafka topics, each in a different format. 

## Setup
Before you start you'll need to delete your existing climate data:

```sql
DELETE FROM demo.climate_data;
```
You'll also need access to servers running Kafka (usually port 9092) and 
Confluent's Kafka Schema Registry, (port 8081). The [schema registry](https://docs.confluent.io/platform/current/schema-registry/index.html) is needed if we
want to use [Avro](https://avro.apache.org/), or [ProtoBuf](https://protobuf.dev/).

## The code
Two Python programs move the demo `climate_data` dataset through Kafka and into
CrateDB. The twist: the single dataset is arbitrarily **split by latitude into three bands**,
and each band travels in a **different wire format** — so one run exercises JSON,
Avro, and Protobuf side by side.

- **`stream_load_into_kafka.py`** — the producer. Reads `climate_data` from S3,
  classifies each record by latitude, and writes it to that band's topic in that
  band's format.
- **`stream_from_kafka_into_crate.py`** — the consumer. Reads all three band
  topics back out of Kafka, deserializes each in its own format, and bulk-loads
  every record into the one **`demo.climate_data`** table over CrateDB's HTTP
  `_sql` endpoint.

## The Latitude Bands

`climate_data` records carry `geo_location = [longitude, latitude]`. The producer
routes each record by its **latitude** into one of three bands:

| Band | Latitude | Format | Topic | Schema Registry |
| --- | --- | --- | --- | --- |
| northern | `lat ≥ --north-min-lat` (default 52.0) | **Avro** | `climate_data_north` | required |
| central | between the two cuts | **JSON** | `climate_data_central` | not used |
| southern | `lat < --south-max-lat` (default 50.0) | **Protobuf** | `climate_data_south` | required |

Germany spans roughly 47.5°N–54.75°N, so the default cuts at **50.0** and **52.0**
divide it into near-thirds (a 3,000-record sample splits ≈ 831 / 1,014 / 1,155
south / central / north). Move the boundaries with `--south-max-lat` /
`--north-min-lat`.

## The Data

The source is newline-delimited JSON (one object per line):

| Source file | Records | Role |
| --- | --- | --- |
| `export-demo_climate_data_large_v2.json` | ~265k | per-location climate measurements (temperature in **Kelvin**) |

The default points at the public S3 bucket
(`https://guided-path.s3.us-east-1.amazonaws.com/…`); override it with
`--source-url`.

## Rate limiting

The whole stream honours `--climate-rate` (records/sec) and `--climate-limit` (a
record cap, handy for smoke tests). Both apply across all three bands combined.

## Encodings and the Schema Registry

| Band format | Wire bytes | Schema Registry |
| --- | --- | --- |
| `json` (central) | UTF-8 `json.dumps` of the raw record | not used |
| `avro` (north) | Confluent Avro | required |
| `protobuf` (south) | Confluent Protobuf | required |

Because two of the three bands are binary, **a Confluent Schema Registry is
always required** — the Avro and Protobuf bands auto-register their schemas with
it on first use. Schemas live in `schemas/` (`climate_data.avsc`,
`climate_data.proto`).

Message **keys** are plain UTF-8 strings: `"lon,lat"` (the record's location).
Many measurement_times share a location, so the key is only a *subset* of the
row's identity — it co-locates a station's readings on one partition but is not
unique (see [Re-running the loader](#re-running-the-loader-no-broker-side-dedup)).

## Re-running the loader (no broker-side dedup)

Producing to Kafka is **append-only** — unlike the CrateDB table (which dedups on
its primary key, see the [top-level README](../README.md#loading-the-data-with-copy-from)),
re-running the producer writes another full copy of every record. The broker
does not reject duplicates.

The band topics are keyed by **location only** (`"lon,lat"`), not by the full
`(measurement_time, location)` identity, because `climate_data` is an event
stream. **Don't enable log compaction on them** — that would collapse each
location's series to a single reading. Real uniqueness is enforced downstream by
the `climate_data` primary key in CrateDB, not by Kafka.

So: re-running is safe (it never corrupts anything) but additive. To start clean,
delete/recreate the topics, or `DELETE`/`DROP` and recreate `demo.climate_data`.

## Targeting a different streaming platform

The destination is isolated behind the `StreamSink` interface (that we wrote) in
`sinks.py` (`send` / `flush` / `close`), which only ever sees pre-serialized
bytes. `KafkaSink` is the one implementation today; to target Pulsar, Kinesis,
etc., implement `StreamSink` and construct it in `stream_load_into_kafka.py` —
the read, route, serialize, and rate-limit loop is unchanged.

## Install and run

```bash
cd src_stream_load
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Smoke test against a local broker + registry with a small cap:
python stream_load_into_kafka.py \
    --bootstrap-servers localhost:9092 \
    --schema-registry-url http://localhost:8081 \
    --climate-limit 3000 --climate-rate 500
```

A local Kafka + Schema Registry (e.g. the Confluent `cp-all-in-one`
docker-compose) is the simplest way to exercise all three bands.

### CLI reference

| Option | Default | Notes |
| --- | --- | --- |
| `--bootstrap-servers HOST:PORT` | `localhost:9092` | env `KAFKA_BOOTSTRAP_SERVERS` |
| `--schema-registry-url URL` | `http://localhost:8081` | env `SCHEMA_REGISTRY_URL`; used by the Avro + Protobuf bands |
| `--climate-rate N` | `0` (unlimited) | max records/sec across all bands |
| `--climate-limit N` | `0` (all) | cap on total records |
| `--south-max-lat L` | `50.0` | latitudes below `L` go to the southern (Protobuf) band |
| `--north-min-lat L` | `52.0` | latitudes `≥ L` go to the northern (Avro) band |
| `--topic-prefix PREFIX` | none | prepended to every band topic name |
| `--source-url URL` | S3 URL | override the climate_data source |

Exit codes: `0` success · `1` bad argument · `2` source download failed ·
`3` one or more records failed to deliver.

## Loading Kafka into CrateDB

`stream_from_kafka_into_crate.py` is the read side: it consumes all three band
topics — each in its own format — and bulk-inserts every record into the single
`demo.climate_data` table over CrateDB's HTTP `_sql` endpoint. It mirrors the
producer's `--bootstrap-servers`, `--schema-registry-url`, and `--topic-prefix`.

```bash
cd src_stream_load && source .venv/bin/activate

# Read the three bands into CrateDB:
python stream_from_kafka_into_crate.py \
    --bootstrap-servers localhost:9092 \
    --schema-registry-url http://localhost:8081 \
    --cratedb-url http://localhost:4200

# Same, but tail the topics for new readings (Ctrl-C to stop):
python stream_from_kafka_into_crate.py \
    --bootstrap-servers localhost:9092 \
    --schema-registry-url http://localhost:8081 \
    --cratedb-url http://localhost:4200 --follow
```

Behaviour worth knowing:

- **It creates the table if it's missing.** On startup it checks
  `information_schema` and, if `demo.climate_data` is absent, runs the matching
  statement from `../sql/german_weather_data_ddl.sql`, printing the DDL as it
  goes. `climate_data`'s `latitude`/`longitude` are GENERATED from
  `geo_location`, so the loader inserts only `measurement_time`, `geo_location`,
  and `data` and lets CrateDB compute the rest.
- **All three bands are read together.** They are assigned to one consumer and
  decoded per-topic, so their rows interleave into one bulk-insert buffer.
- **`--follow` tails the topics.** Without it the consumer stops once it has
  drained every record currently in the three topics. With it, it keeps
  consuming and inserting new readings until you Ctrl-C.
- **Re-runs are idempotent.** Inserts are plain `INSERT`s; CrateDB's primary key
  rejects duplicates per-row (reported as *skipped*, not *inserted*), so
  re-reading a topic never doubles the data. Offsets are committed after each
  batch, so the consumer also resumes from where it left off.
- **Credentials come from the environment.** Set `CRATE_USER` / `CRATE_PASSWORD`
  (HTTP basic auth); the endpoint itself is `--cratedb-url` (env `CRATEDB_URL`,
  default `http://localhost:4200`). Exit codes: `0` success · `1` bad argument ·
  `2` CrateDB unreachable or rejected a statement.

This is the Kafka-fed counterpart to loading CrateDB straight from S3 with
[`COPY FROM`](../README.md#loading-the-data-with-copy-from).

## Consuming a band topic yourself

### First create your table

Before you start you'll need `demo.climate_data` to exist in CrateDB. See
[german_weather_data_ddl.sql](../sql/german_weather_data_ddl.sql). Note there is
also '[german_weather_data_dynamic_ddl.sql](../sql/german_weather_data_dynamic_ddl.sql)',
which creates columns from weather data as and when they are first seen. So while
the '[official](../sql/german_weather_data_ddl.sql)' DDL file has:

```sql
data OBJECT(DYNAMIC) AS (
      temperature DOUBLE PRECISION,
      pressure DOUBLE PRECISION,
      u10 DOUBLE PRECISION,
      v10 DOUBLE PRECISION,
      latitude DOUBLE PRECISION,
      longitude DOUBLE PRECISION
   ),
```
'[german_weather_data_dynamic_ddl.sql](../sql/german_weather_data_dynamic_ddl.sql)' has:

```sql
data OBJECT(DYNAMIC),
```
'DYNAMIC' means that as previously unknown columns are encountered, CrateDB [adds them to the table](https://cratedb.com/blog/handling-dynamic-objects-in-cratedb).
This means if I am trying to load a 130 column table I don't need to add all 130
columns to the DDL by hand - I can name the important ones, and use DYNAMIC to
find the rest. Once the data is loaded I can see the actual schema with:

```sql
SHOW CREATE TABLE demo.climate_data;
```
Note: If you have already worked with this data set, you may already have a
CrateDB instance with a populated table. You'll need to either drop or delete
from `demo.climate_data` if you want to load the data again.

### Loading the data

If all you want is the topics loaded into CrateDB, use the ready-made consumer —
[`stream_from_kafka_into_crate.py`](stream_from_kafka_into_crate.py) — described
under [Loading Kafka into CrateDB](#loading-kafka-into-cratedb) above. It already
decodes every band, creates the table, and bulk-inserts the rows.

The rest of this section is for when you want to read a band into your *own*
application instead of into CrateDB — for that, see how
[`stream_from_kafka_into_crate.py`](stream_from_kafka_into_crate.py) decodes each
format and adapt the snippets below.

Message keys are UTF-8 strings in every format; only the value decoding differs
per band. Run the snippets from `src_stream_load/` so the generated
`schemas/climate_data_pb2.py` is importable.

**Central band — JSON** — no Schema Registry; the value is just UTF-8 JSON:

```python
import json
from confluent_kafka import Consumer

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["climate_data_central"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    key = msg.key().decode("utf-8") if msg.key() else None
    value = json.loads(msg.value())
    print(key, value["data"]["temperature"])   # Kelvin
```

**Northern band — Avro** — the `AvroDeserializer` fetches the writer schema from
the registry and returns a `dict`:

```python
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

sr = SchemaRegistryClient({"url": "http://localhost:8081"})
deserialize = AvroDeserializer(sr)

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["climate_data_north"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    ctx = SerializationContext(msg.topic(), MessageField.VALUE)
    record = deserialize(msg.value(), ctx)     # -> dict
    print(msg.key().decode("utf-8"), record["data"]["temperature"])
```

**Southern band — Protobuf** — give the `ProtobufDeserializer` the generated
message class; it returns a protobuf object:

```python
from confluent_kafka import Consumer
from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from schemas.climate_data_pb2 import ClimateData

deserialize = ProtobufDeserializer(ClimateData, {"use.deprecated.format": False})

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["climate_data_south"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    ctx = SerializationContext(msg.topic(), MessageField.VALUE)
    record = deserialize(msg.value(), ctx)     # -> ClimateData message
    print(msg.key().decode("utf-8"), record.data.temperature)
```

## Regenerating the Protobuf classes

`schemas/climate_data_pb2.py` is generated from `schemas/climate_data.proto` and
committed. After editing the `.proto`, regenerate it:

```bash
pip install grpcio-tools
python -m grpc_tools.protoc -Ischemas --python_out=schemas \
    schemas/climate_data.proto
```
