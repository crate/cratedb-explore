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
CrateDB Industrial IoT - Predictive Maintenance Model
======================================================
Two models trained from the same dataset:

  Model 1 - Predictive Maintenance (binary classifier)
    Task:    Predict whether a device will enter warning or critical state
             within the next HORIZON sensor readings.
    Inputs:  Current metric_value, quality_score, rolling trends, time features,
             device type, recent fault rate.
    Output:  fault_probability  (0.0 - 1.0)

  Model 2 - Anomaly Detector (Isolation Forest, unsupervised)
    Task:    Score every reading on how anomalous it is relative to the fleet.
    Output:  anomaly_score  (higher = more anomalous)

Requires:  pip install scikit-learn pandas numpy
Optional:  pip install xgboost   (preferred; falls back to GradientBoosting)

Outputs (written to model/ directory):
  predictive_maintenance_model.pkl  - trained classifier + metadata
  anomaly_detector_model.pkl        - Isolation Forest
  feature_importance.csv            - ranked feature importances
  test_predictions.csv              - scored test-set rows for inspection
"""

# Annotations are lazy (PEP 563) so the pandas/sklearn names used in function
# signatures below don't force those heavy imports at module-load time.
from __future__ import annotations

import json
import os
import pickle
import threading
import time
import urllib.request
import warnings

# NOTE: numpy / pandas / scikit-learn are imported lazily by import_heavy_libs()
# rather than here. On a cold filesystem cache that import costs ~30s, and
# deferring it lets the first-run S3 download run concurrently (see main()).

warnings.filterwarnings('ignore')

# -- Config --------------------------------------------------------------------
BASE       = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(BASE, '..', 'data', 'iot_demo_dataset.json')
S3_BUCKET  = 'iot2-601357753311-eu-west-1-an.s3.eu-west-1.amazonaws.com'
DATA_URL   = f'https://{S3_BUCKET}/iot_demo_dataset.json'
MODEL_DIR  = os.path.join(BASE, 'model')
os.makedirs(MODEL_DIR, exist_ok=True)

HORIZON    = 5      # predict fault within next N readings per device
WINDOWS    = [5, 10, 20]   # rolling window sizes for feature engineering
TEST_SIZE  = 0.2    # fraction of devices held out for evaluation
SEED       = 42


def import_heavy_libs() -> None:
    """Import numpy / pandas / scikit-learn and bind them at module scope.

    Kept out of the top-level imports so it can be triggered explicitly after
    the S3 dataset download starts — on a cold filesystem cache this import
    alone takes ~30s, and overlapping it with the download halves first-run
    wall time. Idempotent: re-importing already-loaded modules is cheap.
    """
    global np, pd
    global GradientBoostingClassifier, IsolationForest
    global classification_report, confusion_matrix, roc_auc_score
    global train_test_split, LabelEncoder

    import numpy as np
    import pandas as pd
    from sklearn.ensemble import GradientBoostingClassifier, IsolationForest
    from sklearn.metrics import (classification_report, confusion_matrix,
                                 roc_auc_score)
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import LabelEncoder


# -----------------------------------------------------------------------------
# 1. LOAD DATA
# -----------------------------------------------------------------------------

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


def ensure_dataset(path: str) -> None:
    """Download the dataset from S3 if it isn't already present locally.

    The 240 MB iot_demo_dataset.json is gitignored, so it's auto-fetched on
    first use. Streams to a temp file and renames on success so an interrupted
    download never leaves a truncated file in place.
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


def load_data(path: str) -> pd.DataFrame:
    ensure_dataset(path)
    print(f'Loading {os.path.basename(path)} ...')
    t0 = time.time()

    records = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    df = flatten_records(records)
    print(f'  {len(df):,} rows  |  {df["device_id"].nunique()} devices  '
          f'|  {time.time() - t0:.1f}s')

    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values(['device_id', 'timestamp']).reset_index(drop=True)

    return df


# -----------------------------------------------------------------------------
# 2. FEATURE ENGINEERING
# -----------------------------------------------------------------------------

