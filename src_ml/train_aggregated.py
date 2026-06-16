"""
CrateDB Industrial IoT — Scenario 2: train on DB-side feature aggregation
=========================================================================
Instead of pulling 500,000 raw readings into pandas and computing rolling
features there (what train_model.py does), this script pushes the heavy work
into CrateDB: it asks the database for one pre-aggregated row per device per
time window — metric mean/std, quality mean, fault rate — and trains a compact
classifier on that. pandas only ever sees the small aggregated result, so it is
far faster and uses a fraction of the RAM. This is "Scenario 2" in ML_GUIDE.md.

What it predicts
----------------
The target is whether the *next* window has a fault. The guide's aggregation
emits `had_fault` for each window; using that as a same-window target would be
trivially leaky (it is derived from the same statuses as `fault_rate`), so we
shift it forward one window per device and predict it from the current window's
aggregates. This mirrors train_model.py's "fault within the next N readings".

Connection (same convention as train_model.py / predict.py)
-----------------------------------------------------------
  CRATE_USER / CRATE_PASSWORD   credentials (kept out of argv)
  --cratedb-url / CRATEDB_URL   host, e.g. crate://localhost:4200

Usage
-----
  export CRATE_USER=... CRATE_PASSWORD=...
  python train_aggregated.py --cratedb-url crate://localhost:4200
  python train_aggregated.py --cratedb-url crate://localhost:4200 --window '6 hours'

Requires:  pip install -r requirements.txt   (core + the CrateDB tier)
Output:    model/aggregated_maintenance_model.pkl
"""

import argparse
import os
import pickle
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

import data_source

BASE      = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE, 'model')
OUT_FILE  = os.path.join(MODEL_DIR, 'aggregated_maintenance_model.pkl')
SEED      = 42

# Aggregates returned per window (from CrateDB) plus the time/category features
# we derive from window_start and device_type. fault_rate is the current
# window's fault fraction — a legitimate predictor of the *next* window.
NUMERIC_FEATURES = ['metric_mean', 'metric_std', 'quality_mean', 'fault_rate']
FEATURES = NUMERIC_FEATURES + ['device_type_enc', 'window_dow', 'window_hour']


def build_features(df: pd.DataFrame):
    """Add the encoded/time features and the next-window target. Returns
    (X, y, feature_list, label_encoder)."""
    df = df.copy()

    # STDDEV is NULL for single-reading windows; treat "no variation" as 0.
    df['metric_std'] = df['metric_std'].astype(float).fillna(0.0)

    df['window_dow']  = df['window_start'].dt.dayofweek
    df['window_hour'] = df['window_start'].dt.hour

    le = LabelEncoder()
    df['device_type_enc'] = le.fit_transform(df['device_type'].astype(str))

    # Target: did the NEXT window (same device) contain a fault? Shifting per
    # device avoids leaking across device boundaries; the last window per device
    # has no successor and is dropped.
    df['target'] = (df.groupby('device_id', sort=False)['had_fault']
                      .shift(-1))
    df = df.dropna(subset=['target'])
    df['target'] = df['target'].astype(int)

    return df[FEATURES], df['target'], df


def train(X_train, y_train):
    pos = int((y_train == 1).sum())
    neg = int((y_train == 0).sum())
    t0 = time.time()
    try:
        from xgboost import XGBClassifier
        clf = XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.08,
            subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=neg / max(pos, 1),   # compensate for imbalance
            random_state=SEED, n_jobs=-1, eval_metric='logloss', verbosity=0,
        )
        name = 'XGBoost'
    except Exception as exc:
        # Not installed, or native lib won't load (e.g. missing libomp on macOS).
        print(f'  xgboost unavailable ({type(exc).__name__}); using GradientBoosting fallback')
        clf = GradientBoostingClassifier(
            n_estimators=200, max_depth=5, learning_rate=0.08,
            subsample=0.8, min_samples_leaf=50, random_state=SEED,
        )
        name = 'GradientBoosting (install xgboost + libomp for better performance)'

    clf.fit(X_train, y_train)
    print(f'  {name}  -  trained in {time.time() - t0:.1f}s')
    return clf, name


