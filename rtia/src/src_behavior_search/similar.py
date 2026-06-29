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
similar — find devices behaving like a given device, via CrateDB KNN_MATCH.

The numeric analogue of src_rag's notes search: KNN_MATCH over the stored
behaviour vectors in rtia.device_behavior, SCOPED to the same device_type (cross-
type comparison is meaningless — different units/scales). Reads the query
device's own stored vector and matches it against the fleet.

    python similar.py DEVICE_0212 --k 8
"""

from __future__ import annotations

import argparse
import sys

import behavior_features as bf


def find_similar(device_id: str, k: int = 8):
    """Return (query_row, neighbours). query_row is None if the device is unknown."""
    _, rows = bf.run_sql(
        "SELECT b.behavior_vector, b.device_type, b.n_critical, b.n_readings, "
        "       d.plant_id, d.line_id "
        "FROM device_behavior b JOIN devices d ON d.device_id = b.device_id "
        "WHERE b.device_id = ?", args=[device_id])
    if not rows:
        return None, []
    qvec, dtype, q_crit, q_n, q_plant, q_line = rows[0]
    # KNN_MATCH returns approximate nearest over the whole table; vectors are
    # per-type standardized so other types CAN sit near in the shared space.
    # Ask for the whole table (500) then keep same-type neighbours, excluding self.
    # Join devices (1 row/device) for each neighbour's plant/line — a shared plant
    # or line among co-faulting neighbours points at a likely common cause.
    cols, neigh = bf.run_sql(
        "SELECT b.device_id, b.device_type, d.plant_id, d.line_id, "
        "       b.n_critical, b.n_warning, b.n_readings, b._score "
        "FROM device_behavior b JOIN devices d ON d.device_id = b.device_id "
        f"WHERE KNN_MATCH(b.behavior_vector, {bf.vec_literal(qvec)}, 500) "
        "  AND b.device_type = ? AND b.device_id <> ? "
        "ORDER BY b._score DESC LIMIT ?",
        args=[dtype, device_id, k])
    return {"device_id": device_id, "device_type": dtype, "n_critical": q_crit,
            "n_readings": q_n, "plant_id": q_plant, "line_id": q_line}, \
        [dict(zip(cols, r)) for r in neigh]


def main():
    p = argparse.ArgumentParser(description="Find devices behaving like a given device (KNN_MATCH).")
    p.add_argument("device_id", help="e.g. DEVICE_0212")
    p.add_argument("--k", type=int, default=8, help="neighbours to return (default 8)")
    args = p.parse_args()

    q, neigh = find_similar(args.device_id, args.k)
    if q is None:
        print(f"No behaviour vector for {args.device_id} — is it in device_behavior?", file=sys.stderr)
        return 1
    print(f"Query: {q['device_id']} ({q['device_type']}, plant={q['plant_id']} "
          f"line={q['line_id']}) — {q['n_critical']} critical of {q['n_readings']} readings\n")
    print(f"{'rank':>4}  {'device_id':14} {'plant':16} {'line':9} {'crit':>5} {'warn':>5}  score")
    for i, r in enumerate(neigh, 1):
        print(f"{i:>4}  {r['device_id']:14} {r['plant_id']:16} {r['line_id']:9} "
              f"{r['n_critical']:>5} {r['n_warning']:>5}  {r['_score']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
