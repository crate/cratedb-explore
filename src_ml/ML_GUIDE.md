# Running the ML Models — Predictive Maintenance & Anomaly Detection

You have 500,000 sensor readings across 500 devices and three health profiles baked into the data: gradual degraders, occasional faulters, and devices that are reliably healthy. The dataset is clean, but realistic — fault classes are imbalanced, readings arrive at device-level frequencies, and the strongest signals are historical context, not point-in-time values.

This guide walks through building two models from that dataset, understanding what they actually learned, and then moving from offline scripts to a live inference service backed by CrateDB.

By the end you will have:

- **A trained fault classifier** — XGBoost, trained on per-device rolling windows (5, 10, 20 readings), split by `device_id` to prevent leakage, with class-imbalance correction. Outputs `fault_probability` for the next 5 readings.
- **A trained anomaly detector** — Isolation Forest fit on normal readings only. Scores any reading on how far it deviates from the healthy-fleet baseline, independent of whether a fault label was ever assigned.
- **Two ways to score** — a batch script (`predict.py`) for offline or scheduled use, and a FastAPI service (`realtime_inference.py`) that fetches rolling context from CrateDB per request and writes predictions back.
- **An understanding of when CrateDB replaces the JSON file** — for recurring retraining, large-scale feature pre-aggregation, and live scoring where historical context cannot be shipped as a file.

The three scenarios in the "Using CrateDB as the data source" section are the practical bridge between a working demo and a production pipeline.

---

## What the models do

| Model | Type | Question it answers |
|---|---|---|
| Predictive maintenance | Binary classifier (XGBoost) | Will this device enter warning or critical state within the next 5 readings? |
| Anomaly detector | Isolation Forest (unsupervised) | How anomalous is this reading relative to the healthy-fleet baseline? |

Both models are trained in a single script and saved to `model/`. Scoring runs separately via `predict.py`.

---

## Prerequisites

### Python packages

From `src_ml/`, create a virtualenv and install the dependencies:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`xgboost` is optional but strongly recommended. Without it the training script falls back to scikit-learn's `GradientBoostingClassifier`, which produces the same result but trains roughly 3–5× slower.

### Dataset

Both `train_model.py` and `predict.py` read directly from the shared
`../data/iot_demo_dataset.json` — the same dataset the COPY FROM and Telegraf
demos use. It is gitignored and auto-downloaded from S3 on first use by
whichever script runs first (`predict.py` only auto-downloads when scoring the
default dataset, not a custom `--input` file):

```
../data/
├── iot_demo_dataset.json   ← required (240 MB, 500,000 rows, gitignored)
├── devices.json            ← not used by the ML models
└── plants.json             ← not used by the ML models
```

---

## Step 1 — Train the models

```bash
python train_model.py
```

### What happens during training

The script runs in five phases:

**1. Load** — reads all 500,000 rows from `iot_demo_dataset.json` into memory (~4 GB RAM peak during feature engineering).

**2. Feature engineering** — groups readings by `device_id` and computes rolling statistics for each device independently. Rolling windows look *backward only* (shifted by one reading) so no future data leaks into the features.

The intuition: a device reading `metric_value = 85` right now means something very different depending on whether it has been steady at 85 for the past 20 readings, or whether it was at 30 an hour ago and is climbing fast. The raw number alone cannot tell you that. The rolling features can.

| Feature | Window | Description |
|---|---|---|
| `metric_mean_5/10/20` | 5, 10, 20 | Rolling average of metric value |
| `metric_std_5/10/20` | 5, 10, 20 | Rolling standard deviation — captures volatility |
| `quality_mean_5/10/20` | 5, 10, 20 | Rolling average quality score |
| `fault_rate_5/10/20` | 5, 10, 20 | Fraction of last N readings in warning/critical |
| `metric_delta` | — | Point-to-point change in metric value |
| `quality_delta` | — | Point-to-point change in quality score |
| `hour`, `day_of_week` | — | Time-of-day and day-of-week patterns |
| `device_type_enc` | — | Label-encoded device category |
| `fw_major` | — | Firmware major version number |