def main(cratedb_url, window, days):
    print('=' * 70)
    print('CrateDB Industrial IoT - Aggregated (Scenario 2) Training')
    print('=' * 70)
    t_total = time.time()
    os.makedirs(MODEL_DIR, exist_ok=True)

    engine = data_source.make_engine(cratedb_url)
    window_desc = 'all history' if days is not None and days <= 0 else (
        f'last {days} days' if days else 'latest 90 days')
    print(f'Aggregating in CrateDB at {cratedb_url} '
          f'({window} windows, {window_desc}) ...')
    df = data_source.load_aggregated_frame(engine, window=window, days=days)
    if df.empty:
        raise SystemExit('CrateDB returned no rows — check the URL, the '
                         'rtia.iot_data table, and the --days window.')
    print(f'  {len(df):,} windows  |  {df["device_id"].nunique()} devices  '
          f'|  avg {df["n_readings"].mean():.1f} readings/window')

    X, y, full = build_features(df)
    print(f'  {len(X):,} training rows after next-window target shift  |  '
          f'fault_incoming: {int(y.sum()):,} ({y.mean():.1%})')

    # Split by device so no device appears in both train and test.
    devices = full['device_id'].unique()
    train_dev, test_dev = train_test_split(devices, test_size=0.2, random_state=SEED)
    train_mask = full['device_id'].isin(set(train_dev)).values
    X_train, y_train = X[train_mask], y[train_mask]
    X_test,  y_test  = X[~train_mask], y[~train_mask]
    print(f'\nSplitting by device ...')
    print(f'  Train: {len(X_train):,} rows ({len(train_dev)} devices)  |  '
          f'Test: {len(X_test):,} rows ({len(test_dev)} devices)')

    clf, model_name = train(X_train, y_train)

    y_pred  = clf.predict(X_test)
    y_proba = clf.predict_proba(X_test)[:, 1]
    print('\n-- Evaluation ------------------------------------------------------')
    print(classification_report(y_test, y_pred, target_names=['healthy', 'fault_incoming']))
    roc = roc_auc_score(y_test, y_proba) if y_test.nunique() > 1 else float('nan')
    print(f'ROC-AUC:          {roc:.4f}')
    tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()
    print(f'Confusion matrix:\n  TN={tn:,}  FP={fp:,}\n  FN={fn:,}  TP={tp:,}')

    importances = getattr(clf, 'feature_importances_', None)
    if importances is not None:
        print('\nFeature importances:')
        ranked = sorted(zip(FEATURES, importances), key=lambda t: t[1], reverse=True)
        for feat, imp in ranked:
            print(f'  {feat:<16} {imp:.4f}')

    payload = {
        'model':        clf,
        'model_name':   model_name,
        'features':     FEATURES,
        'window':       window,
        'target':       'had_fault in the next window',
        'roc_auc':      round(float(roc), 4) if roc == roc else None,
        'trained_at':   pd.Timestamp.now().isoformat(),
    }
    with open(OUT_FILE, 'wb') as f:
        pickle.dump(payload, f)
    print(f'\nModel saved -> {OUT_FILE}')
    print(f'Total time: {time.time() - t_total:.1f}s')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Train a predictive-maintenance model on CrateDB-side '
                    'window aggregates (Scenario 2).')
    parser.add_argument(
        '--cratedb-url', default=os.getenv('CRATEDB_URL'), required=False,
        help='CrateDB host, e.g. crate://localhost:4200 (credentials via '
             'CRATE_USER / CRATE_PASSWORD env). Env: CRATEDB_URL')
    parser.add_argument(
        '--window', default='1 day',
        help="Aggregation window as a CrateDB INTERVAL, e.g. '1 day', '6 hours'. "
             "Default: '1 day' (the demo data is hourly, so sub-day windows "
             "barely aggregate).")
    parser.add_argument(
        '--days', type=int, default=None,
        help='Limit source rows to the last N days, NOW()-relative (0 = all '
             'history). Default: last 90 days relative to the latest reading.')
    args = parser.parse_args()

    if not args.cratedb_url:
        parser.error('a CrateDB URL is required — pass --cratedb-url or set CRATEDB_URL '
                     '(this script has no file-based mode).')

    main(args.cratedb_url, args.window, args.days)
