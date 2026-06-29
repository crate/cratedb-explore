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
validate — does a behaviour-similarity signal actually exist in rtia.iot_data?

Run THIS before building the CrateDB vector table or the agent tool. It answers
two questions on the live data, with no plumbing to commit to yet:

  (a) Signal check — within each device_type, do faulting devices (any `critical`
      reading) have measurably different feature stats than healthy ones? Reports
      per-feature separation as a standardized mean difference (Cohen's d) and the
      AUC of ranking devices by that feature. |d| >= ~0.5 or AUC away from 0.5 is
      real separation.

  (b) Clone-and-perturb self-match — a ground truth we MANUFACTURE, so it holds
      even if the synthetic data has no real structure. Take a device's series,
      add noise, re-featurize, and check its nearest neighbour (within its type)
      is itself. High precision@1 that degrades gracefully with noise == the
      vector is discriminative.

    python validate.py            # uses CRATEDB_* env vars (HTTP 4200, schema rtia)
"""

from __future__ import annotations

import sys

import numpy as np

import behavior_features as bf


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """AUC of using `scores` to rank the positive class (label True). 0.5 = no signal."""
    pos = scores[labels]
    neg = scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    # Mann-Whitney U / (n_pos * n_neg)
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    r_pos = ranks[:len(pos)].sum()
    u = r_pos - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan")
    pooled = np.sqrt(((na - 1) * a.var(ddof=1) + (nb - 1) * b.var(ddof=1)) / (na + nb - 2))
    return (a.mean() - b.mean()) / pooled if pooled > 0 else 0.0


def signal_check(devices, ids, types, X):
    print("\n" + "=" * 70)
    print("(a) SIGNAL CHECK — faulting vs healthy feature separation, per type")
    print("=" * 70)
    faulting = np.array([devices[d].is_faulting for d in ids])
    any_signal = False
    for t in sorted(set(types)):
        mask = types == t
        lab = faulting[mask]
        n_f, n_h = int(lab.sum()), int((~lab).sum())
        print(f"\n  {t}: {n_f} faulting / {n_h} healthy")
        if n_f < 2 or n_h < 2:
            print("    (too few in one class to test)")
            continue
        Xt = X[mask]
        scored = []
        for j, name in enumerate(bf.FEATURE_NAMES):
            col = Xt[:, j]
            d = cohens_d(col[lab], col[~lab])
            a = auc(col, lab)
            scored.append((abs(d), d, a, name))
        scored.sort(reverse=True)
        for absd, d, a, name in scored[:4]:
            flag = "  <-- separates" if absd >= 0.5 else ""
            print(f"    {name:12s} d={d:+.2f}  AUC={a:.2f}{flag}")
        if scored[0][0] >= 0.5:
            any_signal = True
    return any_signal


def perturb_self_match(devices, ids, types, noise_fracs=(0.02, 0.05, 0.10, 0.20),
                       sample=60, seed=0):
    print("\n" + "=" * 70)
    print("(b) CLONE-AND-PERTURB SELF-MATCH — within-type precision@1")
    print("=" * 70)
    rng = np.random.default_rng(seed)
    _, _types, _X = bf.build_matrix(devices)
    _, scalers = bf.standardize_within_type(_X, _types)

    # Pre-standardize the fleet per type so we compare in the same space.
    by_type = {}
    for t in sorted(set(types)):
        t_ids = [d for d in ids if devices[d].device_type == t]
        mu, sd = scalers[t]
        Z = np.vstack([(devices[d].vector() - mu) / sd for d in t_ids])
        by_type[t] = (t_ids, Z, mu, sd)

    pick = rng.choice(ids, size=min(sample, len(ids)), replace=False)
    print(f"\n  probing {len(pick)} devices at each noise level "
          f"(noise = fraction of each series' std)\n")
    print("    noise   precision@1   median_rank_of_self")
    ok_overall = True
    for nf in noise_fracs:
        hits, ranks = 0, []
        for did in pick:
            dev = devices[did]
            t = dev.device_type
            t_ids, Z, mu, sd = by_type[t]
            sigma = dev.values.std()
            probe_vals = dev.values + rng.normal(0, nf * sigma, size=dev.values.shape)
            q = (bf.featurize(probe_vals, dev.times) - mu) / sd
            d = np.linalg.norm(Z - q, axis=1)
            order = np.argsort(d)
            self_i = t_ids.index(did)
            rank = int(np.where(order == self_i)[0][0]) + 1
            ranks.append(rank)
            if rank == 1:
                hits += 1
        p1 = hits / len(pick)
        print(f"    {nf:5.0%}   {p1:11.2f}   {np.median(ranks):.1f}")
        if nf <= 0.05 and p1 < 0.9:
            ok_overall = False
    return ok_overall


def main():
    print("Loading iot_data from CrateDB (HTTP _sql, schema rtia)…")
    devices = bf.load_devices()
    ids, types, X = bf.build_matrix(devices)
    print(f"Loaded {len(ids)} devices, "
          f"{sum(d.n_total for d in devices.values())} readings, "
          f"{len(set(types))} device types.")

    sig = signal_check(devices, ids, types, X)
    match = perturb_self_match(devices, ids, types)

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(f"  (a) faulting-vs-healthy signal present:  {'YES' if sig else 'NO / WEAK'}")
    print(f"  (b) self-match holds at low noise:       {'YES' if match else 'NO'}")
    if match:
        print("\n  -> Vectors are discriminative within type: the KNN plumbing is")
        print("     worth building. Proceed to the device_behavior table + tool.")
    else:
        print("\n  -> Self-match is weak even at low noise: revisit the feature set")
        print("     before committing to the table/tool.")
    if not sig:
        print("\n  Note: fault separation is weak — 'find devices behaving alike'")
        print("  will still cluster by operating profile (e.g. runs hot vs cool),")
        print("  but won't reliably surface co-faulting devices. Demo accordingly.")


if __name__ == "__main__":
    sys.exit(main())
