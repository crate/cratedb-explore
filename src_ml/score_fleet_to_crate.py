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
CrateDB Industrial IoT — Scenario 3: live batch scoring against the fleet
=========================================================================
Pull recent readings, score every device with both models, and write one
prediction row per device back to CrateDB — the current fleet risk snapshot.

  readings (CrateDB *or* a local file)  ->  score  ->  CrateDB rtia.fault_predictions

Per the task: the input may be local IoT data (--input) OR CrateDB (default),
but predictions are **always written to CrateDB**. A --cratedb-url is therefore
required even when reading from a file.

The output table matches the schema realtime_inference.py creates (same columns,
same names) so the batch job and the live service write one shared schema. It is
created with CREATE TABLE IF NOT EXISTS, then appended to — never replaced.

Connection (same convention as the other scripts)
  CRATEDB_USER / CRATEDB_PASSWORD   credentials (kept out of argv)
  --cratedb-url / CRATEDB_ALCHEMY_URL   host, e.g. crate://localhost:4200

Usage
  export CRATEDB_USER=... CRATEDB_PASSWORD=...
  python score_fleet_to_crate.py --cratedb-url crate://localhost:4200            # read CrateDB
  python score_fleet_to_crate.py --cratedb-url crate://localhost:4200 --input ../data/iot_demo_dataset.json
  python score_fleet_to_crate.py --cratedb-url crate://localhost:4200 --device DEVICE_0042

Requires the trained models in model/ (run train_model.py first).
"""

import argparse
import json
import os

import pandas as pd
from sqlalchemy import text

import data_source
import predict   # reuse flatten_records / build_features / _load_models / paths

# Columns written to the destination table — must match rtia.fault_predictions
# as defined in sql/rtia_schema_create.sql.
CANON_COLS = ['device_id', 'scored_at', 'latest_reading_ts', 'device_type',
              'plant_id', 'current_status', 'fault_probability',
              'fault_risk_label', 'anomaly_score']


def _require_table(engine, table: str):
    """Verify the destination table exists. It is created by
    sql/rtia_schema_create.sql, not here — fail with a clear message if missing
    rather than silently creating it."""
    schema, _, name = table.rpartition('.')
    check = text("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = :schema AND table_name = :name
    """)
    with engine.connect() as conn:
        exists = conn.execute(check, {'schema': schema or 'doc',
                                      'name': name}).scalar()
    if not exists:
        raise SystemExit(
            f'Table {table} not found — create the schema first:  '
            'crash < sql/rtia_schema_create.sql'
        )


def read_local(input_file: str, device):
    """Load readings from a local NDJSON file (last 50 per device for rolling
    context, or all rows for a single --device)."""
    print(f'\nLoading readings from {os.path.basename(input_file)} ...')
    records = []
    with open(input_file, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    df = predict.flatten_records(records)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if device:
        df = df[df['device_id'] == device]
        if df.empty:
            raise SystemExit(f'Device {device} not found in {input_file}.')
    else:
        df = (df.sort_values('timestamp')
                .groupby('device_id', sort=False)
                .tail(50)
                .reset_index(drop=True))
    return df


def main(cratedb_url, input_file, device, table):
    print('CrateDB Industrial IoT - Scenario 3: live batch scoring -> CrateDB')

    for path in (predict.CLF_FILE, predict.ISO_FILE):
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'{path} not found — run:  python train_model.py')

    predict.import_heavy_libs()          # bind pd/np for the reused predict funcs
    clf_p, iso_p = predict._load_models()

    engine = data_source.make_engine(cratedb_url)   # always needed: we write here

    # --- read source: local file or CrateDB ---
    if input_file:
        df = read_local(input_file, device)
        source = os.path.basename(input_file)
    else:
        print(f'\nReading last 50 readings/device from CrateDB at {cratedb_url} ...')
        df = data_source.load_scoring_frame(engine, device=device, context_rows=50)
        if df.empty:
            raise SystemExit('CrateDB returned no rows to score — check the URL / table.')
        source = cratedb_url
    print(f'  {len(df):,} readings  |  {df["device_id"].nunique()} devices  (from {source})')

    # --- score with both models ---
    df = predict.build_features(df, clf_p['label_encoder'])
    df['fault_probability'] = clf_p['model'].predict_proba(df[clf_p['features']].to_numpy())[:, 1]
    df['anomaly_score']     = -iso_p['model'].score_samples(df[iso_p['features']].to_numpy())

    # --- collapse to one row per device (the current fleet state) ---
    latest = (df.sort_values('timestamp')
                .groupby('device_id', sort=False)
                .last()
                .reset_index())
    latest['scored_at']         = pd.Timestamp.utcnow()
    latest['latest_reading_ts'] = latest['timestamp']
    latest['current_status']    = latest['status']
    latest['fault_risk_label']  = pd.cut(
        latest['fault_probability'], bins=[-0.01, 0.25, 0.60, 1.01],
        labels=['low', 'medium', 'high']).astype(str)

    # --- always write back to CrateDB ---
    schema, _, name = table.rpartition('.')
    _require_table(engine, table)
    print(f'\nWriting {len(latest):,} predictions to CrateDB table {table} ...')
    latest[CANON_COLS].to_sql(name, engine, schema=schema or None,
                              if_exists='append', index=False, method='multi')
    print(f'  wrote {len(latest):,} rows  (scored_at={latest["scored_at"].iloc[0]})')

    # --- summary ---
    print('\n-- Fleet risk summary ----------------------------------------------')
    print('Fault-risk distribution:')
    print(latest['fault_risk_label'].value_counts().to_string())
    print('\nTop 5 fault probabilities:')
    top = latest.nlargest(5, 'fault_probability')[
        ['device_id', 'plant_id', 'current_status', 'fault_probability', 'anomaly_score']]
    print(top.to_string(index=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Live batch scoring of the fleet, writing predictions back '
                    'to CrateDB (Scenario 3).')
    parser.add_argument(
        '--cratedb-url', default=os.getenv('CRATEDB_ALCHEMY_URL'),
        help='CrateDB host, e.g. crate://localhost:4200 (credentials via '
             'CRATEDB_USER / CRATEDB_PASSWORD env). Required — predictions are '
             'always written here. Env: CRATEDB_ALCHEMY_URL')
    parser.add_argument(
        '--input', default=None,
        help='Read readings from this local NDJSON file instead of CrateDB. '
             'Predictions are still written to CrateDB.')
    parser.add_argument('--device', default=None,
                        help='Score only this device_id')
    parser.add_argument('--table', default='rtia.fault_predictions',
                        help="Destination table (optionally schema-qualified, "
                             "e.g. rtia.fault_predictions). Default: rtia.fault_predictions")
    args = parser.parse_args()

    if not args.cratedb_url:
        parser.error('a CrateDB URL is required — pass --cratedb-url or set CRATEDB_ALCHEMY_URL '
                     '(predictions are always written to CrateDB).')

    main(args.cratedb_url, args.input, args.device, args.table)
