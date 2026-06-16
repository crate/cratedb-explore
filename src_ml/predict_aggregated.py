"""
CrateDB Industrial IoT — Scenario 2: score with the aggregated model
====================================================================
Companion to train_aggregated.py. It pulls the same CrateDB-side window
aggregates (one row per device per time window), then scores each window with
the saved model to get the probability that the *next* window contains a fault.
Like the trainer, the heavy aggregation runs in the database; pandas only ever
sees the compact result.

By default it uses the same window the model was trained with (stored in the
pickle) so features line up; --window overrides it. --latest keeps only the most
recent window per device — the actionable "what happens next" row.

Connection (same convention as the other scripts)
  CRATE_USER / CRATE_PASSWORD   credentials (kept out of argv)
  --cratedb-url / CRATEDB_URL   host, e.g. crate://localhost:4200

Usage
  export CRATE_USER=... CRATE_PASSWORD=...
  python predict_aggregated.py --cratedb-url crate://localhost:4200
  python predict_aggregated.py --cratedb-url crate://localhost:4200 --latest
  python predict_aggregated.py --cratedb-url crate://localhost:4200 --device DEVICE_0042

Output:  model/aggregated_scored.csv
"""

import argparse
import os
import pickle

import pandas as pd

import data_source

BASE       = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE = os.path.join(BASE, 'model', 'aggregated_maintenance_model.pkl')
OUT_FILE   = os.path.join(BASE, 'model', 'aggregated_scored.csv')


def build_features(df: pd.DataFrame, features, label_encoder) -> pd.DataFrame:
    """Reproduce train_aggregated.build_features for scoring (no target shift).
    Must mirror the trainer's feature logic exactly."""
    df = df.copy()

    # STDDEV is NULL for single-reading windows; "no variation" -> 0 (as in training).
    df['metric_std'] = df['metric_std'].astype(float).fillna(0.0)

    df['window_dow']  = df['window_start'].dt.dayofweek
    df['window_hour'] = df['window_start'].dt.hour

    # Encode device_type with the training encoder; map unseen categories to a
    # known class so transform() never raises (same approach as predict.py).
    known = set(label_encoder.classes_)
    df['device_type_safe'] = df['device_type'].astype(str).apply(
        lambda x: x if x in known else label_encoder.classes_[0])
    df['device_type_enc'] = label_encoder.transform(df['device_type_safe'])

    return df


def main(cratedb_url, window, days, device_filter, latest_only):
    print('CrateDB Industrial IoT - Aggregated (Scenario 2) Scoring')

    if not os.path.exists(MODEL_FILE):
        raise FileNotFoundError(
            f'{MODEL_FILE} not found — run:  python train_aggregated.py')

    with open(MODEL_FILE, 'rb') as f:
        payload = pickle.load(f)
    features = payload['features']
    model_window = payload.get('window', '1 day')
    window = window or model_window
    if window != model_window:
        print(f'  WARNING: scoring with {window!r} windows but the model was '
              f'trained on {model_window!r} — features may not line up.')
    print(f'Model loaded: {payload["model_name"]}  '
          f'(ROC-AUC: {payload.get("roc_auc", "n/a")}, window: {model_window})')

    engine = data_source.make_engine(cratedb_url)
    print(f'\nAggregating in CrateDB at {cratedb_url} ({window} windows) ...')
    df = data_source.load_aggregated_frame(engine, window=window, days=days)
    if df.empty:
        raise SystemExit('CrateDB returned no rows — check the URL, the '
                         'rtia.iot_data table, and the --days window.')

    if device_filter:
        df = df[df['device_id'] == device_filter]
        if df.empty:
            raise SystemExit(f'Device {device_filter} not found in the aggregated rows.')
    if latest_only:
        df = df.groupby('device_id', sort=False).tail(1).reset_index(drop=True)
    print(f'  {len(df):,} windows  |  {df["device_id"].nunique()} devices to score')

    df = build_features(df, features, payload['label_encoder'])
    df['fault_probability'] = payload['model'].predict_proba(df[features])[:, 1]
    df['fault_risk_label'] = pd.cut(
        df['fault_probability'], bins=[-0.01, 0.25, 0.60, 1.01],
        labels=['low', 'medium', 'high']).astype(str)

    out_cols = ['device_id', 'device_type', 'plant_id', 'window_start',
                'metric_mean', 'metric_std', 'quality_mean', 'fault_rate',
                'n_readings', 'fault_probability', 'fault_risk_label']
    out = df[out_cols].copy()
    out['window_start'] = out['window_start'].astype(str)
    out.to_csv(OUT_FILE, index=False)

    print(f'\n-- Scoring summary -------------------------------------------------')
    print(f'Windows scored: {len(out):,}  (predicting fault in the NEXT window)')
    print(f'\nFault-risk distribution:')
    print(out['fault_risk_label'].value_counts().to_string())
    print(f'\nTop 10 highest next-window fault probabilities:')
    top = out.nlargest(10, 'fault_probability')[
        ['device_id', 'plant_id', 'window_start', 'fault_rate', 'fault_probability']]
    print(top.to_string(index=False))
    print(f'\nScored windows written -> {OUT_FILE}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Score CrateDB window aggregates with the model from '
                    'train_aggregated.py (Scenario 2).')
    parser.add_argument(
        '--cratedb-url', default=os.getenv('CRATEDB_URL'),
        help='CrateDB host, e.g. crate://localhost:4200 (credentials via '
             'CRATE_USER / CRATE_PASSWORD env). Env: CRATEDB_URL')
    parser.add_argument(
        '--window', default=None,
        help="Aggregation window as a CrateDB INTERVAL. Default: the window the "
             "model was trained with.")
    parser.add_argument(
        '--days', type=int, default=None,
        help='Limit source rows to the last N days, NOW()-relative (0 = all '
             'history). Default: last 90 days relative to the latest reading.')
    parser.add_argument('--device', default=None,
                        help='Score only this device_id')
    parser.add_argument('--latest', action='store_true',
                        help='Keep only the most recent window per device')
    args = parser.parse_args()

    if not args.cratedb_url:
        parser.error('a CrateDB URL is required — pass --cratedb-url or set CRATEDB_URL '
                     '(this script has no file-based mode).')

    main(args.cratedb_url, args.window, args.days, args.device, args.latest)
