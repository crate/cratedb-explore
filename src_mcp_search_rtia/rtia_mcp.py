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
MCP server over the CrateDB Industrial-IoT (`rtia`) demo schema.

Exposes two kinds of tools to any MCP client (Claude Code, Claude Desktop, ...)
over stdio:

  * `query_sql` runs read-only SQL against CrateDB's HTTP `_sql` endpoint under
    the `rtia` schema (plants, devices, maintenance_log, iot_data, locations,
    knn_searches, fault_predictions).
  * `inference_health` / `score_device` / `score_batch` / `fleet_high_risk`
    proxy the predictive-maintenance FastAPI service (`realtime_inference.py`,
    started with `uvicorn realtime_inference:app --port 8000`). They let the
    model trigger live ML scoring, which fetches each device's recent history
    from CrateDB, builds rolling features, scores it, and writes the prediction
    back to `rtia.fault_predictions`.

The CrateDB connection defaults to a local cluster (crate@localhost:4200) and
can be overridden with --cratedb-url / --cratedb-host / ... flags or the
matching CRATEDB_* environment variables (flags win). The inference service
base URL defaults to http://localhost:8000 and is set with --inference-url or
INFERENCE_URL.
"""

import argparse
import os
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# Fallbacks so the server runs with no arguments.
DEFAULTS = {
    "host": "localhost",
    "port": "4200",
    "user": "crate",
    "password": "a_password",
    "scheme": "http",
    "inference_url": "http://localhost:8000",
}


def parse_args() -> argparse.Namespace:
    """CLI flags. All default to None so environment variables can layer
    underneath them in resolve_endpoint.

    Uses parse_known_args so the module can be imported by the MCP CLI
    (`mcp dev`/`mcp run`/`mcp install`), which re-passes its own argv — the
    extra tokens are ignored instead of aborting at import time."""
    p = argparse.ArgumentParser(
        description="MCP server over the CrateDB Industrial-IoT (rtia) schema.",
    )
    p.add_argument("--cratedb-url", help="Full URL, e.g. http://user:pw@host:4200/")
    p.add_argument("--cratedb-host", help="CrateDB host.")
    p.add_argument("--cratedb-port", help="CrateDB HTTP port (default 4200).")
    p.add_argument("--cratedb-user", help="CrateDB username.")
    p.add_argument("--cratedb-password", help="CrateDB password.")
    p.add_argument("--cratedb-scheme", help="http or https.")
    p.add_argument(
        "--inference-url",
        help="Base URL of the realtime_inference FastAPI service "
        "(default http://localhost:8000).",
    )
    return p.parse_known_args()[0]


def resolve_endpoint(args: argparse.Namespace) -> tuple[str, tuple[str, str], str]:
    """Resolve the `_sql` endpoint URL, HTTP Basic auth, and the inference
    service base URL.

    Either a full --cratedb-url / CRATEDB_CLUSTER_URL is supplied, or the
    pieces are assembled from --cratedb-host / CRATEDB_HOST and friends.
    CLI flags always win over environment variables, and anything still
    missing falls back to a local cluster so the example runs out of the box.

    Precedence is, in order: the --cratedb-url flag; any individual
    --cratedb-* flag (which forces the host-parts path so a
    CRATEDB_CLUSTER_URL in the environment can't silently override it);
    CRATEDB_CLUSTER_URL; then host parts from CRATEDB_* env vars / defaults.

    Credentials are kept separate so there is a single source of truth: when a
    URL carries no userinfo, the user/password still come from CRATEDB_USER /
    CRATEDB_PASSWORD (then the defaults), so you never embed them in the URL.
    """
    part_flags = (
        args.cratedb_host,
        args.cratedb_port,
        args.cratedb_user,
        args.cratedb_password,
        args.cratedb_scheme,
    )
    url = args.cratedb_url or (
        os.environ.get("CRATEDB_CLUSTER_URL") if not any(part_flags) else None
    )
    if url:
        u = urlparse(url)
        scheme = u.scheme or DEFAULTS["scheme"]
        host = u.hostname or DEFAULTS["host"]
        port = str(u.port or DEFAULTS["port"])
        user = u.username or os.environ.get("CRATEDB_USER") or DEFAULTS["user"]
        password = u.password or os.environ.get("CRATEDB_PASSWORD") or DEFAULTS["password"]
    else:
        scheme = args.cratedb_scheme or os.environ.get("CRATEDB_SCHEME") or DEFAULTS["scheme"]
        host = args.cratedb_host or os.environ.get("CRATEDB_HOST") or DEFAULTS["host"]
        port = args.cratedb_port or os.environ.get("CRATEDB_PORT") or DEFAULTS["port"]
        user = args.cratedb_user or os.environ.get("CRATEDB_USER") or DEFAULTS["user"]
        password = args.cratedb_password or os.environ.get("CRATEDB_PASSWORD") or DEFAULTS["password"]
    inference_url = (
        args.inference_url
        or os.environ.get("INFERENCE_URL")
        or DEFAULTS["inference_url"]
    ).rstrip("/")
    return f"{scheme}://{host}:{port}/_sql", (user, password), inference_url


ENDPOINT, AUTH, INFERENCE_URL = resolve_endpoint(parse_args())

INSTRUCTIONS = (
    "Tools query a CrateDB cluster of industrial-IoT data in the `rtia` schema "
    "and call a predictive-maintenance ML service over it. Tables: "
    "plants (5 facilities, geo_location), devices (500 assets: device_id, "
    "device_type, plant_id, line_id, ...), maintenance_log (~1,700 work orders "
    "with a full-text `notes` column - use MATCH(notes, '<terms>') - and a "
    "notes_embedding FLOAT_VECTOR(384) for KNN_MATCH), iot_data (sensor "
    "readings in Telegraf line-protocol shape: tags['device_id'], "
    "tags['status'] ('normal'|'warning'|'critical'), tags['metric_unit'], "
    "fields['metric_value'], fields['quality_score'], a generated geo_location, "
    "partitioned by event_week), locations (named points plus a Bavaria "
    "geo_area polygon), knn_searches (reference query vectors keyed by "
    "search_string for the maintenance_log KNN), and fault_predictions (ML "
    "output: device_id, scored_at, fault_probability, fault_risk_label, "
    "anomaly_score, plus denormalised device_type/plant_id/current_status). "
    "MANDATORY FIRST STEP: never run a data query without first confirming the "
    "actual table and column names. Before any SELECT against the data, query "
    "information_schema (e.g. SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'rtia', then SELECT column_name, data_type FROM "
    "information_schema.columns WHERE table_schema = 'rtia' AND table_name = "
    "'<table>') and write your query using only the table and column names that "
    "those results return. The schema summary above is guidance, not a "
    "substitute for this check. "
    "Sensor values are NOT temperatures-in-Kelvin: each reading's unit is in "
    "tags['metric_unit'] (e.g. C, mm/s, bar) - report the value with that unit, "
    "never convert. "
    "When a query reads iot_data and the user gives no time range, scope it to "
    "each device's latest readings (the broken devices' faults are their most "
    "recent rows); e.g. filter on \"timestamp\" relative to "
    "(SELECT MAX(d2.\"timestamp\") FROM rtia.iot_data d2 WHERE "
    "d2.tags['device_id'] = ...). "
    "fault_predictions is denormalised - do NOT join it to iot_data (one "
    "prediction per device vs ~1,000 readings each fans the result out 1,000x); "
    "join the 1-row-per-device rtia.devices for extra asset columns. "
    "For 'in/near a place' questions use WITHIN(geo_location, l.geo_area) or "
    "DISTANCE() against rtia.locations rather than guessing coordinates. "
    "End every SQL statement with LIMIT 1000 unless the user instructs you "
    "otherwise. "
    "To score devices live, use the inference tools (score_device, "
    "score_batch, fleet_high_risk); they require the realtime_inference FastAPI "
    "service to be running (uvicorn realtime_inference:app --port 8000) and "
    "write results to rtia.fault_predictions."
)

mcp = FastMCP("rtia", instructions=INSTRUCTIONS)


@mcp.tool()
def query_sql(statement: str) -> str:
    """Run a read-only SQL statement against the CrateDB `rtia` schema and
    return columns + rows.

    UNDER NO CIRCUMSTANCES query the data before checking the table and column
    names first. Your first calls for any task must inspect the schema via
    information_schema (SELECT table_name FROM information_schema.tables WHERE
    table_schema = 'rtia'; then SELECT column_name, data_type FROM
    information_schema.columns WHERE table_schema = 'rtia' AND table_name =
    '<table>'). Only after you have confirmed the real names from those results
    may you build SELECTs against the data, and only with names that appear in
    them.

    iot_data follows a Telegraf line-protocol shape - device identity and
    status live under tags (tags['device_id'], tags['status'],
    tags['metric_unit']) and the numbers under fields (fields['metric_value'],
    fields['quality_score']). Sensor values are NOT Kelvin temperatures; report
    each value with its tags['metric_unit'] and never convert.

    maintenance_log carries a full-text `notes` column and a notes_embedding
    FLOAT_VECTOR(384): answer "what went wrong / find similar issues" questions
    with MATCH(notes, '<terms>') or KNN_MATCH(notes_embedding, (SELECT embedding
    FROM rtia.knn_searches WHERE search_string = '<query>'), <k>) rather than
    assuming the data is numeric-only.

    When you read iot_data and the user gives no time range, scope it to each
    device's latest readings (the broken devices' faults are their most recent
    rows). fault_predictions is denormalised - do NOT join it to iot_data (1
    prediction per device vs ~1,000 readings each fans the result out 1,000x);
    join the 1-row-per-device rtia.devices for extra asset columns.

    For "in/near a place" questions use WITHIN(geo_location, l.geo_area) or
    DISTANCE() against rtia.locations rather than guessing coordinates.

    End every SQL statement with LIMIT 1000 unless the user instructs you
    otherwise.
    """
    # CrateDB's HTTP _sql endpoint is stateless, so the persistent equivalent
    # of `SET search_path TO rtia` is the Default-Schema header on each request.
    r = httpx.post(
        ENDPOINT,
        json={"stmt": statement},
        auth=AUTH,
        headers={"Default-Schema": "rtia"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    cols, rows = data.get("cols", []), data.get("rows", [])
    lines = [f"columns: {cols}", f"row count: {len(rows)}"]
    lines += [f"  {row}" for row in rows[:50]]
    if len(rows) > 50:
        lines.append(f"  ... {len(rows) - 50} more rows omitted")
    return "\n".join(lines)


def _inference_get(path: str, params: dict | None = None) -> str:
    """GET the realtime_inference service and return its JSON as text."""
    r = httpx.get(f"{INFERENCE_URL}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.text


@mcp.tool()
def inference_health() -> str:
    """Return the predictive-maintenance service status, model version, and
    ROC-AUC (proxies GET /health on realtime_inference). Call this first to
    confirm the service is running before scoring."""
    return _inference_get("/health")


@mcp.tool()
def score_device(device_id: str, write: bool = True) -> str:
    """Score a single device for fault risk (proxies GET /score/{device_id}).

    The service fetches the device's recent readings from rtia.iot_data, builds
    rolling features, scores with the trained model, and returns
    fault_probability, fault_risk_label, anomaly_score, and the latest reading
    context. With write=True (default) it also writes the prediction to
    rtia.fault_predictions; pass write=False for a read-only score.
    """
    return _inference_get(f"/score/{device_id}", {"write": str(write).lower()})


@mcp.tool()
def score_batch(device_ids: list[str], write_to_cratedb: bool = False) -> str:
    """Score several devices in one call (proxies POST /score/batch).

    Pass a list of device_ids; set write_to_cratedb=True to persist each
    prediction to rtia.fault_predictions (default False = read-only).
    """
    r = httpx.post(
        f"{INFERENCE_URL}/score/batch",
        json={"device_ids": device_ids, "write_to_cratedb": write_to_cratedb},
        timeout=120,
    )
    r.raise_for_status()
    return r.text


@mcp.tool()
def fleet_high_risk(threshold: float = 0.6, limit: int = 20) -> str:
    """List the most recently scored devices whose fault_probability is at or
    above `threshold` (proxies GET /fleet/high-risk). This reads existing rows
    from rtia.fault_predictions - score devices first if it comes back empty.
    """
    return _inference_get(
        "/fleet/high-risk", {"threshold": threshold, "limit": limit}
    )


if __name__ == "__main__":
    mcp.run()