STATUS_CODE = {'normal': 0, 'warning': 1, 'critical': 2, 'offline': -1}

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    print('Engineering features ...')

    df = df.copy()
    df['status_code'] = df['status'].map(STATUS_CODE).fillna(0).astype(int)

    # Time features
    df['hour']         = df['timestamp'].dt.hour
    df['day_of_week']  = df['timestamp'].dt.dayofweek

    grp = df.groupby('device_id', sort=False)

    # Rolling stats - shift(1) so we never use the current reading as a feature
    for w in WINDOWS:
        df[f'metric_mean_{w}']   = grp['metric_value'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        df[f'metric_std_{w}']    = grp['metric_value'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).std().fillna(0))
        df[f'quality_mean_{w}']  = grp['quality_score'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        # Fraction of last w readings that were warning or critical
        df[f'fault_rate_{w}']    = grp['status_code'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1)
                       .apply(lambda s: (s > 0).sum() / len(s), raw=True))

    # The first reading(s) per device have no prior window. Fill those gaps so
    # features are NaN-free (XGBoost tolerates NaN; GradientBoosting does not,
    # and the batch/realtime scorers must agree on identical values):
    #   rolling means -> the reading's own value;  fault_rate -> 0.
    for w in WINDOWS:
        df[f'metric_mean_{w}']  = df[f'metric_mean_{w}'].fillna(df['metric_value'])
        df[f'quality_mean_{w}'] = df[f'quality_mean_{w}'].fillna(df['quality_score'])
        df[f'fault_rate_{w}']   = df[f'fault_rate_{w}'].fillna(0)

    # Point-to-point delta (rate of change)
    df['metric_delta']   = grp['metric_value'].transform(lambda x: x.diff().fillna(0))
    df['quality_delta']  = grp['quality_score'].transform(lambda x: x.diff().fillna(0))

    # Firmware major version (numeric)
    df['fw_major'] = (
        df['firmware_version']
        .str.extract(r'^(\d+)', expand=False)
        .astype(float)
        .fillna(1)
    )

    # Device type encoding (fit on full set)
    le = LabelEncoder()
    df['device_type_enc'] = le.fit_transform(df['device_type'])

    return df, le


# -----------------------------------------------------------------------------
# 3. TARGET: FAULT WITHIN NEXT HORIZON READINGS
# -----------------------------------------------------------------------------