**3. Target creation** — for each reading at time *t*, the target is `1` if any reading in the next 5 positions (for the same device) has `status = 'warning'` or `status = 'critical'`. The last 5 rows per device are dropped because no valid lookahead label exists for them.

This turns a status label into a predictive signal. The model does not learn to recognise a fault that already happened — it learns to detect the pattern in the readings *before* a fault arrives. That is what makes it useful for scheduling maintenance rather than just logging incidents.

**4. Train/test split** — split by `device_id` (80/20). No readings from a test device appear in the training set. This is intentional: it tests whether the model generalises to *new* devices, not just new readings from devices it has seen.

A random row split would give a misleadingly high test score. The model would have seen the same device during training, memorised its historical pattern, and "predicted" the test rows by recognising the device — not by learning transferable behaviour. Splitting by device prevents that.

**5. Class imbalance** — roughly 75% of readings are healthy. XGBoost's `scale_pos_weight` is set to `n_negative / n_positive` to compensate, preventing the model from defaulting to "always predict healthy."

Without this correction, a model that predicts "healthy" for every single reading would score 75% accuracy and look fine on paper. `scale_pos_weight` tells XGBoost that missing a real fault is much more costly than a false alarm, and forces it to take the minority class seriously during training.

### Expected output

```
Loading iot_demo_dataset.json ...
  500,000 rows  |  500 devices  |  3.2s
Engineering features ...
Creating target: fault within next 5 readings ...
  Rows after trim: 497,500  |  healthy: ~370,000  |  fault_incoming: ~127,000  |  imbalance ratio: ~2.9:1
Splitting by device ...
  Train: ~398,000 rows (400 devices)  |  Test: ~99,500 rows (100 devices)
Training classifier ...
  XGBoost  —  trained in ~45s

── Evaluation ──────────────────────────────────────────────────────
              precision  recall  f1-score
healthy            0.92    0.87      0.89
fault_incoming     0.73    0.82      0.77

ROC-AUC:  0.91

Top 10 features:
              feature  importance
         fault_rate_5    0.182
        fault_rate_10    0.141
    quality_mean_5        0.098
       metric_std_10    0.087
...

Training anomaly detector (Isolation Forest) ...
  Isolation Forest trained in ~12s

Model saved  → model/predictive_maintenance_model.pkl
Anomaly detector saved → model/anomaly_detector_model.pkl
```

**ROC-AUC of ~0.91** means the classifier separates healthy from fault-incoming devices well. The recall on `fault_incoming` (~0.82) is more important than precision here — missing a real fault is more costly than a false alarm.

### Outputs written to `model/`

| File | Description |
|---|---|
| `predictive_maintenance_model.pkl` | Classifier + feature list + label encoder + metadata |
| `anomaly_detector_model.pkl` | Isolation Forest + feature list |
| `feature_importance.csv` | All features ranked by importance |
| `test_predictions.csv` | Every test-set row with `fault_probability`, `predicted_fault`, `actual_fault` |

---

## Step 2 — Score new readings

Training is a one-time operation. Scoring is what you run against new data — on demand, daily, or on a schedule. The script builds the same rolling features used during training, so whatever the model learned during training is applied consistently here. The output is one row per reading with a fault probability and anomaly score attached.

```bash
python predict.py
```

By default, this takes the last 50 readings per device from `iot_demo_dataset.json` (50 readings per device provides enough history for rolling features to stabilise), scores them, and writes results to `model/scored_batch.csv`.

### Options

```bash
# Score last 50 readings per device (default)
python predict.py

# Score a single device only
python predict.py --device DEVICE_0042

# Score from a different NDJSON file
python predict.py --input /path/to/new_readings.json
```

