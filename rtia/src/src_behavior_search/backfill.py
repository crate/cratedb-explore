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
backfill — compute every device's behaviour vector and store it in CrateDB.

Creates rtia.device_behavior (one row per device, so it never fans out when
joined) and upserts each device's WITHIN-TYPE standardized feature vector plus
the held-out fault counts. Vectors are standardized per device_type so that
within-type KNN over the stored vectors is scale-fair — the same transform the
validation harness proved discriminative. Because the standardized vector is
stored for every device, similar-device search just reads a device's row and
KNN_MATCHes it; no scaler is needed at query time.

    python backfill.py        # uses CRATEDB_* env vars (HTTP 4200, schema rtia)
"""

from __future__ import annotations

import sys

import behavior_features as bf

VECTOR_DIM = len(bf.FEATURE_NAMES)

DDL = f"""
CREATE TABLE IF NOT EXISTS rtia.device_behavior (
    device_id       TEXT PRIMARY KEY,
    device_type     TEXT,
    window_start    TIMESTAMP WITH TIME ZONE,
    window_end      TIMESTAMP WITH TIME ZONE,
    n_readings      INTEGER,
    n_critical      INTEGER,
    n_warning       INTEGER,
    behavior_vector FLOAT_VECTOR({VECTOR_DIM})  -- {', '.join(bf.FEATURE_NAMES)}, within-type z-scored
)
"""


def main():
    print("Loading iot_data and featurizing…")
    devices = bf.load_devices()
    ids, types, X = bf.build_matrix(devices)
    Z, _ = bf.standardize_within_type(X, types)
    print(f"  {len(ids)} devices, {VECTOR_DIM}-dim vectors, "
          f"{len(set(types))} types.")

    print("Creating rtia.device_behavior (if absent)…")
    bf.run_sql(DDL)

    print("Upserting behaviour vectors…")
    inserted = 0
    for i, did in enumerate(ids):
        dev = devices[did]
        stmt = (
            "INSERT INTO rtia.device_behavior "
            "(device_id, device_type, window_start, window_end, n_readings, "
            " n_critical, n_warning, behavior_vector) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, {bf.vec_literal(Z[i])}) "
            "ON CONFLICT (device_id) DO UPDATE SET "
            "device_type = excluded.device_type, "
            "window_start = excluded.window_start, window_end = excluded.window_end, "
            "n_readings = excluded.n_readings, n_critical = excluded.n_critical, "
            "n_warning = excluded.n_warning, behavior_vector = excluded.behavior_vector"
        )
        bf.run_sql(stmt, args=[
            dev.device_id, dev.device_type,
            int(dev.times.min()), int(dev.times.max()),
            dev.n_total, dev.n_critical, dev.n_warning,
        ])
        inserted += 1
        if inserted % 100 == 0:
            print(f"  {inserted}/{len(ids)}")

    bf.run_sql("REFRESH TABLE rtia.device_behavior")
    cols, rows = bf.run_sql("SELECT COUNT(*) FROM rtia.device_behavior")
    print(f"Done — rtia.device_behavior now holds {rows[0][0]} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
