"""
CrateDB Industrial IoT - Real-time Inference Service
=====================================================
FastAPI service that scores devices on demand using the trained predictive
maintenance model. CrateDB is the live data source for feature context.

Why CrateDB is required at inference time (not just training):
  The model's strongest features are rolling windows over the last 5, 10, and 20
  readings. A single new reading carries no context. CrateDB stores the full
  history, so scoring any device requires fetching its last CONTEXT_ROWS readings
  first. rtia.iot_data is PARTITIONED BY (day) with tags['device_id'] indexed, so
  a recent-history lookup prunes to the latest partitions and returns in ms.

Architecture:
  [Sensor] --> CrateDB (rtia.iot_data) --> [this service] --> CrateDB (rtia.fault_predictions)
                                            |
                                     trained model

Endpoints:
  GET /score/{device_id}       Score one device right now
  GET /fleet/high-risk         All devices with fault_probability > threshold
  GET /health                  Service status + model metadata
  POST /score/batch            Score a list of device IDs in one call

Run:
  pip install fastapi uvicorn sqlalchemy crate pandas numpy scikit-learn xgboost
  uvicorn realtime_inference:app --reload --port 8000

Test with curl:
  curl http://localhost:8000/health
  curl http://localhost:8000/score/DEVICE_0042
  curl http://localhost:8000/fleet/high-risk?threshold=0.6
  curl -X POST http://localhost:8000/score/batch \\
       -H "Content-Type: application/json" \\
       -d '{"device_ids": ["DEVICE_0001", "DEVICE_0042", "DEVICE_0387"]}'
"""

import json
import os
import pickle
import sys
import time
import warnings
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from data_source import connection_error, make_engine

warnings.filterwarnings('ignore')

# ── Config ────────────────────────────────────────────────────────────────────
CRATEDB_ALCHEMY_URL    = os.getenv('CRATEDB_ALCHEMY_URL', 'crate://localhost:4200')
MODEL_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model')
CONTEXT_ROWS   = 50    # readings fetched per device for rolling-feature context
WINDOWS        = [5, 10, 20]
STATUS_CODE    = {'normal': 0, 'warning': 1, 'critical': 2, 'offline': -1}

# Global state — loaded once at startup, reused across all requests
_clf    = None
_iso    = None
_engine = None


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _clf, _iso, _engine

    print('Loading models ...')
    clf_path = os.path.join(MODEL_DIR, 'predictive_maintenance_model.pkl')
    iso_path = os.path.join(MODEL_DIR, 'anomaly_detector_model.pkl')

    for path in [clf_path, iso_path]:
        if not os.path.exists(path):
            raise RuntimeError(f'Model not found: {path}  --  run train_model.py first')

    with open(clf_path, 'rb') as f:
        _clf = pickle.load(f)
    with open(iso_path, 'rb') as f:
        _iso = pickle.load(f)

    print(f'  Classifier:  {_clf["model_name"]}  (ROC-AUC {_clf["roc_auc"]})')
    print(f'  Anomaly:     {_iso["model_name"]}')

    print(f'Connecting to CrateDB at {CRATEDB_ALCHEMY_URL} ...')
    _engine = make_engine(CRATEDB_ALCHEMY_URL)
    try:
        with _engine.connect() as conn:
            conn.execute(text('SELECT 1'))
    except SQLAlchemyError as exc:
        # Turn a 401 / unreachable-host stack trace into a one-line message and
        # stop the server cleanly instead of dumping the full traceback.
        print(f'\n{connection_error(_engine, exc)}', file=sys.stderr)
        raise SystemExit(1) from None
    print('  Connected.')

    # Predictions table must already exist (see sql/rtia_schema_create.sql)
    try:
        _require_predictions_table()
    except RuntimeError as exc:
        print(f'\n{exc}', file=sys.stderr)
        raise SystemExit(1) from None
    print('Ready.')

    yield

    print('Shutting down.')