The input file must be NDJSON (one JSON object per line). Records may be in the
canonical Telegraf `tags{}`/`fields{}` shape (like the demo dataset) or already
flat — the loader flattens either. A flat record needs at minimum:
`device_id`, `device_type`, `timestamp`, `metric_value`, `quality_score`, `status`, `metadata`

### Output columns in `scored_batch.csv`

| Column | Type | Description |
|---|---|---|
| `device_id` | text | Device identifier |
| `device_type` | text | Sensor category |
| `plant_id` | text | Facility |
| `timestamp` | datetime | Reading timestamp |
| `metric_value` | float | Raw sensor reading |
| `metric_unit` | text | Unit of measure |
| `quality_score` | float | Quality score (0–100) |
| `status` | text | Current status label |
| `fault_probability` | float (0–1) | Classifier output — probability of fault in next 5 readings |
| `fault_risk_label` | text | `low` (< 0.25) · `medium` (0.25–0.60) · `high` (> 0.60) |
| `anomaly_score` | float | Isolation Forest score — higher means more anomalous |

### Example output

```
── Scoring summary ─────────────────────────────────────────────────
Rows scored: 25,000

Fault risk distribution:
low       18,432
medium     4,891
high       1,677

Top 10 highest fault-probability readings:
   device_id device_type    plant_id          timestamp  status  fault_probability  anomaly_score
DEVICE_0387  vibration_sensor  PLANT_DORTMUND  2025-10-11 22:00  critical  0.974  0.312
DEVICE_0219  pressure_sensor   PLANT_HAMBURG   2025-10-10 14:00  warning   0.961  0.287
...
```

---

## Step 3 — Inspect the results

Before relying on the model in production, check whether it learned the right things. Two outputs are worth examining: the feature importances (did the model find the signals you expected?) and the test predictions (where does it fail, and on which device types?).

### Feature importance's

`model/feature_importance.csv` shows which signals the model found most predictive. Typical ranking for this dataset:

1. `fault_rate_5` — the single strongest signal: if 3 of the last 5 readings were warning/critical, the device is almost certainly heading for another fault
2. `fault_rate_10` — sustained fault rate over a longer window
3. `quality_mean_5` — recent quality score trend
4. `metric_std_10` — volatility in the metric value — rising variance precedes faults in degrading devices
5. `metric_delta` — direction and speed of change right now

The rolling features systematically outrank the raw point-in-time features (`metric_value`, `quality_score`), which confirms the value of time-series context over snapshot analysis.

### Test predictions

`model/test_predictions.csv` contains every test-set row with the actual label alongside the prediction. Use it to:

- Filter to `actual_fault = 1, predicted_fault = 0` to see false negatives — readings where a fault was coming but the model missed it
- Filter to `actual_fault = 0, predicted_fault = 1` to see false positives — unnecessary alerts
- Group by `device_type` to check whether the model performs unevenly across sensor categories

---

## Interpreting the scores

Every scored reading carries two independent numbers. They answer different questions and should be read together. A device can be high-anomaly but low-fault-probability (a reading that looks unusual but is not trending toward failure), or high-fault-probability but low-anomaly (a device following the exact same degradation pattern the model has seen before — unsurprising, but still on a fault trajectory). Neither number alone is sufficient.

### fault_probability

| Range | Label | Interpretation |
|---|---|---|
| 0.00 – 0.25 | `low` | Device operating within normal parameters |
| 0.25 – 0.60 | `medium` | Early-warning signal — monitor closely |
| 0.60 – 1.00 | `high` | Strong fault signal — consider scheduling inspection |

A score of `0.85` means the model is confident this device will cross a threshold within the next 5 readings (~5 hours given hourly data). It is not a certainty — treat it as a triage score.

### anomaly_score

The Isolation Forest is calibrated with `contamination = 0.05`, so roughly the top 5% of scores in production data will be flagged as anomalous by the default threshold. Unlike the classifier, anomaly scores carry no probability interpretation — they are ordinal. Use them to rank readings, not to set a fixed threshold.