def make_target(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """
    For each reading at time t, the target is 1 if any reading in
    [t+1, t+horizon] for the same device has status warning or critical.

    rolling(horizon).max() looks backward; shift(-horizon) aligns it forward:
    at row t, the result is max(status_code[t+1 .. t+horizon]).
    """
    print(f'Creating target: fault within next {horizon} readings ...')

    grp = df.groupby('device_id', sort=False)
    df['target'] = (
        grp['status_code']
        .transform(lambda x: x.rolling(horizon, min_periods=1).max().shift(-horizon))
        .fillna(0)
        .gt(0)
        .astype(int)
    )

    # Drop the last HORIZON rows per device (no valid lookahead label).
    # cumcount(ascending=False) = 0 for the last row, 1 for the second-to-last, etc.
    # Rows where that count >= horizon are outside the tail window - keep them.
    keep = df.groupby('device_id', sort=False).cumcount(ascending=False) >= horizon
    df = df[keep].reset_index(drop=True)

    pos = df['target'].sum()
    neg = (df['target'] == 0).sum()
    ratio = f'{neg/pos:.1f}:1' if pos else 'n/a (no fault_incoming rows)'
    print(f'  Rows after trim: {len(df):,}  |  healthy: {neg:,}  |  fault_incoming: {pos:,}  '
          f'|  imbalance ratio: {ratio}')
    return df


# -----------------------------------------------------------------------------
# 4. TRAIN / TEST SPLIT (by device - no leakage)
# -----------------------------------------------------------------------------

FEATURES = (
    ['metric_value', 'quality_score', 'hour', 'day_of_week',
     'device_type_enc', 'metric_delta', 'quality_delta', 'fw_major']
    + [f'metric_mean_{w}'   for w in WINDOWS]
    + [f'metric_std_{w}'    for w in WINDOWS]
    + [f'quality_mean_{w}'  for w in WINDOWS]
    + [f'fault_rate_{w}'    for w in WINDOWS]
)


def split_by_device(df: pd.DataFrame, test_size: float, seed: int):
    devices = df['device_id'].unique()
    train_devs, test_devs = train_test_split(devices, test_size=test_size, random_state=seed)
    train_mask = df['device_id'].isin(train_devs)
    test_mask  = df['device_id'].isin(test_devs)

    X_train = df.loc[train_mask, FEATURES].values
    y_train = df.loc[train_mask, 'target'].values
    X_test  = df.loc[test_mask,  FEATURES].values
    y_test  = df.loc[test_mask,  'target'].values

    print(f'  Train: {X_train.shape[0]:,} rows  ({len(train_devs)} devices)  '
          f'|  Test: {X_test.shape[0]:,} rows  ({len(test_devs)} devices)')

    return X_train, y_train, X_test, y_test, df[test_mask].copy()


# -----------------------------------------------------------------------------
# 5. MODEL 1 - PREDICTIVE MAINTENANCE CLASSIFIER
# -----------------------------------------------------------------------------

def train_classifier(X_train, y_train, X_test, y_test, test_df):
    print('\nTraining classifier ...')
    t0 = time.time()

    pos = (y_train == 1).sum()
    neg = (y_train == 0).sum()

    if pos == 0 or neg == 0:
        only = 'fault_incoming' if neg == 0 else 'healthy'
        raise SystemExit(
            f'\nTraining split has only one class ({only}) — cannot fit a '
            f'classifier (needs both healthy and fault_incoming). This usually '
            f'means the data window is too narrow; see the --days guidance above.')

    try:
        from xgboost import XGBClassifier
        clf = XGBClassifier(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.08,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=neg / pos,   # compensate for class imbalance
            random_state=SEED,
            n_jobs=-1,
            eval_metric='logloss',
            verbosity=0,
        )
        model_name = 'XGBoost'
    except Exception as exc:
        # Not installed (ImportError), or installed but the native lib won't load
        # (XGBoostError — e.g. missing libomp on macOS: `brew install libomp`).
        # Either way, fall back to scikit-learn's GradientBoosting.
        print(f'  xgboost unavailable ({type(exc).__name__}); using GradientBoosting fallback')
        clf = GradientBoostingClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.08,
            subsample=0.8,
            min_samples_leaf=50,
            random_state=SEED,
        )
        model_name = 'GradientBoosting (install xgboost + libomp for better performance)'

    clf.fit(X_train, y_train)
    print(f'  {model_name}  -  trained in {time.time() - t0:.1f}s')

    # Evaluate
    y_pred  = clf.predict(X_test)
    y_proba = clf.predict_proba(X_test)[:, 1]

    print('\n-- Evaluation ------------------------------------------------------')
    print(classification_report(y_test, y_pred, target_names=['healthy', 'fault_incoming']))
    print(f'ROC-AUC:          {roc_auc_score(y_test, y_proba):.4f}')
    print('Confusion matrix:')
    cm = confusion_matrix(y_test, y_pred)
    print(f'  TN={cm[0,0]:,}  FP={cm[0,1]:,}')
    print(f'  FN={cm[1,0]:,}  TP={cm[1,1]:,}')

    # Feature importances
    fi = pd.DataFrame({'feature': FEATURES, 'importance': clf.feature_importances_})
    fi = fi.sort_values('importance', ascending=False)
    fi_path = os.path.join(MODEL_DIR, 'feature_importance.csv')
    fi.to_csv(fi_path, index=False)
    print(f'\nTop 10 features:')
    print(fi.head(10).to_string(index=False))

    # Save test predictions
    test_df = test_df.copy()
    test_df['fault_probability'] = y_proba
    test_df['predicted_fault']   = y_pred
    test_df['actual_fault']      = y_test
    pred_cols = ['device_id', 'device_type', 'plant_id', 'timestamp',
                 'metric_value', 'quality_score', 'status',
                 'fault_probability', 'predicted_fault', 'actual_fault']
    pred_path = os.path.join(MODEL_DIR, 'test_predictions.csv')
    test_df[pred_cols].to_csv(pred_path, index=False)
    print(f'\nTest predictions -> {pred_path}')

    # Save model
    payload = {
        'model':         clf,
        'model_name':    model_name,
        'features':      FEATURES,
        'label_encoder': None,   # filled in by caller
        'horizon':       HORIZON,
        'trained_at':    pd.Timestamp.now().isoformat(),
        'roc_auc':       round(roc_auc_score(y_test, y_proba), 4),
    }
    return payload


