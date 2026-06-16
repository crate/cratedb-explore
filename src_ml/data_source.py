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
    'localhost:4200'); credentials come from the CRATE_USER / CRATE_PASSWORD
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
        user = os.getenv('CRATE_USER')
        password = os.getenv('CRATE_PASSWORD')
        if user:
            cred = quote(user, safe='')
            if password:
                cred += ':' + quote(password, safe='')
            netloc = f'{cred}@{netloc}'

    resolved = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return create_engine(resolved, echo=False)


def _connection_error(engine, exc):
    """Turn a noisy SQLAlchemy/crate stack trace into a one-line SystemExit."""
    host = engine.url.render_as_string(hide_password=True)
    detail = str(exc)
    if '401' in detail or 'Unauthorized' in detail:
        msg = (f'CrateDB rejected the credentials at {host} (401 Unauthorized).\n'
               f'Set them before running, e.g.:\n'
               f'    export CRATE_USER=<user> CRATE_PASSWORD=<password>')
        if engine.url.username is None:
            msg += '\n(CRATE_USER is not set, so no credentials were sent.)'
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
        raise _connection_error(engine, exc) from exc


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


def oldest_timestamp(engine):
    """Earliest reading timestamp in rtia.iot_data, or None if the table is
    empty. Used to anchor train_model.py's default window to the data instead
    of wall-clock NOW()."""
    import pandas as pd
    df = _query(engine, 'SELECT MIN("timestamp") AS oldest FROM rtia.iot_data')
    val = df.iloc[0, 0] if not df.empty else None
    if val is None or pd.isna(val):
        return None
    return _coerce_timestamp(pd.Series([val])).iloc[0]


def load_training_frame(engine, days=None, since=None):
    """Full per-reading history for training.

    `since` (a datetime) filters to readings at or after an absolute cutoff and
    takes precedence. Otherwise `days` limits to the last N days relative to
    NOW(); None / <= 0 pulls everything.
    """
    where = ''
    params = None
    if since is not None:
        import pandas as pd
        where = 'WHERE "timestamp" >= :since'
        params = {'since': pd.Timestamp(since).isoformat()}
    elif days and days > 0:
        where = f'WHERE "timestamp" >= NOW() - INTERVAL \'{int(days)}\' DAY'

    sql = f"""
    SELECT {SELECT_COLUMNS}
    FROM rtia.iot_data
    {where}
    ORDER BY tags['device_id'], "timestamp"
    """
    return _read_frame(engine, sql, params=params)


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