app = FastAPI(
    title='CrateDB Predictive Maintenance API',
    version='1.0',
    lifespan=lifespan,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_predictions_table():
    """Verify rtia.fault_predictions exists. It is created by
    sql/rtia_schema_create.sql, not here — fail with a clear message if missing
    rather than silently creating it."""
    check = text("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'rtia' AND table_name = 'fault_predictions'
    """)
    with _engine.connect() as conn:
        exists = conn.execute(check).scalar()
    if not exists:
        raise RuntimeError(
            'Table rtia.fault_predictions not found — '
            'create the schema first:  crash < sql/rtia_schema_create.sql'
        )


def _fmt_ts(v):
    """Format a CrateDB TIMESTAMP for JSON. The sqlalchemy-cratedb dialect
    returns timestamps as epoch-millisecond integers; render them as a readable
    'YYYY-MM-DD HH:MM:SS' string (and tolerate None / already-parsed values)."""
    if v is None:
        return None
    return pd.to_datetime(int(v), unit='ms').strftime('%Y-%m-%d %H:%M:%S')


def _fetch_context(device_id: str) -> pd.DataFrame:
    """
    Fetch the last CONTEXT_ROWS readings for a device from CrateDB.
    Reads from rtia.iot_data, mapping the Telegraf tags{}/fields{} objects back
    to flat columns the feature builder expects.
    """
    sql = text("""
        SELECT
            tags['device_id']                 AS device_id,
            tags['device_type']               AS device_type,
            tags['plant_id']                  AS plant_id,
            "timestamp",
            fields['metric_value']            AS metric_value,
            fields['quality_score']           AS quality_score,
            tags['status']                    AS status,
            tags['metadata_firmware_version'] AS firmware_version
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY tags['device_id']
                       ORDER BY "timestamp" DESC
                   ) AS rn
            FROM rtia.iot_data
            WHERE tags['device_id'] = :device_id
        ) t
        WHERE rn <= :n
        ORDER BY "timestamp" ASC
    """)

    with _engine.connect() as conn:
        result = conn.execute(sql, {'device_id': device_id, 'n': CONTEXT_ROWS})
        rows = result.fetchall()
        cols = result.keys()

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows, columns=list(cols))


def _build_features(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Build classifier and anomaly-detector feature arrays from a context window.
    Must mirror the logic in train_model.py exactly.
    Returns (X_clf, X_iso) for the last row only (the most recent reading).
    """
    df = df.copy()
    df['timestamp']    = pd.to_datetime(df['timestamp'])
    df['status_code']  = df['status'].map(STATUS_CODE).fillna(0).astype(int)
    df['hour']         = df['timestamp'].dt.hour
    df['day_of_week']  = df['timestamp'].dt.dayofweek

    # Rolling features (shift(1) to exclude current reading from its own context)
    for w in WINDOWS:
        df[f'metric_mean_{w}']   = df['metric_value'].shift(1).rolling(w, min_periods=1).mean()
        df[f'metric_std_{w}']    = df['metric_value'].shift(1).rolling(w, min_periods=1).std().fillna(0)
        df[f'quality_mean_{w}']  = df['quality_score'].shift(1).rolling(w, min_periods=1).mean()
        df[f'fault_rate_{w}']    = (
            df['status_code'].shift(1)
            .rolling(w, min_periods=1)
            .apply(lambda s: (s > 0).sum() / len(s), raw=True)
        )

    # First reading(s) have no prior window — fill to stay NaN-free and
    # consistent with train_model.py (means -> own value; fault_rate -> 0).
    for w in WINDOWS:
        df[f'metric_mean_{w}']  = df[f'metric_mean_{w}'].fillna(df['metric_value'])
        df[f'quality_mean_{w}'] = df[f'quality_mean_{w}'].fillna(df['quality_score'])
        df[f'fault_rate_{w}']   = df[f'fault_rate_{w}'].fillna(0)

    df['metric_delta']  = df['metric_value'].diff().fillna(0)
    df['quality_delta'] = df['quality_score'].diff().fillna(0)
    df['fw_major']      = (
        df['firmware_version'].str.extract(r'^(\d+)', expand=False)
        .astype(float).fillna(1)
    )

    le = _clf['label_encoder']
    known = set(le.classes_)
    safe_type = df['device_type'].apply(lambda x: x if x in known else le.classes_[0])
    df['device_type_enc'] = le.transform(safe_type)

    # Score the last row only (the most recent reading)
    last = df.iloc[[-1]]

    X_clf = last[_clf['features']].values
    X_iso = last[_iso['features']].values

    return X_clf, X_iso


