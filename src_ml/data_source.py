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

"""Optional CrateDB data source for train_model.py and predict.py.

The default scripts read the local NDJSON file; passing --cratedb-url makes them
pull from CrateDB instead. This module centralises that path so the two scripts
share one connection helper and one set of SQL queries (the same queries the
"Using CrateDB as the data source" section of ML_GUIDE.md documents).

The readings live in rtia.iot_data in Telegraf line-protocol shape — strings in
the `tags` object, numbers in `fields` — so every query reaches into them with
tags['…'] / fields['…'] and aliases each back to the flat column name the
feature code expects (device_id, device_type, plant_id, timestamp,
metric_value, metric_unit, quality_score, status, firmware_version).

Imports of sqlalchemy / pandas are deferred into the functions so importing this
module stays cheap (train_model.py keeps its heavy imports lazy on purpose).
"""

import os
import re

# Flat columns every loader returns — the contract the feature code relies on.
SELECT_COLUMNS = """
        tags['device_id']                 AS device_id,
        tags['device_type']               AS device_type,
        tags['plant_id']                  AS plant_id,
        "timestamp",
        fields['metric_value']            AS metric_value,
        tags['metric_unit']               AS metric_unit,
        tags['status']                    AS status,
        fields['quality_score']           AS quality_score,
        tags['metadata_firmware_version'] AS firmware_version
"""


