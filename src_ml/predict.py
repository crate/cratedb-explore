"""
CrateDB Industrial IoT — Batch Scoring
=======================================
Loads the two trained models and scores a batch of sensor readings:
  - fault_probability   (predictive maintenance classifier)
  - anomaly_score       (Isolation Forest, higher = more anomalous)

Usage:
  python predict.py                        # scores last 2,000 rows of iot_demo_dataset.json
  python predict.py --input my_batch.json  # score any NDJSON file
  python predict.py --device DEVICE_0042   # filter to a single device

The output is written to:
  model/scored_batch.csv

To use the model programmatically (e.g. from a CrateDB UDF or API):

    import pickle, numpy as np
    with open('model/predictive_maintenance_model.pkl', 'rb') as f:
        p = pickle.load(f)

    # p['features'] is the ordered list of feature names
    # Build your feature row as a (1, len(features)) numpy array, then:
    prob = p['model'].predict_proba(x_new)[0, 1]
    print(f'Fault probability (next {p["horizon"]} readings): {prob:.1%}')
"""

import argparse
import json
import os
import pickle
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE        = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(BASE, '..', 'data', 'iot_demo_dataset.json')
CLF_FILE    = os.path.join(BASE, 'model', 'predictive_maintenance_model.pkl')
ISO_FILE    = os.path.join(BASE, 'model', 'anomaly_detector_model.pkl')
OUT_FILE    = os.path.join(BASE, 'model', 'scored_batch.csv')

STATUS_CODE = {'normal': 0, 'warning': 1, 'critical': 2, 'offline': -1}
WINDOWS     = [5, 10, 20]


# ─────────────────────────────────────────────────────────────────────────────
# Data loading / feature engineering (must match train_model.py exactly)
# ─────────────────────────────────────────────────────────────────────────────

def flatten_records(records: list) -> pd.DataFrame:
    """Normalise raw dataset records into the flat columns the feature code
    expects.

    The canonical dataset (data/iot_demo_dataset.json) is in Telegraf
    line-protocol shape — {hash_id, timestamp, name, tags{...}, fields{...}} —
    with device attributes under `tags` and numeric readings under `fields`.
    Older or hand-built inputs may already be flat (with a nested `metadata`
    dict). Handle both, and always expose a `firmware_version` column.
    """
    rows = []
    for rec in records:
        if 'tags' in rec or 'fields' in rec:
            tags   = rec.get('tags') or {}
            fields = rec.get('fields') or {}
            row = {**tags, **fields, 'timestamp': rec.get('timestamp')}
        else:
            row = dict(rec)

        if 'firmware_version' not in row:
            md = row.get('metadata')
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except (ValueError, TypeError):
                    md = {}
            if isinstance(md, dict) and 'firmware_version' in md:
                row['firmware_version'] = md['firmware_version']
            else:
                # Telegraf flattens nested metadata to tags['metadata_*']
                row['firmware_version'] = row.get('metadata_firmware_version', '0.0.0')
        rows.append(row)

    return pd.DataFrame(rows)


