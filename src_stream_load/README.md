    # src_stream_load — stream the demo datasets into Kafka

A Python producer that reads the three demo JSON files from S3 and streams them
into Kafka, encoded as **JSON, Avro, or Protobuf**. Avro and Protobuf register
their schemas with a Confluent **Schema Registry**.

## The data

The three sources are newline-delimited JSON (one object per line):

| Source file | Topic | Records | Role |
| --- | --- | --- | --- |
| `german_regions.json` | `german_regions` | 16 | reference table — 16 German Länder with GeoJSON geometry, descriptive text, and a 1536-d embedding |
| `geo_points.json` | `geo_points` | 726 | reference table — weather-station locations |
| `export-demo_climate_data_large_v2.json` | `climate_data` | ~265k | fact stream — per-location climate measurements (temperature in **Kelvin**) |

Defaults point at the public S3 bucket
(`https://guided-path.s3.us-east-1.amazonaws.com/…`); override per source with
`--geo-points-url` / `--german-regions-url` / `--climate-data-url`.

## Load order and rate limiting

The two small **reference tables are streamed first and in full** at full speed
(`german_regions`, then `geo_points`). The large **`climate_data` fact stream is
streamed last** and is the only stream that honours `--climate-rate`
(records/sec) and `--climate-limit` (a record cap, handy for smoke tests). This
mirrors how the data is used downstream: the dimension tables must be complete
before the facts that reference them start flowing.

## Encodings and the Schema Registry

| `--format` | Wire bytes | Schema Registry |
| --- | --- | --- |
| `json` (default) | UTF-8 `json.dumps` of the raw record | not used |
| `avro` | Confluent Avro | required |
| `protobuf` | Confluent Protobuf | required |

Message **keys** are plain UTF-8 strings (`nearest_town`, `region_name`, and
`"lon,lat"` respectively) for stable partitioning.

> **`geo_coords`:** the German-region geometry is a GeoJSON `Polygon` *or*
> `MultiPolygon` (different array depths), which a single Avro/Protobuf field
> can't express. For `avro`/`protobuf` it is stored as a GeoJSON **string**; for
> `json` it stays a native nested object. Schemas live in `schemas/`.

## Targeting a different streaming platform

The destination is isolated behind the `StreamSink` interface in `sinks.py`
(`send` / `flush` / `close`), which only ever sees pre-serialized bytes.
`KafkaSink` is the one implementation today; to target Pulsar, Kinesis, etc.,
implement `StreamSink` and construct it in `stream_load.py` — the read,
serialize, and rate-limit loop is unchanged.

## Install and run

```bash
cd src_stream_load
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# JSON — no Schema Registry needed; smoke test with a small climate cap:
python stream_load.py --format json --climate-limit 1000 --climate-rate 200

# Avro / Protobuf — against Kafka + a Schema Registry:
python stream_load.py --format avro \
    --bootstrap-servers localhost:9092 \
    --schema-registry-url http://localhost:8081
```

A local Kafka + Schema Registry (e.g. the Confluent `cp-all-in-one`
docker-compose) is the simplest way to exercise the binary formats.

### CLI reference

| Option | Default | Notes |
| --- | --- | --- |
| `--format {json,avro,protobuf}` | `json` | value encoding |
| `--bootstrap-servers HOST:PORT` | `localhost:9092` | env `KAFKA_BOOTSTRAP_SERVERS` |
| `--schema-registry-url URL` | `http://localhost:8081` | env `SCHEMA_REGISTRY_URL`; avro/protobuf only |
| `--climate-rate N` | `0` (unlimited) | max `climate_data` records/sec |
| `--climate-limit N` | `0` (all) | cap on `climate_data` records |
| `--topic-prefix PREFIX` | none | prepended to every topic name |
| `--geo-points-url` / `--german-regions-url` / `--climate-data-url` | S3 URLs | override a source |

Exit codes: `0` success · `1` bad argument · `2` source download failed ·
`3` one or more records failed to deliver.

## Consuming

Message keys are UTF-8 strings in every format; only the value decoding differs.
The snippets below read the `geo_points` topic — swap the topic name for
`german_regions` or `climate_data`. Run them from `src_stream_load/` so the
generated `schemas/*_pb2.py` are importable.

**JSON** — no Schema Registry; the value is just UTF-8 JSON:

```python
import json
from confluent_kafka import Consumer

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["geo_points"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    key = msg.key().decode("utf-8") if msg.key() else None
    value = json.loads(msg.value())
    print(key, value["nearest_town"])
```

**Avro** — the `AvroDeserializer` fetches the writer schema from the registry and
returns a `dict`:

```python
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

sr = SchemaRegistryClient({"url": "http://localhost:8081"})
deserialize = AvroDeserializer(sr)

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["geo_points"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    ctx = SerializationContext(msg.topic(), MessageField.VALUE)
    record = deserialize(msg.value(), ctx)     # -> dict
    print(msg.key().decode("utf-8"), record["nearest_town"])
```

**Protobuf** — give the `ProtobufDeserializer` the matching generated message
class; it returns a protobuf object:

```python
from confluent_kafka import Consumer
from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from schemas.geo_point_pb2 import GeoPoint

deserialize = ProtobufDeserializer(GeoPoint, {"use.deprecated.format": False})

c = Consumer({"bootstrap.servers": "localhost:9092",
              "group.id": "demo-consumer", "auto.offset.reset": "earliest"})
c.subscribe(["geo_points"])
while True:
    msg = c.poll(1.0)
    if msg is None or msg.error():
        continue
    ctx = SerializationContext(msg.topic(), MessageField.VALUE)
    record = deserialize(msg.value(), ctx)     # -> GeoPoint message
    print(msg.key().decode("utf-8"), record.nearest_town)
```

> For `german_regions` in `avro`/`protobuf`, `geo_coords` comes back as a GeoJSON
> **string** — `json.loads(record["geo_coords"])` (avro) or
> `json.loads(record.geo_coords)` (protobuf) to get the geometry object back.

## Regenerating the Protobuf classes

`schemas/*_pb2.py` are generated from `schemas/*.proto` and committed. After
editing a `.proto`, regenerate them:

```bash
pip install grpcio-tools
python -m grpc_tools.protoc -Ischemas --python_out=schemas \
    schemas/geo_point.proto schemas/german_region.proto schemas/climate_data.proto
```