A reading can be high-anomaly but low-fault-probability (unusual sensor reading that is not trending toward a fault) and vice versa. Both scores together give a more complete picture than either alone.

---

## Using the models in your own code

The snippet below is also available as a runnable script: [`use_models_example.py`](use_models_example.py).

```python
import pickle
import pandas as pd

# Load
with open('model/predictive_maintenance_model.pkl', 'rb') as f:
    clf = pickle.load(f)
with open('model/anomaly_detector_model.pkl', 'rb') as f:
    iso = pickle.load(f)

print(clf['features'])          # exact ordered feature list the model expects (20 features)
print(f"Horizon: {clf['horizon']} readings   ROC-AUC: {clf['roc_auc']}")

# device_type must be label-encoded first (strings -> ints)
device_type_enc = clf['label_encoder'].transform(['vibration_sensor'])[0]

# Build one feature row as {name: value}, then select each model's feature list
# BY NAME. The classifier uses all 20 features; the anomaly detector uses a
# 7-feature subset in a *different* order — so never slice the array positionally.
row = {
    'metric_value': 85.0, 'quality_score': 91.0, 'hour': 14, 'day_of_week': 2,
    'device_type_enc': device_type_enc, 'metric_delta': 1.2, 'quality_delta': -0.5,
    'fw_major': 2,
    # Rolling-window features — in real use these come from engineer_features();
    # here they're filled with plausible values so the example runs standalone.
    'metric_mean_5': 84.0, 'metric_mean_10': 83.5, 'metric_mean_20': 83.0,
    'metric_std_5': 1.5, 'metric_std_10': 1.8, 'metric_std_20': 2.1,
    'quality_mean_5': 91.5, 'quality_mean_10': 92.0, 'quality_mean_20': 92.5,
    'fault_rate_5': 0.0, 'fault_rate_10': 0.0, 'fault_rate_20': 0.0,
}
X = pd.DataFrame([row])

# Select by name, then pass a plain NumPy array (.to_numpy()): the anomaly
# detector was fitted on a nameless array, so a named DataFrame would trigger a
# sklearn feature-name warning.
prob    = clf['model'].predict_proba(X[clf['features']].to_numpy())[0, 1]
anomaly = -iso['model'].score_samples(X[iso['features']].to_numpy())[0]
print(f'Fault probability: {prob:.1%}   Anomaly score: {anomaly:.4f}')
```

Selecting columns with `X[clf['features']]` / `X[iso['features']]` guarantees each model sees its features in the order it was trained on — the same thing `predict.py` and `realtime_inference.py` do.

---

## Retraining on new data

To retrain on a fresh dataset:

1. Replace `../data/iot_demo_dataset.json` with the new file (same schema)
2. Run `python train_model.py`
3. The new `.pkl` files overwrite the old ones in `model/`

No code changes needed as long as the field names in the JSON match what the script expects: `device_id`, `device_type`, `plant_id`, `timestamp`, `metric_value`, `quality_score`, `status`, `metadata`.

---

## Using CrateDB as the data source

The default scripts read from a local JSON file. That works for a one-time demo or offline development. The moment the dataset grows — new readings every minute, new devices added, data spanning months — the file goes stale the instant you export it, and re-exporting before every training run becomes a manual step that will eventually be skipped or forgotten.

Swapping the file read for a SQL query against CrateDB removes that problem entirely. The training and scoring logic stays identical; only the data source changes. The three scenarios below cover the most common cases in order of complexity.

### When the JSON file is sufficient

- First run on a static dataset
- Offline model development with no running cluster
- Sharing a reproducible training artefact (the file is the dataset)

### When to query CrateDB instead