def _score_device(device_id: str) -> dict:
    t0 = time.time()

    df = _fetch_context(device_id)
    if df.empty:
        raise HTTPException(status_code=404, detail=f'Device {device_id} not found in rtia.iot_data')
    if len(df) < 5:
        raise HTTPException(
            status_code=422,
            detail=f'Device {device_id} has only {len(df)} readings — need at least 5 for rolling features'
        )

    X_clf, X_iso = _build_features(df)

    fault_prob   = float(_clf['model'].predict_proba(X_clf)[0, 1])
    anomaly_sc   = float(-_iso['model'].score_samples(X_iso)[0])

    if fault_prob < 0.25:
        risk_label = 'low'
    elif fault_prob < 0.60:
        risk_label = 'medium'
    else:
        risk_label = 'high'

    last = df.iloc[-1]
    result = {
        'device_id':          device_id,
        'device_type':        last['device_type'],
        'plant_id':           last['plant_id'],
        'latest_reading_ts':  _fmt_ts(last['timestamp']),
        'current_status':     last['status'],
        'fault_probability':  round(fault_prob, 4),
        'fault_risk_label':   risk_label,
        'anomaly_score':      round(anomaly_sc, 4),
        'context_rows_used':  len(df),
        'latency_ms':         round((time.time() - t0) * 1000, 1),
    }
    return result


def _write_prediction(result: dict):
    sql = text("""
        INSERT INTO rtia.fault_predictions
            (device_id, scored_at, latest_reading_ts, device_type, plant_id,
             current_status, fault_probability, fault_risk_label, anomaly_score)
        VALUES
            (:device_id, NOW(), :latest_reading_ts, :device_type, :plant_id,
             :current_status, :fault_probability, :fault_risk_label, :anomaly_score)
    """)
    with _engine.connect() as conn:
        conn.execute(sql, {
            'device_id':          result['device_id'],
            'latest_reading_ts':  result['latest_reading_ts'],
            'device_type':        result['device_type'],
            'plant_id':           result['plant_id'],
            'current_status':     result['current_status'],
            'fault_probability':  result['fault_probability'],
            'fault_risk_label':   result['fault_risk_label'],
            'anomaly_score':      result['anomaly_score'],
        })
        conn.commit()


# ── Request / response models ─────────────────────────────────────────────────

class BatchRequest(BaseModel):
    device_ids: list[str]
    write_to_cratedb: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get('/health')
def health():
    return {
        'status':       'ok',
        'model':        _clf['model_name'],
        'trained_at':   _clf['trained_at'],
        'roc_auc':      _clf['roc_auc'],
        'horizon':      f'{_clf["horizon"]} readings',
        'cratedb':      CRATEDB_ALCHEMY_URL,
    }


@app.get('/score/{device_id}')
def score_device(device_id: str, write: bool = True):
    """
    Score a single device.

    Fetches the last 50 readings from CrateDB, builds rolling features,
    returns fault_probability and anomaly_score.

    ?write=true (default) also writes the prediction to rtia.fault_predictions.
    """
    result = _score_device(device_id)
    if write:
        _write_prediction(result)
    return result


@app.post('/score/batch')
def score_batch(req: BatchRequest):
    """
    Score a list of devices in one call.

    Each device is queried independently so context windows do not overlap.
    Results are returned in the same order as the input list.
    """
    results = []
    errors  = []

    for device_id in req.device_ids:
        try:
            result = _score_device(device_id)
            if req.write_to_cratedb:
                _write_prediction(result)
            results.append(result)
        except HTTPException as e:
            errors.append({'device_id': device_id, 'error': e.detail})

    return {
        'scored':  len(results),
        'errors':  len(errors),
        'results': results,
        'failed':  errors,
    }


@app.get('/fleet/high-risk')
def fleet_high_risk(threshold: float = 0.6, limit: int = 20):
    """
    Return the most recently scored devices with fault_probability above threshold.

    Reads from rtia.fault_predictions — populated by /score or /score/batch calls.
    Joining back against rtia.iot_data would return the live sensor context.
    """
    sql = text("""
        SELECT
            device_id,
            device_type,
            plant_id,
            current_status,
            fault_probability,
            fault_risk_label,
            anomaly_score,
            scored_at
        FROM rtia.fault_predictions
        WHERE fault_probability >= :threshold
        ORDER BY fault_probability DESC
        LIMIT :lim
    """)

    with _engine.connect() as conn:
        rows   = conn.execute(sql, {'threshold': threshold, 'lim': limit}).fetchall()
        cols   = ['device_id', 'device_type', 'plant_id', 'current_status',
                  'fault_probability', 'fault_risk_label', 'anomaly_score', 'scored_at']

    devices = [dict(zip(cols, r)) for r in rows]
    for d in devices:
        d['scored_at'] = _fmt_ts(d['scored_at'])

    return {
        'threshold': threshold,
        'count':     len(rows),
        'devices':   devices,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    uvicorn.run('realtime_inference:app', host='0.0.0.0', port=8000, reload=False)