def make_engine(url: str):
    """Build a SQLAlchemy engine for CrateDB from a host URL.

    `url` carries host/port only (e.g. 'crate://localhost:4200' or just
    'localhost:4200'); credentials come from the CRATEDB_USER / CRATEDB_PASSWORD
    environment variables and are injected here, so secrets never appear in
    argv. A scheme is added if missing, and an http(s):// scheme is rewritten to
    the crate:// dialect sqlalchemy-cratedb registers.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    try:
        from sqlalchemy import create_engine
    except ModuleNotFoundError as exc:   # optional dependency tier
        raise SystemExit(
            'CrateDB source needs sqlalchemy + sqlalchemy-cratedb — '
            'install the optional deps:  pip install -r requirements.txt'
        ) from exc

    if '://' not in url:
        url = 'crate://' + url
    parts = urlsplit(url)
    scheme = 'crate' if parts.scheme in ('crate', 'http', 'https') else parts.scheme

    # Inject env credentials only when the URL doesn't already carry userinfo.
    netloc = parts.netloc
    if '@' not in netloc:
        user = os.getenv('CRATEDB_USER')
        password = os.getenv('CRATEDB_PASSWORD')
        if user:
            cred = quote(user, safe='')
            if password:
                cred += ':' + quote(password, safe='')
            netloc = f'{cred}@{netloc}'

    resolved = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return create_engine(resolved, echo=False)


def connection_error(engine, exc):
    """Turn a noisy SQLAlchemy/crate stack trace into a one-line SystemExit."""
    host = engine.url.render_as_string(hide_password=True)
    detail = str(exc)
    if '401' in detail or 'Unauthorized' in detail:
        msg = (f'CrateDB rejected the credentials at {host} (401 Unauthorized).\n'
               f'Set them before running, e.g.:\n'
               f'    export CRATEDB_USER=<user> CRATEDB_PASSWORD=<password>')
        if engine.url.username is None:
            msg += '\n(CRATEDB_USER is not set, so no credentials were sent.)'
        return SystemExit(msg)
    return SystemExit(f'Could not query CrateDB at {host}: {detail}')


def _query(engine, sql: str, params=None):
    """Run a query, turning a noisy connection/auth failure into a SystemExit."""
    import pandas as pd
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        return pd.read_sql(text(sql), engine, params=params)
    except SQLAlchemyError as exc:
        raise connection_error(engine, exc) from exc


def _coerce_timestamp(series):
    """CrateDB may return TIMESTAMP as epoch milliseconds; coerce both shapes."""
    import pandas as pd
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit='ms')
    return pd.to_datetime(series)


def _read_frame(engine, sql: str, params=None):
    """Run a query and return a DataFrame with `timestamp` coerced to datetime
    and rows ordered by (device_id, timestamp) — matching what the file-based
    loaders hand to the feature code."""
    df = _query(engine, sql, params=params)
    if df.empty:
        return df
    df['timestamp'] = _coerce_timestamp(df['timestamp'])
    return df.sort_values(['device_id', 'timestamp']).reset_index(drop=True)


def _time_filter(days):
    """WHERE clause for the training window, shared by the loaders.

    `days` is None  -> last 90 days relative to the *latest reading* (the demo
                       dataset's timestamps can predate wall-clock NOW()).
    `days` > 0      -> last N days relative to NOW().
    `days` <= 0     -> no filter (all history).
    """
    if days is None:
        return ('WHERE "timestamp" >= '
                '(SELECT MAX("timestamp") FROM rtia.iot_data) - INTERVAL \'90\' DAY')
    if days > 0:
        return f'WHERE "timestamp" >= NOW() - INTERVAL \'{int(days)}\' DAY'
    return ''


def load_training_frame(engine, days=None):
    """Full per-reading history for training (see _time_filter for `days`)."""
    sql = f"""
    SELECT {SELECT_COLUMNS}
    FROM rtia.iot_data
    {_time_filter(days)}
    ORDER BY tags['device_id'], "timestamp"
    """
    return _read_frame(engine, sql)


# Matches a CrateDB INTERVAL literal like '1 day' / '6 hours' / '30 minutes'.
_INTERVAL_RE = re.compile(r'^\d+\s+(second|minute|hour|day|week)s?$', re.IGNORECASE)


def load_aggregated_frame(engine, window='1 day', days=None):
    """Per-device, per-time-window feature aggregates computed in CrateDB
    (Scenario 2 in ML_GUIDE.md).

    Returns one row per (device, window) with metric mean/std, quality mean,
    fault_rate, and had_fault — the heavy rolling work pushed into the database
    so pandas only ever sees the compact result. `window` is any CrateDB
    INTERVAL literal (e.g. '1 day', '6 hours'); `days` matches load_training_frame.
    """
    if not _INTERVAL_RE.match(window.strip()):
        raise SystemExit(
            f"--window {window!r} is not a valid interval — use e.g. '1 day', "
            f"'6 hours', '30 minutes'.")

    sql = f"""
    SELECT
        tags['device_id']    AS device_id,
        tags['device_type']  AS device_type,
        tags['plant_id']     AS plant_id,
        DATE_BIN('{window}'::INTERVAL, "timestamp", TIMESTAMP '1970-01-01')
            AS window_start,
        AVG(fields['metric_value'])    AS metric_mean,
        STDDEV(fields['metric_value']) AS metric_std,
        AVG(fields['quality_score'])   AS quality_mean,
        COUNT(*) FILTER (WHERE tags['status'] IN ('warning', 'critical'))
            * 1.0 / NULLIF(COUNT(*), 0) AS fault_rate,
        MAX(CASE WHEN tags['status'] IN ('warning', 'critical') THEN 1 ELSE 0 END)
            AS had_fault,
        COUNT(*)             AS n_readings
    FROM rtia.iot_data
    {_time_filter(days)}
    GROUP BY tags['device_id'], tags['device_type'], tags['plant_id'], window_start
    ORDER BY tags['device_id'], window_start
    """
    df = _query(engine, sql)
    if df.empty:
        return df
    df['window_start'] = _coerce_timestamp(df['window_start'])
    return df.sort_values(['device_id', 'window_start']).reset_index(drop=True)


def load_scoring_frame(engine, device=None, context_rows=50):
    """Last `context_rows` readings per device for scoring (enough history for
    rolling features to stabilise). `device` restricts to a single device_id."""
    device_filter = ''
    params = {'rn': int(context_rows)}
    if device:
        device_filter = "WHERE tags['device_id'] = :device"
        params['device'] = device

    sql = f"""
    SELECT {SELECT_COLUMNS}
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY tags['device_id']
                                  ORDER BY "timestamp" DESC) AS rn
        FROM rtia.iot_data
        {device_filter}
    ) t
    WHERE rn <= :rn
    ORDER BY tags['device_id'], "timestamp"
    """
    return _read_frame(engine, sql, params=params)
