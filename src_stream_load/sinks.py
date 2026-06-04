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
Streaming sinks for the demo-data loader.

A ``StreamSink`` is the only thing that knows about the destination streaming
platform. It receives *already-serialized* bytes (the serializer layer in
``serializers.py`` owns the JSON/Avro/Protobuf encoding) and is responsible
purely for transport. To target a different platform than Kafka in the future
— Pulsar, Kinesis, Redpanda, etc. — implement this interface and swap it in;
nothing in the loader's read/serialize loop needs to change.
"""

from abc import ABC, abstractmethod


class StreamSink(ABC):
    """Transport-only interface: ship pre-serialized records to a destination."""

    @abstractmethod
    def send(self, topic: str, key: bytes | None, value: bytes) -> None:
        """Enqueue one record (already serialized) onto ``topic``."""

    @abstractmethod
    def flush(self) -> None:
        """Block until all enqueued records have been delivered."""

    @abstractmethod
    def close(self) -> None:
        """Flush and release any underlying resources."""

    @property
    @abstractmethod
    def error_count(self) -> int:
        """Number of records that failed to deliver."""

    def __enter__(self) -> "StreamSink":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class KafkaSink(StreamSink):
    """A ``StreamSink`` backed by a ``confluent_kafka.Producer``.

    ``produce()`` is asynchronous and buffers locally, so we poll the producer
    after each send to service delivery callbacks and apply back-pressure when
    the local queue fills up.
    """

    # How often (in records) to poll the producer for delivery callbacks.
    _POLL_EVERY = 10_000

    def __init__(self, bootstrap_servers: str, extra_config: dict | None = None):
        # Imported lazily so that `--help` and unit tests don't require librdkafka.
        from confluent_kafka import Producer

        config = {
            "bootstrap.servers": bootstrap_servers,
            # Favour throughput for a bulk load while keeping delivery guarantees.
            "linger.ms": 50,
            "compression.type": "lz4",
            "acks": "all",
            "enable.idempotence": True,
        }
        if extra_config:
            config.update(extra_config)

        self._producer = Producer(config)
        self._errors = 0
        self._since_poll = 0

    def _on_delivery(self, err, _msg) -> None:
        if err is not None:
            self._errors += 1

    def send(self, topic: str, key: bytes | None, value: bytes) -> None:
        # Retry once on a full local queue: poll to drain, then try again.
        try:
            self._producer.produce(topic, key=key, value=value, on_delivery=self._on_delivery)
        except BufferError:
            self._producer.poll(1.0)
            self._producer.produce(topic, key=key, value=value, on_delivery=self._on_delivery)

        self._since_poll += 1
        if self._since_poll >= self._POLL_EVERY:
            self._producer.poll(0)
            self._since_poll = 0

    def flush(self) -> None:
        self._producer.flush()

    def close(self) -> None:
        self.flush()

    @property
    def error_count(self) -> int:
        return self._errors