| Scenario | Why CrateDB |
|---|---|
| Data keeps growing | New sensor readings arrive continuously; the JSON snapshot goes stale immediately |
| Scheduled retraining | A cron job retrains weekly on the last 90 days — pulling from a file requires re-exporting first |
| Selective training | Train a plant-specific model by filtering in SQL, not in pandas |
| Feature aggregation at scale | Pre-compute rolling averages in CrateDB SQL before loading into pandas — faster and lower memory |
| Live scoring | Score the latest N readings per device without exporting any file |
| Write predictions back | Store `fault_probability` in CrateDB so it can be queried alongside raw sensor data |

---

### Setup

Install the dependencies (the CrateDB tier is already listed in
`requirements.txt`):

```bash
pip install -r requirements.txt
```

`sqlalchemy-cratedb` registers the `crate://` SQLAlchemy dialect (and pulls in
the official `crate` client); `sqlalchemy` itself backs `pandas.read_sql()`.

Both `train_model.py` and `predict.py` build the engine for you: pass the host
with `--cratedb-url` (or the `CRATEDB_URL` env var) and supply credentials via
the `CRATE_USER` / `CRATE_PASSWORD` environment variables, so passwords never
land in your shell history or `ps` output. The local JSON file stays the default
— the CrateDB URL is purely additive.

**CrateDB local / Docker:**

```bash
python train_model.py --cratedb-url crate://localhost:4200 --days 90
python predict.py     --cratedb-url crate://localhost:4200
```

**CrateDB Cloud:**

```bash
export CRATE_USER=admin CRATE_PASSWORD='<password>'
export CRATEDB_URL='crate://<your-cluster>.cratedb.net:4200/?ssl=true'
python train_model.py        # picks up CRATEDB_URL automatically
```

`--days N` (training) limits the pull to the last *N* days **relative to now**
(`--days 0` = all history). If you omit `--days`, `train_model.py` uses an
unusual default: the last 90 days relative to the **latest reading in the DB**
(`MAX("timestamp") - INTERVAL '90' DAY`), not wall-clock now, logged at run
time. This avoids the trap where a static demo dataset with older-than-90-days
timestamps makes a `NOW()`-relative window return nothing. `predict.py` pulls
the last 50 readings per device for rolling context — add `--device
DEVICE_0001` to score a single device.

---

### Scenario 1 — Scheduled retraining on recent data

`train_model.py --cratedb-url crate://localhost:4200 --days 90` pulls the last 90 days straight from CrateDB instead of the file (omit `--days` to use the DB-anchored default described above). Everything downstream (feature engineering, training) stays identical. Under the hood the script runs this query (in `data_source.load_training_frame`):

```sql
SELECT
    tags['device_id']                 AS device_id,
    tags['device_type']               AS device_type,
    tags['plant_id']                  AS plant_id,
    "timestamp",
    fields['metric_value']            AS metric_value,
    tags['metric_unit']               AS metric_unit,
    tags['status']                    AS status,
    fields['quality_score']           AS quality_score,
    tags['metadata_firmware_version'] AS firmware_version
FROM rtia.iot_data
WHERE "timestamp" >= (SELECT MAX("timestamp")            -- default: last 90 days
                      FROM rtia.iot_data) - INTERVAL '90' DAY   -- of the newest data
-- with --days N instead:  WHERE "timestamp" >= NOW() - INTERVAL 'N' DAY
-- with --days 0:          no WHERE clause (all history)
ORDER BY tags['device_id'], "timestamp"
```

`rtia.iot_data` stores the readings in Telegraf's line-protocol shape — strings
live in the `tags` object, numbers in the `fields` object — so the query reaches
into them with `tags['…']` / `fields['…']` and aliases each back to the flat
column name the feature code expects. CrateDB returns each as a plain column, so
there is no object to unpack in Python after loading.

Run this on a schedule (weekly, nightly) and the model always reflects the current fleet behaviour without any file management.

---

### Scenario 2 — Push feature aggregation into CrateDB

