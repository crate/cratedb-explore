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

# Annotations are lazy (PEP 563) so the pandas names used in function
# signatures below don't force that heavy import at module-load time.
from __future__ import annotations

import argparse
import json
import os
import pickle
import threading
import time
import urllib.request
import warnings

# NOTE: numpy / pandas are imported lazily by import_heavy_libs() rather than
# here — on a cold filesystem cache that import (plus the sklearn/xgboost pulled
# in when unpickling the models) is slow, and deferring it lets a first-run S3
# download run concurrently (see main()).

warnings.filterwarnings('ignore')

BASE        = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(BASE, '..', 'data', 'iot_demo_dataset.json')
S3_BUCKET   = 'iot2-601357753311-eu-west-1-an.s3.eu-west-1.amazonaws.com'
DATA_URL    = f'https://{S3_BUCKET}/iot_demo_dataset.json'
CLF_FILE    = os.path.join(BASE, 'model', 'predictive_maintenance_model.pkl')
ISO_FILE    = os.path.join(BASE, 'model', 'anomaly_detector_model.pkl')
OUT_FILE    = os.path.join(BASE, 'model', 'scored_batch.csv')

STATUS_CODE = {'normal': 0, 'warning': 1, 'critical': 2, 'offline': -1}
WINDOWS     = [5, 10, 20]


def import_heavy_libs() -> None:
    """Import numpy / pandas and bind them at module scope.

    Kept out of the top-level imports so it can run after the S3 dataset
    download starts — overlapping the two first-run waits instead of
    serialising them (see main()). Idempotent: re-importing is cheap.
    """
    global np, pd
    import numpy as np
    import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Data loading / feature engineering (must match train_model.py exactly)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_dataset(path: str) -> None:
    """Download the canonical dataset from S3 if it isn't already present.

    Only the shared iot_demo_dataset.json (240 MB, gitignored) is auto-fetched;
    a missing user-supplied --input is left to fail as a genuine error. Streams
    to a temp file and renames on success so an interrupted download never
    leaves a truncated file in place.
    """
    if os.path.exists(path):
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f'{os.path.basename(path)} not found — downloading from S3 ...')
    print(f'  {DATA_URL}')
    t0 = time.time()
    tmp = path + '.part'
    try:
        def _progress(blocks, block_size, total):
            done = blocks * block_size
            if total > 0:
                pct = min(100, done * 100 // total)
                print(f'\r  {done >> 20:,} / {total >> 20:,} MiB  ({pct}%)',
                      end='', flush=True)
            else:
                print(f'\r  {done >> 20:,} MiB', end='', flush=True)

        urllib.request.urlretrieve(DATA_URL, tmp, reporthook=_progress)
        print()
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    print(f'  downloaded in {time.time() - t0:.1f}s')


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

def _load_models():
    """Unpickle both models and print a short summary."""
    with open(CLF_FILE, 'rb') as f:
        clf_p = pickle.load(f)
    with open(ISO_FILE, 'rb') as f:
        iso_p = pickle.load(f)
    print(f'Models loaded:')
    print(f'  Classifier:       {clf_p["model_name"]}  '
          f'(ROC-AUC on test set: {clf_p.get("roc_auc", "n/a")})')
    print(f'  Anomaly detector: {iso_p["model_name"]}')
    print(f'  Fault horizon:    {clf_p["horizon"]} readings')
    return clf_p, iso_p


def main(input_file: str, device_filter, n_rows: int, cratedb_url=None):
    print('CrateDB Industrial IoT - Batch Scoring')

    # Check the models exist before doing any slow work (no point fetching data
    # only to fail because training hasn't been run).
    for path in [CLF_FILE, ISO_FILE]:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'{path} not found — run:  python train_model.py')

    if cratedb_url:
        # CrateDB source: import the stack, load the models, then pull the last
        # 50 readings per device (optionally one device) straight from the DB.
        print('Importing pandas + loading models (slow on a cold cache) ...')
        import_heavy_libs()
        clf_p, iso_p = _load_models()

        import data_source
        print(f'\nQuerying CrateDB at {cratedb_url} ...')
        engine = data_source.make_engine(cratedb_url)
        df = data_source.load_scoring_frame(engine, device=device_filter,
                                            context_rows=50)
        if df.empty:
            raise SystemExit(
                'CrateDB returned no rows — check the URL'
                + (f' and that device {device_filter} exists' if device_filter else '')
                + '.')
        print(f'  {len(df):,} rows  |  {df["device_id"].nunique()} devices '
              f'(last 50 readings each) loaded from CrateDB')
    else:
        # File source. Fetch the dataset (if it's the default input and missing)
        # concurrently with importing the heavy stack and unpickling the models:
        # the download is network-bound and needs only stdlib, while the
        # import/unpickle is CPU/disk-bound and slow on a cold cache. A
        # background thread overlaps the two first-run waits.
        dl_error = []

        def _download():
            try:
                if os.path.abspath(input_file) == os.path.abspath(DATA_FILE):
                    ensure_dataset(input_file)
            except BaseException as exc:   # surfaced on the main thread below
                dl_error.append(exc)

        downloader = threading.Thread(target=_download)
        downloader.start()

        print('Importing pandas + loading models (slow on a cold cache) ...')
        import_heavy_libs()
        clf_p, iso_p = _load_models()

        downloader.join()
        if dl_error:
            raise dl_error[0]

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
            # Last readings per device to keep context for rolling features
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
    parser.add_argument('--cratedb-url', default=os.getenv('CRATEDB_ALCHEMY_URL'),
                        help='Score from CrateDB instead of the file, e.g. '
                             'crate://localhost:4200 (credentials via CRATEDB_USER / '
                             'CRATEDB_PASSWORD env). Env: CRATEDB_ALCHEMY_URL')
    args = parser.parse_args()

    if args.cratedb_url and os.path.abspath(args.input) != os.path.abspath(DATA_FILE):
        parser.error('specify either --input or --cratedb-url, not both')

    main(args.input, args.device, args.rows, cratedb_url=args.cratedb_url)
