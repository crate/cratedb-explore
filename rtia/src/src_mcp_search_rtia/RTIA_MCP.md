# Query and score industrial-IoT data in CrateDB with an MCP server

CrateDB stores high-volume IoT and time-series sensor data and lets you query it
in real time with SQL. The Model Context Protocol (MCP) takes this a step
further: it connects an AI assistant directly to your cluster, so you can ask
questions in plain English and let the assistant write the SQL.

This guide builds an MCP server over the [industrial-IoT dataset](https://github.com/crate/cratedb-explore)
(the `rtia` schema — 5 plants, 500 devices, half a million sensor readings) and
wires it to the [predictive-maintenance service](../src_ml/ML_GUIDE.md) that
ships alongside it. In a few minutes you will have an assistant that answers
questions such as "Which devices are at highest fault risk right now?" — and that
can trigger live ML scoring against the same cluster.

## What you'll do

You'll write a single Python file that runs as an MCP server and exposes:

- `query_sql` — sends statements to CrateDB's HTTP `_sql` endpoint and returns
  the rows, under the `rtia` schema.
- `inference_health`, `score_device`, `score_batch`, `fleet_high_risk` — thin
  proxies to the `realtime_inference.py` FastAPI service, so the assistant can
  trigger predictive-maintenance scoring and read the fleet's risk back.

Any MCP-capable assistant — Claude Code or Claude Desktop, for example — can then
discover these tools and use them to answer your questions.

## Prerequisites

- Python 3.10 or later.
- A CrateDB cluster with the `rtia` schema loaded (`rtia/sql/rtia_schema_create.sql`),
  reachable on its HTTP port (`4200` by default).
- For the scoring tools: the `realtime_inference` service running (see
  [Step 5](#step-5--wire-in-the-inference-service)). `query_sql` works without it.
- An MCP-capable AI assistant, such as Claude Code or Claude Desktop.

## The dataset

The demo data lives in the `rtia` schema and models five industrial plants:

- `plants` — 5 facilities (`plant_id`, `plant_name`, `city`, `geo_location`, …).
- `devices` — 500 assets (`device_id`, `device_type`, `plant_id`, `line_id`,
  manufacturer/model, maintenance dates).
- `maintenance_log` — ~1,700 work orders. `notes` is full-text indexed (search
  with `MATCH`) and `notes_embedding` is a `FLOAT_VECTOR(384)` for semantic
  `KNN_MATCH` search.
- `iot_data` — 500,000 sensor readings in Telegraf line-protocol shape: identity
  and state live under `tags` (`tags['device_id']`, `tags['status']`,
  `tags['metric_unit']`), the numbers under `fields` (`fields['metric_value']`,
  `fields['quality_score']`). `geo_location` is generated from the field coords
  and the table is `PARTITIONED BY (event_week)`.
- `locations` — named points (one per plant city) plus a Bavaria `geo_area`
  polygon, for geographic filtering.
- `knn_searches` — reference query vectors keyed by `search_string`, so a
  semantic search can look its embedding up by name instead of pasting a
  384-element literal.
- `fault_predictions` — the ML output: one row per device per scoring run, with
  `fault_probability`, `fault_risk_label`, `anomaly_score`, and denormalised
  `device_type` / `plant_id` / `current_status`.

Unlike the German-weather demo, sensor values here are **not** temperatures in
Kelvin — each reading's unit is in `tags['metric_unit']` (°C, mm/s, bar, …), so
the assistant reports the value with its unit and never converts.

## Step 1 — Install the dependencies

Create and activate a virtual environment first so these packages stay isolated
from your system Python, then install the MCP Python SDK and an HTTP client:

```bash
python -m venv .venv .           # If needed....  
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "mcp[cli]" httpx
```

## Step 2 — Create the server

Save the following as [`rtia_mcp.py`](https://github.com/crate/cratedb-explore/blob/main/rtia/src/src_mcp_search_rtia/rtia_mcp.py).
It resolves the CrateDB connection and inference URL at startup, then defines the
SQL tool and the four inference tools.

```python
"""
MCP server over the CrateDB Industrial-IoT (`rtia`) demo schema.

Exposes a `query_sql` tool over CrateDB's HTTP `_sql` endpoint (Default-Schema:
rtia) plus four tools that proxy the predictive-maintenance FastAPI service
(realtime_inference.py) so the model can trigger live ML scoring.
"""

import argparse
import os
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULTS = {
    "host": "localhost",
    "port": "4200",
    "user": "crate",
    "password": "a_password",
    "scheme": "http",
    "inference_url": "http://localhost:8000",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MCP server over the CrateDB Industrial-IoT (rtia) schema.",
    )
    p.add_argument("--cratedb-url", help="Full URL, e.g. http://user:pw@host:4200/")
    p.add_argument("--cratedb-host", help="CrateDB host.")
    p.add_argument("--cratedb-port", help="CrateDB HTTP port (default 4200).")
    p.add_argument("--cratedb-user", help="CrateDB username.")
    p.add_argument("--cratedb-password", help="CrateDB password.")
    p.add_argument("--cratedb-scheme", help="http or https.")
    p.add_argument("--inference-url", help="realtime_inference base URL "
                   "(default http://localhost:8000).")
    return p.parse_args()


def resolve_endpoint(args):
    """Resolve the `_sql` URL, HTTP Basic auth, and inference base URL.
    CLI flags win over CRATEDB_* env vars, which fall back to a local cluster."""
    part_flags = (args.cratedb_host, args.cratedb_port, args.cratedb_user,
                  args.cratedb_password, args.cratedb_scheme)
    url = args.cratedb_url or (
        os.environ.get("CRATEDB_CLUSTER_URL") if not any(part_flags) else None
    )
    if url:
        u = urlparse(url)
        scheme = u.scheme or DEFAULTS["scheme"]
        host = u.hostname or DEFAULTS["host"]
        port = str(u.port or DEFAULTS["port"])
        user = u.username or DEFAULTS["user"]
        password = u.password or DEFAULTS["password"]
    else:
        scheme = args.cratedb_scheme or os.environ.get("CRATEDB_SCHEME") or DEFAULTS["scheme"]
        host = args.cratedb_host or os.environ.get("CRATEDB_HOST") or DEFAULTS["host"]
        port = args.cratedb_port or os.environ.get("CRATEDB_PORT") or DEFAULTS["port"]
        user = args.cratedb_user or os.environ.get("CRATEDB_USER") or DEFAULTS["user"]
        password = args.cratedb_password or os.environ.get("CRATEDB_PASSWORD") or DEFAULTS["password"]
    inference_url = (args.inference_url or os.environ.get("INFERENCE_URL")
                     or DEFAULTS["inference_url"]).rstrip("/")
    return f"{scheme}://{host}:{port}/_sql", (user, password), inference_url


ENDPOINT, AUTH, INFERENCE_URL = resolve_endpoint(parse_args())

INSTRUCTIONS = (
    "...rtia schema summary + rules: inspect information_schema first, values "
    "are NOT Kelvin (use tags['metric_unit']), default to each device's latest "
    "readings, don't fan out fault_predictions, use rtia.locations for geo..."
)

mcp = FastMCP("rtia", instructions=INSTRUCTIONS)


@mcp.tool()
def query_sql(statement: str) -> str:
    """Read-only SQL against the `rtia` schema. Inspect information_schema
    before querying data; see the full file for the encoded rules."""
    r = httpx.post(ENDPOINT, json={"stmt": statement}, auth=AUTH,
                   headers={"Default-Schema": "rtia"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    cols, rows = data.get("cols", []), data.get("rows", [])
    lines = [f"columns: {cols}", f"row count: {len(rows)}"]
    lines += [f"  {row}" for row in rows[:50]]
    if len(rows) > 50:
        lines.append(f"  ... {len(rows) - 50} more rows omitted")
    return "\n".join(lines)


def _inference_get(path, params=None):
    r = httpx.get(f"{INFERENCE_URL}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.text


@mcp.tool()
def inference_health() -> str:
    """Service status, model version, ROC-AUC (GET /health)."""
    return _inference_get("/health")


@mcp.tool()
def score_device(device_id: str, write: bool = True) -> str:
    """Score one device, optionally writing to rtia.fault_predictions
    (GET /score/{device_id})."""
    return _inference_get(f"/score/{device_id}", {"write": str(write).lower()})


@mcp.tool()
def score_batch(device_ids: list[str], write_to_cratedb: bool = False) -> str:
    """Score several devices in one call (POST /score/batch)."""
    r = httpx.post(f"{INFERENCE_URL}/score/batch",
                   json={"device_ids": device_ids,
                         "write_to_cratedb": write_to_cratedb}, timeout=120)
    r.raise_for_status()
    return r.text


@mcp.tool()
def fleet_high_risk(threshold: float = 0.6, limit: int = 20) -> str:
    """Most recently scored devices with fault_probability >= threshold
    (GET /fleet/high-risk)."""
    return _inference_get("/fleet/high-risk",
                          {"threshold": threshold, "limit": limit})


if __name__ == "__main__":
    mcp.run()
```

> The snippet above trims the long `INSTRUCTIONS` string and a few docstrings for
> readability — the committed `rtia_mcp.py` carries the full versions, which is
> where the data rules actually live.

A few details make this work against CrateDB:

- The connection is resolved once at startup by `resolve_endpoint`, which reads
  the `--cratedb-*` flags, then the matching `CRATEDB_*` environment variables,
  and falls back to a local `crate@localhost:4200` cluster. The inference base
  URL is resolved the same way (`--inference-url` / `INFERENCE_URL`, default
  `http://localhost:8000`).
- `query_sql` posts to the HTTP `_sql` endpoint on port `4200` and reads `cols`
  and `rows` from the JSON response. Each request carries a
  `Default-Schema: rtia` header — the stateless `_sql` equivalent of
  `SET search_path TO rtia`, so the assistant can use unqualified table names.
- The four inference tools are thin HTTP clients for the FastAPI service. They
  return its JSON verbatim, so the assistant sees exactly the same payloads
  documented in `ML_GUIDE.md`.
- The `instructions` and tool descriptions carry the data rules — inspect the
  schema with `information_schema` first, units come from `tags['metric_unit']`
  (not Kelvin), default to each device's latest readings, don't fan out
  `fault_predictions`, and filter geography against `rtia.locations` — so the
  assistant applies them without being reminded in every prompt.

## Step 3 — Register the server

### Point the server at your cluster

The server reads the connection from `--cratedb-*` flags (or the matching
`CRATEDB_*` environment variables) and only falls back to `crate@localhost:4200`
if you pass nothing. The simplest override is a single URL:

```bash
--cratedb-url "http://<user>:<password>@<host>:4200/"
```

or set the equivalent pieces as environment variables before launching:

```bash
export CRATEDB_HOST=<host>
export CRATEDB_USER=<user>
export CRATEDB_PASSWORD=<password>
export CRATEDB_SCHEME=http          # https only if your cluster terminates TLS
```

### Smoke-test the script

Run the script on its own first, so you register something that runs:

```bash
python /path/to/rtia_mcp.py --cratedb-url "http://<user>:<password>@<host>:4200/"
```

It should start and then wait silently for input on stdin (it's a stdio server
with no banner) — that means it launched cleanly; press Ctrl+C to stop it. If it
exits with a traceback, fix that first: the usual causes are a wrong path or a
`python` that can't see the installed `mcp`/`httpx` packages.

### Register with your assistant

For Claude Code, everything after `--` is the launch command:

```bash
claude mcp add rtia -- python /path/to/rtia_mcp.py --cratedb-url "http://<user>:<password>@<host>:4200/"
```

To check the server into a project, add it to a `.mcp.json` at the repo root
instead, keeping credentials in the environment:

```json
{
  "mcpServers": {
    "rtia": {
      "command": "python",
      "args": ["/path/to/rtia_mcp.py"],
      "env": {
        "CRATEDB_HOST": "${CRATEDB_HOST}",
        "CRATEDB_USER": "${CRATEDB_USER}",
        "CRATEDB_PASSWORD": "${CRATEDB_PASSWORD}",
        "CRATEDB_SCHEME": "http",
        "INFERENCE_URL": "http://localhost:8000"
      }
    }
  }
}
```

For Claude Desktop, add the same `mcpServers` entry to
`claude_desktop_config.json`. Restart the assistant so it picks up the server,
then confirm it connected: `claude mcp list` (or `/mcp` in a session) should show
`rtia` with its five tools available.

## Step 4 — Ask a question

With the server registered, ask a question in natural language:

> Show me the five devices with the worst recent sensor quality, and which plant
> they're in.

The assistant inspects the schema, writes the SQL through `query_sql` (scoping
`iot_data` to each device's latest readings), joins `devices`/`plants` for the
asset context, and answers — reporting each value with its `tags['metric_unit']`.

## Step 5 — Wire in the inference service

The four scoring tools proxy the `realtime_inference.py` FastAPI service. Start
it in a separate window first (full walkthrough in `../src_ml/ML_GUIDE.md`):

```bash
cd ../src_ml
source .venv/bin/activate
export CRATEDB_ALCHEMY_URL='crate://localhost:4200'      # CrateDB Cloud: add user/pw env + ?ssl=true
uvicorn realtime_inference:app --reload --port 8000
```

The service fetches each device's recent history from `rtia.iot_data`, builds
rolling features, scores with the trained model (XGBoost + Isolation Forest),
and writes the prediction back to `rtia.fault_predictions`. Now you can ask:

> Score DEVICE_0042 and tell me whether it needs attention.

The assistant calls `inference_health` to confirm the service is up, then
`score_device` — which returns:

```json
{
  "device_id": "DEVICE_0042",
  "device_type": "vibration_sensor",
  "plant_id": "PLANT_DORTMUND",
  "latest_reading_ts": "2025-10-11 22:03:14",
  "current_status": "warning",
  "fault_probability": 0.874,
  "fault_risk_label": "high",
  "anomaly_score": 0.291,
  "context_rows_used": 50,
  "latency_ms": 18.4
}
```

Because the score is written to `rtia.fault_predictions`, the assistant can then
read it back with `query_sql` alongside the rest of the fleet:

```sql
SELECT device_id, device_type, plant_id, current_status,
       fault_probability, fault_risk_label, scored_at
FROM fault_predictions
WHERE current_status IN ('warning', 'critical')
  AND fault_probability > 0.60
ORDER BY fault_probability DESC
LIMIT 20;
```

Or skip the SQL entirely and use `fleet_high_risk` to list the already-scored
devices above a probability threshold. A typical loop is: `score_batch` over the
device list to refresh predictions, then `fleet_high_risk` (or the query above)
to triage — `fault_predictions` is denormalised, so this needs no join back to
the 1,000-rows-per-device `iot_data`.

## Quirks

As of the time of writing Claude (4.8) is sometimes mistaking invalid column
names for '404' errors, but then continuing.

## Next steps

- Read the [ML guide](../src_ml/ML_GUIDE.md) for how the predictions are trained
  and served.
- Read the [CrateDB MCP documentation](https://cratedb.com/docs/guide/integrate/mcp/cratedb-mcp.html)
  for the full picture, including documentation retrieval.
- Explore [`cratedb-mcp`](https://github.com/crate/cratedb-mcp), the
  ready-to-run MCP server from Crate.io, for production use.
```