def build_features(df: pd.DataFrame, label_encoder) -> pd.DataFrame:
    df = df.copy()
    df['timestamp']    = pd.to_datetime(df['timestamp'])
    df = df.sort_values(['device_id', 'timestamp']).reset_index(drop=True)

    df['status_code']  = df['status'].map(STATUS_CODE).fillna(0).astype(int)
    df['hour']         = df['timestamp'].dt.hour
    df['day_of_week']  = df['timestamp'].dt.dayofweek

    grp = df.groupby('device_id', sort=False)

    for w in WINDOWS:
        df[f'metric_mean_{w}']   = grp['metric_value'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        df[f'metric_std_{w}']    = grp['metric_value'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).std().fillna(0))
        df[f'quality_mean_{w}']  = grp['quality_score'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        df[f'fault_rate_{w}']    = grp['status_code'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1)
                       .apply(lambda s: (s > 0).sum() / len(s), raw=True))

    # First reading(s) per device have no prior window — fill to stay NaN-free
    # and consistent with train_model.py (means -> own value; fault_rate -> 0).
    for w in WINDOWS:
        df[f'metric_mean_{w}']  = df[f'metric_mean_{w}'].fillna(df['metric_value'])
        df[f'quality_mean_{w}'] = df[f'quality_mean_{w}'].fillna(df['quality_score'])
        df[f'fault_rate_{w}']   = df[f'fault_rate_{w}'].fillna(0)

    df['metric_delta']  = grp['metric_value'].transform(lambda x: x.diff().fillna(0))
    df['quality_delta'] = grp['quality_score'].transform(lambda x: x.diff().fillna(0))
    df['fw_major']      = (
        df['firmware_version'].str.extract(r'^(\d+)', expand=False)
        .astype(float).fillna(1)
    )

    # Encode device_type — use the same encoder from training (handles unseen gracefully)
    known = set(label_encoder.classes_)
    df['device_type_safe'] = df['device_type'].apply(
        lambda x: x if x in known else label_encoder.classes_[0])
    df['device_type_enc'] = label_encoder.transform(df['device_type_safe'])

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(input_file: str, device_filter, n_rows: int):
    # Load models
    for path in [CLF_FILE, ISO_FILE]:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'{path} not found — run:  python train_model.py')

    with open(CLF_FILE, 'rb') as f:
        clf_p = pickle.load(f)
    with open(ISO_FILE, 'rb') as f:
        iso_p = pickle.load(f)

    print(f'Models loaded:')
    print(f'  Classifier:       {clf_p["model_name"]}  '
          f'(ROC-AUC on test set: {clf_p.get("roc_auc", "n/a")})')
    print(f'  Anomaly detector: {iso_p["model_name"]}')
    print(f'  Fault horizon:    {clf_p["horizon"]} readings')

    # Load batch
    print(f'\nLoading data from {os.path.basename(input_file)} ...')
    records = []
    with open(input_file, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    df = flatten_records(records)

    if device_filter:
        df = df[df['device_id'] == device_filter]
        if df.empty:
            raise ValueError(f'Device {device_filter} not found in dataset.')
        print(f'  Filtered to device {device_filter}: {len(df)} rows')
    else:
        # Sample last n_rows per device to keep context for rolling features
        # but cap total for speed
        df['timestamp_parsed'] = pd.to_datetime(df['timestamp'])
        df = (df.sort_values('timestamp_parsed')
                .groupby('device_id', sort=False)
                .tail(50)            # keep last 50 readings per device for rolling context
                .reset_index(drop=True))
        print(f'  Using last 50 readings per device: {len(df):,} rows')

    # Build features
    df = build_features(df, clf_p['label_encoder'])

    # Score: predictive maintenance
    X_clf = df[clf_p['features']].values
    df['fault_probability']  = clf_p['model'].predict_proba(X_clf)[:, 1]
    df['fault_risk_label']   = pd.cut(
        df['fault_probability'],
        bins=[0, 0.25, 0.60, 1.0],
        labels=['low', 'medium', 'high']
    )

    # Score: anomaly
    X_iso = df[iso_p['features']].values
    df['anomaly_score'] = -iso_p['model'].score_samples(X_iso)   # higher = more anomalous

    # Output
    out_cols = [
        'device_id', 'device_type', 'plant_id', 'timestamp',
        'metric_value', 'metric_unit', 'quality_score', 'status',
        'fault_probability', 'fault_risk_label', 'anomaly_score',
    ]
    out = df[out_cols].copy()
    out['timestamp'] = out['timestamp'].astype(str)
    out.to_csv(OUT_FILE, index=False)

    # Print summary
    print(f'\n── Scoring summary ─────────────────────────────────────────────────')
    print(f'Rows scored: {len(out):,}')
    print(f'\nFault risk distribution:')
    print(out['fault_risk_label'].value_counts().to_string())
    print(f'\nTop 10 highest fault-probability readings:')
    top10 = out.nlargest(10, 'fault_probability')[
        ['device_id', 'device_type', 'plant_id', 'timestamp',
         'status', 'fault_probability', 'anomaly_score']
    ]
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 120)
    print(top10.to_string(index=False))

    print(f'\nScored batch written -> {OUT_FILE}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Score IoT sensor readings.')
    parser.add_argument('--input',  default=DATA_FILE,
                        help='Path to NDJSON input file (default: iot_demo_dataset.json)')
    parser.add_argument('--device', default=None,
                        help='Score only this device_id')
    parser.add_argument('--rows',   type=int, default=2000,
                        help='Max rows to score (default: 2000)')
    args = parser.parse_args()
    main(args.input, args.device, args.rows)