Rolling statistics over 500,000 rows in pandas requires loading every raw reading into memory. CrateDB can pre-compute `DATE_BIN` windows, fault rates, and averages in the database, then hand a compact feature table to pandas. This is substantially faster and uses a fraction of the RAM.

This is an advanced, manual pattern — it changes the feature schema, so it isn't wired into the scripts' `--cratedb-url` path, but the query runs as-is against `rtia.iot_data`.

```python
sql = """
    SELECT
        tags['device_id']                 AS device_id,
        tags['device_type']               AS device_type,
        tags['plant_id']                  AS plant_id,
        DATE_BIN('1 hour'::INTERVAL, "timestamp", TIMESTAMP '2025-09-01') AS window_start,
        AVG(fields['metric_value'])                                AS metric_mean,
        STDDEV(fields['metric_value'])                             AS metric_std,
        AVG(fields['quality_score'])                               AS quality_mean,
        COUNT(*) FILTER (WHERE tags['status'] IN ('warning','critical'))
            * 1.0 / NULLIF(COUNT(*), 0)                           AS fault_rate,
        MAX(CASE WHEN tags['status'] IN ('warning','critical') THEN 1 ELSE 0 END) AS had_fault
    FROM rtia.iot_data
    WHERE "timestamp" >= TIMESTAMP '2025-09-01'
    GROUP BY tags['device_id'], tags['device_type'], tags['plant_id'], window_start
    ORDER BY tags['device_id'], window_start
"""

df = pd.read_sql(sql, engine)
```

Each row is now one device-hour window. The model trains on `metric_mean`, `metric_std`, `quality_mean`, `fault_rate` directly — no rolling transform needed in Python. `had_fault` becomes the target.

This pattern scales to hundreds of millions of rows because CrateDB distributes the aggregation across shards before any data crosses the network.

---

### Scenario 3 — Live batch scoring against the current fleet state

`predict.py --cratedb-url crate://localhost:4200` already does the pull-and-score half of this — it loads the last 50 readings per device from CrateDB, scores them, and writes `model/scored_batch.csv` (add `--device DEVICE_0001` for one device). The pipeline below is the fuller version that also writes the predictions *back* to CrateDB's `fault_predictions` table in one pass.

```python
import pickle
import numpy as np
import pandas as pd
from sqlalchemy import create_engine

engine = create_engine("crate://localhost:4200")

# Pull the last 50 readings per device for rolling-feature context
sql = """
    SELECT tags['device_id']                 AS device_id,
           tags['device_type']               AS device_type,
           tags['plant_id']                  AS plant_id,
           "timestamp",
           fields['metric_value']            AS metric_value,
           fields['quality_score']           AS quality_score,
           tags['status']                    AS status,
           tags['metadata_firmware_version'] AS firmware_version
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY tags['device_id'] ORDER BY "timestamp" DESC) AS rn
        FROM rtia.iot_data
    ) t
    WHERE rn <= 50
    ORDER BY tags['device_id'], "timestamp"
"""

df = pd.read_sql(sql, engine)

# Build features (same logic as predict.py) — produces the named feature
# columns that clf["features"] / iso["features"] refer to.
# ... feature engineering ...

# Load both models and score
with open("model/predictive_maintenance_model.pkl", "rb") as f:
    clf = pickle.load(f)
with open("model/anomaly_detector_model.pkl", "rb") as f:
    iso = pickle.load(f)

df["fault_probability"] = clf["model"].predict_proba(df[clf["features"]].values)[:, 1]
df["anomaly_score"]     = -iso["model"].score_samples(df[iso["features"]].values)

# Keep the latest scored reading per device, then shape it to match the
# fault_predictions table that realtime_inference.py creates — same columns,
# same names — so the batch job and the live service write one schema.
latest = df.groupby("device_id").last().reset_index()
latest["scored_at"]         = pd.Timestamp.utcnow()
latest["latest_reading_ts"] = latest["timestamp"]
latest["current_status"]    = latest["status"]
latest["fault_risk_label"]  = pd.cut(
    latest["fault_probability"],
    bins=[-0.01, 0.25, 0.60, 1.01],
    labels=["low", "medium", "high"],
).astype(str)

cols = ["device_id", "scored_at", "latest_reading_ts", "device_type",
        "plant_id", "current_status", "fault_probability",
        "fault_risk_label", "anomaly_score"]
latest[cols].to_sql(
    "fault_predictions",
    engine,
    if_exists="append",   # append into the existing table — never "replace"
    index=False,          # (replace would drop the service's PK'd table)
    method="multi",
)
```

