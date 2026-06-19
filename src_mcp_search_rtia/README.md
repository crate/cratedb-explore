# rtia_mcp — an MCP server over the CrateDB Industrial-IoT schema

A single-file [Model Context Protocol](https://modelcontextprotocol.io) server
that exposes the `rtia` industrial-IoT demo data in CrateDB **and** the
predictive-maintenance ML service over it. Point any MCP client (Claude Code,
Claude Desktop, …) at it and ask questions — or trigger live fault scoring — in
plain English.

It speaks to CrateDB's HTTP `_sql` endpoint on port 4200, sending a
`Default-Schema: rtia` header on every request so unqualified table names
resolve under the `rtia` schema. It is built on the official MCP Python SDK
(`FastMCP`), the same foundation as [`cratedb-mcp`](https://github.com/crate/cratedb-mcp).

It is a sibling of `src_mcp_search_german_weather/` (same shape, different
schema), with extra tools that proxy the `realtime_inference.py` FastAPI service
described in [`../src_ml/ML_GUIDE.md`](../src_ml/ML_GUIDE.md).

## Tools

| Tool | What it does |
|---|---|
| `query_sql` | Read-only SQL against the `rtia` schema (plants, devices, maintenance_log, iot_data, locations, knn_searches, fault_predictions). |
| `inference_health` | Service status, model version, ROC-AUC (`GET /health`). |
| `score_device` | Score one device for fault risk, optionally writing to `rtia.fault_predictions` (`GET /score/{device_id}`). |
| `score_batch` | Score a list of devices in one call (`POST /score/batch`). |
| `fleet_high_risk` | Devices with `fault_probability` at/above a threshold (`GET /fleet/high-risk`). |

The four inference tools call the `realtime_inference` service, so that service
must be running for them to work (see [Run the inference service](#run-the-inference-service)).

## Files

| File | Purpose |
|---|---|
| `rtia_mcp.py` | The MCP server. Config resolution + `query_sql` and the four inference tools. |
| `requirements.txt` | Dependencies: `mcp[cli]`, `httpx`. |
| `RTIA_MCP.md` | Draft cratedb.com how-to page for this example. |

## Install

Optionally, create and activate a virtual environment first to keep the
dependencies isolated:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

Then install the dependencies:

```bash
pip install -r requirements.txt
```

## Connection

Defaults to a local cluster `crate@localhost:4200` and inference service
`http://localhost:8000`, so it runs with no arguments. Override with CLI flags
or the matching environment variables — flags win.

| Flag | Env var | Default |
|---|---|---|
| `--cratedb-url` | `CRATEDB_CLUSTER_URL` | — (overrides the parts below) |
| `--cratedb-host` | `CRATEDB_HOST` | `localhost` |
| `--cratedb-port` | `CRATEDB_PORT` | `4200` |
| `--cratedb-user` | `CRATEDB_USER` | `crate` |
| `--cratedb-password` | `CRATEDB_PASSWORD` | `a_password` |
| `--cratedb-scheme` | `CRATEDB_SCHEME` | `http` |
| `--inference-url` | `INFERENCE_URL` | `http://localhost:8000` |

## Run the inference service

The scoring tools (`score_device`, `score_batch`, `fleet_high_risk`,
`inference_health`) proxy the FastAPI service from `src_ml/`. Start it in a
separate window first — see `../src_ml/ML_GUIDE.md` for the full walkthrough:

```bash
cd ../src_ml
source .venv/bin/activate
export CRATEDB_URL='crate://localhost:4200'      # CrateDB Cloud: add user/pw env + ?ssl=true
uvicorn realtime_inference:app --reload --port 8000
```

`query_sql` works without it; only the four inference tools need it.

## Try it with the MCP Inspector

The `mcp` dev CLI launches the server with a web Inspector so you can call the
tools by hand:

```bash
mcp dev rtia_mcp.py
```

Call `query_sql` with `SELECT 1` to confirm connectivity, then try a real query
such as `SELECT device_id, device_type, plant_id FROM devices ORDER BY 1 LIMIT 5`.

## Register with an MCP client

**Claude Code:**

```bash
claude mcp add rtia -- python /abs/path/to/src_mcp_search_rtia/rtia_mcp.py
```

Pass connection flags after the script if you are not using the defaults:

```bash
claude mcp add rtia -- python /abs/path/rtia_mcp.py \
    --cratedb-host my-host --cratedb-user me --cratedb-password secret \
    --inference-url http://my-host:8000
```

**Claude Desktop** — add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rtia": {
      "command": "python",
      "args": ["/abs/path/to/src_mcp_search_rtia/rtia_mcp.py"]
    }
  }
}
```

Then ask, for example: *"Which devices are at highest fault risk right now?"* or
*"Score DEVICE_0042 and show its recent vibration readings."*

## The data: rtia schema

The `rtia` schema (DDL in `../sql/rtia_schema_create.sql`) models five
industrial plants:

- **`plants`** — 5 facilities with `geo_location`.
- **`devices`** — 500 assets (`device_id`, `device_type`, `plant_id`, `line_id`, …).
- **`maintenance_log`** — ~1,700 work orders with a full-text `notes` column and
  a `notes_embedding FLOAT_VECTOR(384)` for semantic search.
- **`iot_data`** — sensor readings in Telegraf line-protocol shape: identity and
  state under `tags` (`tags['device_id']`, `tags['status']`,
  `tags['metric_unit']`) and numbers under `fields` (`fields['metric_value']`,
  `fields['quality_score']`); `geo_location` is generated and the table is
  `PARTITIONED BY (event_week)`.
- **`locations`** — named points plus a Bavaria `geo_area` polygon.
- **`knn_searches`** — reference query vectors keyed by `search_string`.
- **`fault_predictions`** — ML output written by the inference service.

### Query conventions the server encodes

These rules live in the server `instructions` and the `query_sql` docstring, so
the connecting model applies them:

- **Values are not Kelvin.** Each reading's unit is in `tags['metric_unit']`
  (°C, mm/s, bar, …). Report the value with its unit; never convert.
- **Default to the latest readings.** With no time range, scope `iot_data` to
  each device's most recent rows — that is where the injected faults live.
- **Don't fan out `fault_predictions`.** It is denormalised (one row per device
  per scoring run). Joining it to `iot_data` multiplies results ~1,000×; join
  the 1-row-per-device `devices` table for extra asset columns instead.
- **Geo questions use `locations`.** Use `WITHIN(geo_location, l.geo_area)` (e.g.
  Bavaria) or `DISTANCE()` against a location point rather than guessing
  coordinates.
- **Inspect `information_schema` first**, then build SELECTs only with names that
  come back — the same guardrail as the German-weather server.

## Example: score, then query the prediction

```bash
# 1. confirm the service is up
inference_health

# 2. score a device (writes to rtia.fault_predictions)
score_device device_id="DEVICE_0042"

# 3. read it back alongside asset data
query_sql statement="
  SELECT f.device_id, f.device_type, f.plant_id, f.current_status,
         f.fault_probability, f.fault_risk_label, f.scored_at
  FROM fault_predictions f
  WHERE f.fault_probability > 0.6
  ORDER BY f.fault_probability DESC
  LIMIT 20"
```
