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
Value encoding for the demo-data loader: JSON, Avro, or Protobuf.

Each of the three datasets maps to one Kafka topic with its own schema, so
serialization is described per-topic by a ``TopicSpec``. The ``StreamSink``
(see ``sinks.py``) only ever handles the bytes this module produces.

Format notes
------------
* ``json``     — plain UTF-8 ``json.dumps`` of the raw record; needs no Schema
                 Registry. ``geo_coords`` stays a native nested GeoJSON object.
* ``avro``     — Confluent Avro; ``geo_coords`` is stored as a GeoJSON *string*
                 (the source mixes Polygon and MultiPolygon, i.e. different
                 array depths, which a single Avro field can't express).
* ``protobuf`` — Confluent Protobuf; same ``geo_coords``-as-string treatment.

Avro and Protobuf auto-register their schemas with the Schema Registry on first
use.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from schemas import climate_data_pb2, geo_point_pb2, german_region_pb2

_SCHEMA_DIR = Path(__file__).parent / "schemas"

FORMATS = ("json", "avro", "protobuf")


# --- per-topic dict / protobuf adapters --------------------------------------
#
# The S3 records are already close to the target shape; only german_regions
# needs its nested geo_coords flattened to a JSON string for the binary formats.

def _region_to_avro(rec: dict) -> dict:
    return {**rec, "geo_coords": json.dumps(rec["geo_coords"], ensure_ascii=False)}


def _geo_point_to_proto(rec: dict):
    return geo_point_pb2.GeoPoint(
        latitude=rec["latitude"],
        longitude=rec["longitude"],
        geo_location=rec["geo_location"],
        nearest_town=rec["nearest_town"],
    )


def _region_to_proto(rec: dict):
    return german_region_pb2.GermanRegion(
        region_name=rec["region_name"],
        geo_coords=json.dumps(rec["geo_coords"], ensure_ascii=False),
        tourism_info=rec["tourism_info"],
        transportation=rec["transportation"],
        economics=rec["economics"],
        introduced_species=rec["introduced_species"],
        embedding=rec["embedding"],
    )


def _climate_to_proto(rec: dict):
    d = rec["data"]
    return climate_data_pb2.ClimateData(
        measurement_time=rec["measurement_time"],
        geo_location=rec["geo_location"],
        data=climate_data_pb2.ClimateData.ClimateMeasurement(
            temperature=d["temperature"],
            pressure=d["pressure"],
            v10=d["v10"],
            u10=d["u10"],
            latitude=d["latitude"],
            longitude=d["longitude"],
        ),
    )


@dataclass(frozen=True)
class TopicSpec:
    """Everything topic-specific: name, message key, and per-format schemas."""

    name: str
    key_of: Callable[[dict], str]
    avsc_file: str
    proto_msg: type
    to_avro: Callable[[dict], dict]
    to_proto: Callable[[dict], object]

    @property
    def avsc(self) -> str:
        return (_SCHEMA_DIR / self.avsc_file).read_text(encoding="utf-8")


GEO_POINTS = TopicSpec(
    name="geo_points",
    # Key on the coordinates (the record's identity, = the CrateDB PK), not on
    # nearest_town, which is a non-unique attribute (10 towns name two stations).
    # Same "lon,lat" form as climate_data, so the two co-partition on location.
    key_of=lambda r: f'{r["geo_location"][0]},{r["geo_location"][1]}',
    avsc_file="geo_point.avsc",
    proto_msg=geo_point_pb2.GeoPoint,
    to_avro=lambda r: r,
    to_proto=_geo_point_to_proto,
)

GERMAN_REGIONS = TopicSpec(
    name="german_regions",
    key_of=lambda r: r["region_name"],
    avsc_file="german_region.avsc",
    proto_msg=german_region_pb2.GermanRegion,
    to_avro=_region_to_avro,
    to_proto=_region_to_proto,
)

CLIMATE_DATA = TopicSpec(
    name="climate_data",
    key_of=lambda r: f'{r["geo_location"][0]},{r["geo_location"][1]}',
    avsc_file="climate_data.avsc",
    proto_msg=climate_data_pb2.ClimateData,
    to_avro=lambda r: r,
    to_proto=_climate_to_proto,
)


def key_serializer() -> Callable[[str], bytes]:
    """Message keys are plain UTF-8 strings; no Schema Registry involved."""
    return lambda key: key.encode("utf-8")


def build_value_serializer(
    fmt: str, spec: TopicSpec, topic: str, sr_client
) -> Callable[[dict], bytes]:
    """Return a ``record dict -> value bytes`` callable for ``fmt`` on ``topic``.

    ``sr_client`` (a ``SchemaRegistryClient``) is required for avro/protobuf and
    ignored for json.
    """
    if fmt == "json":
        return lambda rec: json.dumps(rec, ensure_ascii=False).encode("utf-8")

    # confluent serdes are imported lazily so json mode needs none of them.
    from confluent_kafka.serialization import MessageField, SerializationContext

    ctx = SerializationContext(topic, MessageField.VALUE)

    if fmt == "avro":
        from confluent_kafka.schema_registry.avro import AvroSerializer

        avro_ser = AvroSerializer(sr_client, spec.avsc)
        to_avro = spec.to_avro
        return lambda rec: avro_ser(to_avro(rec), ctx)

    if fmt == "protobuf":
        from confluent_kafka.schema_registry.protobuf import ProtobufSerializer

        proto_ser = ProtobufSerializer(
            spec.proto_msg, sr_client, {"use.deprecated.format": False}
        )
        to_proto = spec.to_proto
        return lambda rec: proto_ser(to_proto(rec), ctx)

    raise ValueError(f"unknown format: {fmt!r} (expected one of {FORMATS})")