# -----------------------------------------------------------------------------
# 6. MODEL 2 - ANOMALY DETECTOR (Isolation Forest)
# -----------------------------------------------------------------------------

ANOMALY_FEATURES = ['metric_value', 'quality_score', 'device_type_enc',
                    'hour', 'day_of_week', 'metric_delta', 'quality_delta']

def train_anomaly_detector(df: pd.DataFrame):
    print('\nTraining anomaly detector (Isolation Forest) ...')
    t0 = time.time()

    # Train only on "normal" readings so the model learns the healthy baseline
    normal_df = df[df['status'] == 'normal'].sample(
        n=min(80_000, (df['status'] == 'normal').sum()), random_state=SEED)

    X_normal = normal_df[ANOMALY_FEATURES].values

    iso = IsolationForest(
        n_estimators=200,
        contamination=0.05,   # expected anomaly fraction in production data
        max_samples=min(10_000, len(X_normal)),
        random_state=SEED,
        n_jobs=-1,
    )
    iso.fit(X_normal)
    print(f'  Isolation Forest trained in {time.time() - t0:.1f}s  '
          f'(fitted on {len(X_normal):,} normal readings)')

    # Spot check: anomaly scores on test sample by status
    sample = df.sample(n=min(20_000, len(df)), random_state=SEED)
    scores = -iso.score_samples(sample[ANOMALY_FEATURES].values)   # higher = more anomalous
    sample = sample.copy()
    sample['anomaly_score'] = scores

    print('\n  Mean anomaly score by status (higher = more anomalous):')
    print(sample.groupby('status')['anomaly_score']
          .mean().sort_values(ascending=False).to_string())

    payload = {
        'model':            iso,
        'model_name':       'IsolationForest',
        'features':         ANOMALY_FEATURES,
        'trained_at':       pd.Timestamp.now().isoformat(),
    }
    return payload


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main(cratedb_url=None, days=None, input_file=DATA_FILE):
    print('=' * 70)
    print('CrateDB Industrial IoT - Predictive Maintenance Training Pipeline')
    print('=' * 70)
    t_total = time.time()

    if cratedb_url:
        # CrateDB source: no file to download, so just pay the import cost then
        # pull the per-reading history straight into a DataFrame.
        print('Importing pandas + scikit-learn (~30s on a cold cache) ...')
        import_heavy_libs()

        import data_source
        engine = data_source.make_engine(cratedb_url)

        if days is None:
            # Unusual default: anchor the window to the DATA, not wall-clock
            # NOW(). The demo dataset's timestamps can predate "now", which
            # makes a NOW()-relative window return nothing. The query filters on
            # the last 90 days relative to the latest reading in the table.
            print('  [unusual default] no --days given: pulling the last 90 days '
                  'relative to the LATEST reading in the DB, not wall-clock now '
                  '(WHERE "timestamp" >= (SELECT MAX("timestamp") ...) - INTERVAL 90 DAY).')
            print('  (pass --days N for a NOW()-relative window, or --days 0 for all history.)')
            window = 'latest 90 days'
        else:
            window = f'last {days} days' if days > 0 else 'all history'

        print(f'Querying CrateDB at {cratedb_url} ({window}) ...')
        df = data_source.load_training_frame(engine, days=days)
        if df.empty:
            raise SystemExit('CrateDB returned no rows — check the URL, the '
                             'rtia.iot_data table, and the --days window.')
        print(f'  {len(df):,} rows  |  {df["device_id"].nunique()} devices '
              f'loaded from CrateDB')
    else:
        # File source. Fetch the dataset (if missing) and import the heavy ML
        # stack concurrently: the S3 download is network-bound and needs only
        # stdlib, while the pandas/sklearn import is CPU/disk-bound and costs
        # ~30s on a cold cache. Running the download in a background thread
        # overlaps the two first-run waits instead of serialising them.
        dl_error = []

        def _download():
            try:
                # Only the canonical dataset is auto-fetched; a missing custom
                # --input is left to fail as a genuine error.
                if os.path.abspath(input_file) == os.path.abspath(DATA_FILE):
                    ensure_dataset(input_file)
            except BaseException as exc:   # surfaced on the main thread below
                dl_error.append(exc)

        downloader = threading.Thread(target=_download)
        downloader.start()

        print('Importing pandas + scikit-learn (~30s on a cold cache) ...')
        import_heavy_libs()

        downloader.join()
        if dl_error:
            raise dl_error[0]

        df = load_data(input_file)

    # Feature engineering
    df, label_encoder = engineer_features(df)

    # Target
    df = make_target(df, HORIZON)

    # Guard: the classifier needs both classes. Fail clearly here instead of
    # deep inside xgboost.fit() ("Invalid classes inferred from unique values
    # of `y`"). The default CrateDB window (latest 90 days) is the usual culprit:
    # if the most recent rows are an injected-fault batch dated well after the
    # historical data, the window sees only faults -> a single-class target.
    classes = sorted(int(c) for c in df['target'].unique())
    if len(classes) < 2:
        only = 'fault_incoming' if classes == [1] else 'healthy'
        if cratedb_url and days is None:
            hint = ('Re-run with --days 0 (all history) or a wider --days N. '
                    'The default window pulls the last 90 days relative to the '
                    'LATEST reading, so a fault batch dated far after the '
                    'historical data leaves only faults in range — check that '
                    'rtia.iot_data is one contiguous timeline.')
        elif cratedb_url:
            hint = 'Re-run with a wider --days window or --days 0 (all history).'
        else:
            hint = 'Check that the input dataset spans both healthy and faulty readings.'
        raise SystemExit(
            f'\nTraining target has only one class ({only}); the classifier needs '
            f'both healthy and fault_incoming examples.\n  {hint}')

    # Split
    print('\nSplitting by device ...')
    X_train, y_train, X_test, y_test, test_df = split_by_device(df, TEST_SIZE, SEED)

    # Model 1: classifier
    clf_payload = train_classifier(X_train, y_train, X_test, y_test, test_df)
    clf_payload['label_encoder'] = label_encoder

    clf_path = os.path.join(MODEL_DIR, 'predictive_maintenance_model.pkl')
    with open(clf_path, 'wb') as f:
        pickle.dump(clf_payload, f)
    print(f'\nClassifier saved -> {clf_path}')

    # Model 2: anomaly detector
    iso_payload = train_anomaly_detector(df)
    iso_path = os.path.join(MODEL_DIR, 'anomaly_detector_model.pkl')
    with open(iso_path, 'wb') as f:
        pickle.dump(iso_payload, f)
    print(f'Anomaly detector saved -> {iso_path}')

    print(f'\nTotal time: {time.time() - t_total:.1f}s')
    print('\nNext steps:')
    print('  python predict.py          - score a batch of readings')
    print('  model/test_predictions.csv - inspect scored test rows')
    print('  model/feature_importance.csv - see which signals matter most')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Train the predictive-maintenance and anomaly-detection models.')
    parser.add_argument(
        '--input', default=DATA_FILE,
        help='NDJSON dataset path (default: data/iot_demo_dataset.json)')
    parser.add_argument(
        '--cratedb-url', default=os.getenv('CRATEDB_ALCHEMY_URL'),
        help='Train from CrateDB instead of the file, e.g. crate://localhost:4200 '
             '(credentials via CRATEDB_USER / CRATEDB_PASSWORD env). Env: CRATEDB_ALCHEMY_URL')
    parser.add_argument(
        '--days', type=int, default=None,
        help='With --cratedb-url: pull the last N days, NOW()-relative (0 = all '
             'history). Default: the last 90 days relative to the latest reading '
             'in the DB (not wall-clock now) — logged at run time.')
    args = parser.parse_args()

    if args.cratedb_url and os.path.abspath(args.input) != os.path.abspath(DATA_FILE):
        parser.error('specify either --input or --cratedb-url, not both')

    main(cratedb_url=args.cratedb_url, days=args.days, input_file=args.input)
