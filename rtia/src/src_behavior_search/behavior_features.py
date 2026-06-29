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
behavior_features — turn an rtia device's sensor window into a behaviour vector.

This is the numeric analogue of the text embedding in src_rag: instead of a
language model over `maintenance_log.notes`, a fixed set of summary statistics
over a device's `iot_data` readings becomes a vector you can KNN over to find
devices behaving alike.

Recon facts that shape the design (see README):
  - Each device measures exactly ONE metric (device_type ↔ metric_unit is 1:1),
    so a device vector is the stats of a single series, not a multi-metric concat.
  - Units differ across types (C / mm/s / kW / bar / m3/h), so similarity is only
    meaningful WITHIN a device_type — standardize and KNN per type.
  - `status` (normal/warning/critical) is held out as a validation label, never a
    feature, so we don't leak the answer into the vector.

Connection: CrateDB HTTP `_sql` endpoint (port 4200), schema `rtia` — same
transport as the MCP servers. Reads `CRATEDB_CLUSTER_URL` or `CRATEDB_HOST`/
`CRATEDB_PORT`, with creds from `CRATEDB_USER`/`CRATEDB_PASSWORD`.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

# Value-derived features only (no status, no quality_score — those would leak the
# fault label we validate against). Order is frozen: it defines the vector layout.
FEATURE_NAMES = [
    "mean", "std", "min", "max", "range", "p05", "p50", "p95", "trend_slope",
]


def featurize(values: np.ndarray, times: np.ndarray) -> np.ndarray:
    """One device's single series -> a fixed-length behaviour vector.

    `times` (epoch ms) only orders the series for the trend slope; the slope is
    value-per-window (time normalized to [0, 1]) so it's comparable across
    devices regardless of absolute timestamps or sampling jitter.
    """
    values = np.asarray(values, dtype=float)
    order = np.argsort(times)
    v = values[order]
    t = np.asarray(times, dtype=float)[order]
    span = t[-1] - t[0]
    t01 = (t - t[0]) / span if span > 0 else np.zeros_like(t)
    slope = float(np.polyfit(t01, v, 1)[0]) if span > 0 and len(v) > 1 else 0.0
    return np.array([
        v.mean(),
        v.std(),
        v.min(),
        v.max(),
        v.max() - v.min(),
        np.percentile(v, 5),
        np.percentile(v, 50),
        np.percentile(v, 95),
        slope,
    ], dtype=float)


@dataclass
class Device:
    device_id: str
    device_type: str
    values: np.ndarray
    times: np.ndarray
    n_total: int
    n_critical: int
    n_warning: int

    @property
    def is_faulting(self) -> bool:
        # Validation label, derived from held-out status. Not a feature.
        return self.n_critical > 0

    def vector(self) -> np.ndarray:
        return featurize(self.values, self.times)


# ---- CrateDB HTTP _sql access -------------------------------------------------

def _resolve_endpoint():
    base = os.getenv("CRATEDB_CLUSTER_URL")
    if not base:
        host = os.getenv("CRATEDB_HOST", "localhost")
        port = os.getenv("CRATEDB_PORT_HTTP", os.getenv("CRATEDB_HTTP_PORT", "4200"))
        scheme = os.getenv("CRATEDB_SCHEME", "http")
        base = f"{scheme}://{host}:{port}"
    base = base.rstrip("/")
    if not base.endswith("/_sql"):
        base = base + "/_sql"
    user = os.getenv("CRATEDB_USER", "crate")
    pw = os.getenv("CRATEDB_PASSWORD", "")
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return base, auth


def run_sql(stmt: str, args=None, bulk_args=None, schema: str = "rtia", timeout: int = 120):
    """POST a statement to the _sql endpoint. `args` for a single parameterized
    call, `bulk_args` for a batched insert. Returns (cols, rows)."""
    base, auth = _resolve_endpoint()
    payload = {"stmt": stmt}
    if args is not None:
        payload["args"] = args
    if bulk_args is not None:
        payload["bulk_args"] = bulk_args
    req = urllib.request.Request(base, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Default-Schema", schema)
    req.add_header("Authorization", "Basic " + auth)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d.get("cols", []), d.get("rows", [])


def vec_literal(v) -> str:
    """Render a float vector as a CrateDB array literal, e.g. '[0.1, -0.2, ...]'.

    Inlined into KNN_MATCH / INSERT SQL over the HTTP endpoint, which is less
    fussy about FLOAT_VECTOR than passing the array as a bound parameter."""
    return "[" + ", ".join(f"{float(x):.8f}" for x in v) + "]"


def load_devices() -> dict[str, Device]:
    """Pull every reading once and group into per-device series in memory.

    ~501k rows / 500 devices on the demo cluster — small for numpy. We sort by
    time inside featurize(), so no server-side ORDER BY needed.
    """
    _, rows = run_sql(
        "SELECT tags['device_id'], tags['device_type'], \"timestamp\", "
        "fields['metric_value'], tags['status'] FROM iot_data"
    )
    vals = defaultdict(list)
    times = defaultdict(list)
    types: dict[str, str] = {}
    n_total = defaultdict(int)
    n_crit = defaultdict(int)
    n_warn = defaultdict(int)
    for did, dtype, ts, val, status in rows:
        if val is None:
            continue
        vals[did].append(val)
        times[did].append(ts)
        types[did] = dtype
        n_total[did] += 1
        if status == "critical":
            n_crit[did] += 1
        elif status == "warning":
            n_warn[did] += 1
    return {
        did: Device(did, types[did], np.array(vals[did]), np.array(times[did]),
                    n_total[did], n_crit[did], n_warn[did])
        for did in vals
    }


# ---- fleet matrix + within-type standardization -------------------------------

def build_matrix(devices: dict[str, Device]):
    """-> (ids, types, X) where X[i] is device ids[i]'s raw (un-standardized) vector."""
    ids = list(devices)
    types = np.array([devices[d].device_type for d in ids])
    X = np.vstack([devices[d].vector() for d in ids])
    return ids, types, X


def standardize_within_type(X: np.ndarray, types: np.ndarray):
    """Z-score each feature within each device_type.

    Per-type (not global) so within-type KNN distances are scale-fair and aren't
    dominated by between-type magnitude gaps. Returns the standardized matrix and
    a {type: (mean, std)} scaler so query vectors get the identical transform.
    """
    Z = np.zeros_like(X)
    scalers = {}
    for t in np.unique(types):
        mask = types == t
        mu = X[mask].mean(axis=0)
        sd = X[mask].std(axis=0)
        sd_safe = np.where(sd == 0, 1.0, sd)
        Z[mask] = (X[mask] - mu) / sd_safe
        scalers[t] = (mu, sd_safe)
    return Z, scalers


def knn(query: np.ndarray, X: np.ndarray, k: int):
    """In-memory Euclidean KNN -> (indices, distances), nearest first.

    Used by the validation harness so we can prove the signal before committing
    to a CrateDB FLOAT_VECTOR table + KNN_MATCH.
    """
    d = np.linalg.norm(X - query, axis=1)
    idx = np.argsort(d)[:k]
    return idx, d[idx]
