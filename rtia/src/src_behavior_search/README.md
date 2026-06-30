# src_behavior_search — find devices that behave alike

A numeric counterpart to `src_rag`'s text search. Where `semantic_search` embeds
free-text `notes` and KNN-matches them, this turns each device's **sensor window**
into a numeric **behaviour vector** and KNN-matches *those* — so you can ask "which
devices behave like this failing one?" It plugs into the `src_rag` agentic loop as
a third tool, `similar_devices`.

## Why a different model than the notes embedding

`iot_data` is numeric, so a text embedding model is the wrong tool — stringifying
`23.5` and embedding it is meaningless. Instead the "embedding" is a fixed set of
**summary statistics** over the device's readings. Recon of the demo data shaped
the design:

- **Each device measures exactly one metric** (`device_type` ↔ `metric_unit` is
  1:1: temperature→C, vibration→mm/s, power→kW, pressure→bar, flow→m³/h). So a
  device vector is the stats of a single series, not a multi-metric concat.
- **Units differ across types**, so similarity is only meaningful **within a
  `device_type`**. Vectors are z-scored per type and KNN is scoped to the type.
- **`status` is held out as a validation label, never a feature** — so the vector
  can't "cheat" by encoding the fault flag.

The vector is 9 value-derived features: `mean, std, min, max, range, p05, p50,
p95, trend_slope`.

## Files

| File | What it is |
| --- | --- |
| `behavior_features.py` | Core: load `iot_data`, `featurize()` a series, build the fleet matrix, within-type z-score, and an in-memory KNN. Also the CrateDB HTTP `_sql` helpers. |
| `validate.py`          | The go/no-go harness — run it first. Proves a signal exists before any plumbing: (a) faulting-vs-healthy feature separation per type, (b) clone-and-perturb self-match (a manufactured ground truth that holds even if the synthetic data has no real structure). |
| `backfill.py`          | Upserts every device's within-type standardized vector + fault counts into `rtia.device_behavior` (1 row per device — never fans out). The table's DDL lives in `rtia/sql/rtia_schema_create.sql`; backfill does not create it and fails with a clear message if it is missing. |
| `similar.py`           | Standalone CLI: KNN_MATCH over `device_behavior` for a device's nearest behavioural neighbours, scoped to its type. |

The `similar_devices` agent tool lives in `rtia/src/src_rag/rtia_rag.py` (handler
`tool_similar_devices`), reusing the table this module builds.

## Run

First time, create and activate a Python virtualenv (an isolated per-project set of
packages), then install the one dependency (numpy):

```bash
python3 -m venv .venv          # create the environment in ./.venv
source .venv/bin/activate       # activate it — your prompt shows (.venv); Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For the CrateDB connection, copy the repo-root `env.example.sh` to `env.sh`, fill in
your host and credentials, and `source env.sh` — it defines every `CRATEDB_*` variable
this module reads, so nothing sensitive goes on the command line. Then:

```bash
python validate.py                # prove the signal first (go/no-go)
# rtia.device_behavior must exist — it's created by rtia/sql/rtia_schema_create.sql
python backfill.py                # populate rtia.device_behavior
python similar.py DEVICE_0180 --k 8
```

Connection is the CrateDB HTTP `_sql` endpoint (port 4200), schema `rtia` — the
same transport as the MCP servers. The `src_rag` tool handler instead reuses that
module's PostgreSQL-wire (`psycopg`, 5432) connection; both speak to the same
`rtia.device_behavior` table.

## How it validated (demo cluster, 500 devices / 501k readings)

- **Signal:** faulting vs healthy separated in every type (Cohen's d 0.9–1.9, AUC
  up to 0.94), driven by the spread/tail features (`range`, `max`, `std`, `p95`) —
  faults show up as excursions, not a shifted baseline.
- **Self-match:** within-type precision@1 was 1.00 at 2–5% noise, degrading
  gracefully to 0.93 at 20%.
- **End to end:** `similar_devices(DEVICE_0180)` (230 critical) returns same-type
  neighbours that are also heavy faulters — behaviour similarity surfaces
  co-faulting devices.

## Caveats

- **Within-type only.** Cross-type "similarity" is meaningless and is not offered;
  the tool scopes KNN to the query device's `device_type`.
- **No scaler persisted.** The per-type z-score scaler is applied at backfill time
  and the standardized vector is stored, so query-time search needs no scaler. A
  brand-new device not yet in `device_behavior` can't be searched until the
  backfill is re-run.
- **Snapshot, not streaming.** `backfill.py` is a one-shot over the current
  window; re-run it to refresh. There's no incremental update path yet.
