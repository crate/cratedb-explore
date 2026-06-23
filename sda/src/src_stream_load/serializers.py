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
Latitude bands and value (de)serialization for the climate_data loader.

The single ``climate_data`` dataset is split into three latitude **bands**, and
each band travels in a *different* wire format so one run exercises all three
encodings at once:

* the **northern** band  -> Avro      (topic ``climate_data_north``)
* the **central** band   -> JSON      (topic ``climate_data_central``)
* the **southern** band  -> Protobuf  (topic ``climate_data_south``)

Avro and Protobuf register their schemas with a Confluent Schema Registry; JSON
needs none. The ``StreamSink`` (see ``sinks.py``) only ever handles the bytes
this module produces.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from schemas import climate_data_pb2

_SCHEMA_DIR = Path(__file__).parent / "schemas"

FORMATS = ("json", "avro", "protobuf")


# --- the climate_data record -------------------------------------------------

CLIMATE_AVSC = (_SCHEMA_DIR / "climate_data.avsc").read_text(encoding="utf-8")
CLIMATE_PROTO_MSG = climate_data_pb2.ClimateData


def climate_key(rec: dict) -> str:
    """Message key = ``"lon,lat"`` (location), matching the CrateDB layout.

    climate_data is an event stream — many measurement_times share a location —
    so this is a subset of the row's identity, not a unique key. Don't enable
    log compaction on the band topics (it would collapse each location's series
    to a single reading); uniqueness is enforced downstream by CrateDB's PK.
    """
    return f'{rec["geo_location"][0]},{rec["geo_location"][1]}'


def latitude_of(rec: dict) -> float:
    """Latitude of a record, from ``geo_location`` = ``[longitude, latitude]``."""
    return rec["geo_location"][1]


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


def _proto_to_climate(msg) -> dict:
    """Convert a generated ClimateData message back to a CrateDB-ready dict."""
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


# --- latitude bands ----------------------------------------------------------

@dataclass(frozen=True)
class Band:
    """One latitude band of climate_data, with its own topic and wire format."""

    name: str    # north / central / south
    fmt: str     # avro / json / protobuf  (one of FORMATS)
    topic: str   # base topic name (before any --topic-prefix)


# Germany spans roughly 47.5°N–54.75°N. The three bands below split it into
# (near-)thirds; the cut latitudes are the producer's defaults and can be moved
# on its command line. Each band is pinned to a distinct format on purpose.
NORTH = Band(name="north", fmt="avro", topic="climate_data_north")
CENTRAL = Band(name="central", fmt="json", topic="climate_data_central")
SOUTH = Band(name="south", fmt="protobuf", topic="climate_data_south")
BANDS = (NORTH, CENTRAL, SOUTH)

# Default band boundaries, in degrees latitude (overridable on the producer CLI).
DEFAULT_SOUTH_MAX_LAT = 50.0   # lat <  50.0  -> south
DEFAULT_NORTH_MIN_LAT = 52.0   # lat >= 52.0  -> north;  in between -> central


def band_for_latitude(lat: float, south_max: float, north_min: float) -> Band:
    """Classify a latitude into its band (south < ``south_max`` <= central < ``north_min`` <= north)."""
    if lat < south_max:
        return SOUTH
    if lat >= north_min:
        return NORTH
    return CENTRAL


# --- (de)serializer factories ------------------------------------------------

def key_serializer() -> Callable[[str], bytes]:
    """Message keys are plain UTF-8 strings; no Schema Registry involved."""
    return lambda key: key.encode("utf-8")


def build_value_serializer(fmt: str, topic: str, sr_client) -> Callable[[dict], bytes]:
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

        avro_ser = AvroSerializer(sr_client, CLIMATE_AVSC)
        return lambda rec: avro_ser(rec, ctx)

    if fmt == "protobuf":
        from confluent_kafka.schema_registry.protobuf import ProtobufSerializer

        proto_ser = ProtobufSerializer(
            CLIMATE_PROTO_MSG, sr_client, {"use.deprecated.format": False}
        )
        return lambda rec: proto_ser(_climate_to_proto(rec), ctx)

    raise ValueError(f"unknown format: {fmt!r} (expected one of {FORMATS})")


def build_value_deserializer(fmt: str, topic: str, sr_client) -> Callable[[bytes], dict]:
    """Return a ``value bytes -> CrateDB-ready dict`` callable for ``fmt`` (reverse of above)."""
    if fmt == "json":
        return json.loads

    from confluent_kafka.serialization import MessageField, SerializationContext

    ctx = SerializationContext(topic, MessageField.VALUE)

    if fmt == "avro":
        from confluent_kafka.schema_registry.avro import AvroDeserializer

        de = AvroDeserializer(sr_client)
        return lambda b: de(b, ctx)  # already a dict

    if fmt == "protobuf":
        from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer

        de = ProtobufDeserializer(CLIMATE_PROTO_MSG, {"use.deprecated.format": False})
        return lambda b: _proto_to_climate(de(b, ctx))

    raise ValueError(f"unknown format: {fmt!r} (expected one of {FORMATS})")