Create `fault_predictions` once with the canonical DDL before the first append —
either by starting `realtime_inference.py` (it runs `CREATE TABLE IF NOT EXISTS`
on startup) or by issuing the same statement yourself. Letting pandas auto-create
it would infer a different, PK-less schema.

Once `fault_predictions` exists in CrateDB, you can JOIN it against live sensor data in a single query:

```sql
SELECT i.tags['device_id']      AS device_id,
       i.tags['device_type']    AS device_type,
       i.tags['plant_id']       AS plant_id,
       i.tags['status']         AS status,
       i.fields['metric_value'] AS metric_value,
       f.fault_probability
FROM rtia.iot_data i
JOIN fault_predictions f ON i.tags['device_id'] = f.device_id
WHERE f.fault_probability > 0.60
ORDER BY f.fault_probability DESC;
```

This is the full loop: sensor data lives in CrateDB, features are computed there, predictions are scored in Python, and results land back in CrateDB — queryable alongside the raw readings in real time.

---

## Real-time inference

The batch scripts above work well when you can afford to wait — export data, run the script, read the CSV. Real-time inference is what you need when the question is "what is this device's fault risk *right now*, based on everything that has happened up to this second?"

This section covers a FastAPI service that answers that question on demand. It fetches the device's recent history from CrateDB, builds rolling features in memory, scores with the trained model, and writes the result back to CrateDB — all in a single HTTP call.

### The rolling-window problem

The model's two strongest features (`fault_rate_5`, `fault_rate_10`) are computed over the last 5 and 10 readings of each device. A single new reading carries no context on its own — scoring it requires the device's recent history.

This is why CrateDB is required at inference time, not just training time. The full history lives there, and `rtia.iot_data` is `PARTITIONED BY (day)` with `tags['device_id']` indexed (every key in the `tags` object is indexed by default). A recent-history lookup for one device prunes to the latest day partitions and uses that index, so fetching the last 50 readings returns in single-digit milliseconds.

### Architecture

```
[Sensor / gateway]
       |
       v
  CrateDB  (rtia.iot_data)
       |
       v                     <-- query last 50 readings per device
  realtime_inference.py
       |
       +-- build rolling features (Python / pandas)
       |
       +-- score (XGBoost + Isolation Forest)
       |
       v
  CrateDB  (fault_predictions)  <-- write fault_probability back
       |
       v
  Dashboard / alert system  <-- JOIN predictions against live readings
```

### Running the service

**File:** `realtime_inference.py` (in `src_ml/`)

Install the dependencies (`requirements.txt` already includes FastAPI/uvicorn and
the CrateDB tier — `sqlalchemy-cratedb` registers the `crate://` dialect the
service connects through), then:

```bash
pip install -r requirements.txt
uvicorn realtime_inference:app --reload --port 8000
```

Point `CRATEDB_URL` at your cluster:

```bash
# Local / Docker
CRATEDB_URL=crate://localhost:4200 uvicorn realtime_inference:app --port 8000

# CrateDB Cloud
CRATEDB_URL=crate://admin:<password>@<cluster>.cratedb.net:4200 uvicorn realtime_inference:app --port 8000
```

### Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Service status, model version, ROC-AUC |
| `GET` | `/score/{device_id}` | Score one device, write result to `fault_predictions` |
| `POST` | `/score/batch` | Score a list of devices in one call |
| `GET` | `/fleet/high-risk` | Devices with `fault_probability` above a threshold |

### Test with curl

```bash
# Check the service is up and show model metadata
curl http://localhost:8000/health

# Score a single device
curl http://localhost:8000/score/DEVICE_0042

# Score without writing to CrateDB (read-only)
curl "http://localhost:8000/score/DEVICE_0042?write=false"

# Score three devices in one request
curl -X POST http://localhost:8000/score/batch \
     -H "Content-Type: application/json" \
     -d '{"device_ids": ["DEVICE_0001", "DEVICE_0042", "DEVICE_0387"], "write_to_cratedb": true}'

# Fleet view: all devices with fault_probability > 0.6
curl "http://localhost:8000/fleet/high-risk?threshold=0.6&limit=20"
```

### Example response — `/score/DEVICE_0042`

```json
{
  "device_id": "DEVICE_0042",
  "device_type": "vibration_sensor",
  "plant_id": "PLANT_DORTMUND",
  "latest_reading_ts": "2025-10-11 22:03:14",
  "current_status": "warning",
  "fault_probability": 0.874,
  "fault_risk_label": "high",
  "anomaly_score": 0.291,
  "context_rows_used": 50,
  "latency_ms": 18.4
}
```

The service fetches 50 rows from CrateDB, builds rolling features, and returns a score in under 20 ms for a single device.

### Querying predictions alongside live data

Once `/score` writes to `fault_predictions`, the predictions are queryable from CrateDB like any other table:

```sql
-- Devices currently in warning or critical with high fault probability
SELECT
    i.tags['device_id']      AS device_id,
    i.tags['device_type']    AS device_type,
    i.tags['plant_id']       AS plant_id,
    i.tags['status']         AS status,
    i.fields['metric_value'] AS metric_value,
    f.fault_probability,
    f.fault_risk_label,
    f.scored_at
FROM rtia.iot_data i
JOIN fault_predictions f ON i.tags['device_id'] = f.device_id
WHERE i.tags['status'] IN ('warning', 'critical')
  AND f.fault_probability > 0.60
ORDER BY f.fault_probability DESC
LIMIT 20;
```

```sql
-- Average fault probability by plant — operational risk overview
SELECT
    f.plant_id,
    COUNT(DISTINCT f.device_id)          AS devices_scored,
    ROUND(AVG(f.fault_probability), 3)   AS avg_fault_probability,
    COUNT(*) FILTER (WHERE f.fault_risk_label = 'high') AS high_risk_devices
FROM fault_predictions f
GROUP BY f.plant_id
ORDER BY avg_fault_probability DESC;
```

### What triggers scoring in production

The service is passive — it scores on demand. Common triggers:

| Trigger | Pattern |
| --- | --- |
| Dashboard refresh | Browser calls `/score/{device_id}` on page load |
| Alert threshold breach | SCADA system calls `/score` when a reading exceeds a limit |
| Scheduled sweep | Cron job calls `/score/batch` with all 500 device IDs every 15 minutes |
| New reading insert | Application layer calls `/score` after each write to CrateDB |

---

## File reference

```
src_ml/
├── ML_GUIDE.md             ← this guide
├── requirements.txt        ← Python dependencies
├── train_model.py          ← training pipeline (run once)
├── predict.py              ← batch scoring (run on new data)
├── data_source.py          ← optional CrateDB loader (--cratedb-url)
├── use_models_example.py   ← minimal "use the models" example
├── realtime_inference.py   ← FastAPI inference service (optional)
└── model/                  ← created by train_model.py
    ├── predictive_maintenance_model.pkl
    ├── anomaly_detector_model.pkl
    ├── feature_importance.csv
    ├── test_predictions.csv
    └── scored_batch.csv    ← written by predict.py

../data/iot_demo_dataset.json   ← shared input dataset (gitignored)
```
