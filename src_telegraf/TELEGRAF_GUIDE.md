# Telegraf Integration — Streaming the IoT Dataset into CrateDB

Telegraf is InfluxData's open-source collection agent. In a production IoT setup, Telegraf runs on each gateway or edge node, receives metrics from sensors, and forwards them to a time-series database. This guide shows how to use it with the demo dataset: a Python script replays the NDJSON file to a Telegraf HTTP listener, which writes to CrateDB with Telegraf's native **`outputs.cratedb`** plugin over the PostgreSQL wire protocol.

> **Despite what an LLM may tell you, CrateDB has no InfluxDB line-protocol endpoint.** There is no `/_ingest/influxdb` on port 4200, and `outputs.influxdb` cannot target CrateDB. The supported path is the `outputs.cratedb` plugin (Postgres wire, port 5432) — CrateDB's own "migrate from InfluxDB" guidance is to swap `outputs.influxdb` → `outputs.cratedb`.

This is the live ingestion path. For a one-time bulk load of the full dataset, `COPY FROM` also works against the same table — see [README.md](../README.md) Step 2.

---

## Architecture

```
[replay_to_telegraf.py]
        |
        | HTTP POST  (one JSON object per record)
        v
Telegraf  :8186/telegraf
        |
        | json_v2 parser
        | reads nested tags{} / fields{}, timestamp
        v
        | outputs.cratedb  (PostgreSQL wire, :5432)
        v
  rtia.iot_data  (hash_id / timestamp / name / tags / fields)
        |
        | geo_location GEO_POINT GENERATED from
        | fields['geo_lon'], fields['geo_lat']
```

In production, replace the replay script with your actual sensor gateway or MQTT bridge. Everything downstream (Telegraf config, CrateDB table) stays the same.

---

## What maps through Telegraf

The `outputs.cratedb` plugin writes Telegraf's standard metric model — `hash_id`, `timestamp`, `name`, plus a `tags` OBJECT and a `fields` OBJECT — which is exactly the shape of `rtia.iot_data` (see `sql/rtia_schema_create.sql`). Both ingestion paths land in the **same table**:

| Source JSON | Via Telegraf (`outputs.cratedb`) | Via COPY FROM |
| --- | --- | --- |
| `tags.device_id`, `device_type`, `plant_id`, `line_id`, `status`, `metric_unit` | `tags['…']` (OBJECT keys, indexed) | same `tags` OBJECT |
| `tags.metadata_firmware_version`, `metadata_model`, … | `tags['metadata_…']` | same |
| `fields.metric_value`, `quality_score` | `fields['…']` (DOUBLE) | same `fields` OBJECT |
| `fields.geo_lon`, `fields.geo_lat` | `fields['geo_lon']` / `fields['geo_lat']` (DOUBLE) | same |
| `geo_location` | **GENERATED** server-side from `fields['geo_lon']`/`['geo_lat']` | same generated column |
| `timestamp` | Timestamp → `"timestamp"` | TIMESTAMP WITH TIME ZONE |

Because the coordinates travel as ordinary numeric fields and `geo_location` is a `GENERATED ALWAYS AS [fields['geo_lon'], fields['geo_lat']]` column, **`GEO_POINT` works through Telegraf** — all `DISTANCE()` / `WITHIN()` geo queries run against Telegraf-ingested rows exactly as they do against `COPY FROM` rows.

---

## Prerequisites

```bash
# Telegraf (any recent version)
# https://docs.influxdata.com/telegraf/latest/install/

# Python dependency for the replay script (in a per-module venv)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Step 1 — Create the table

If you have already done other parts of this, the table rtia.iiot_data may already exist, in which case you many need to DELETE from it, as
we're loading the same data set we originally used. If you don't have rtia.iiot_data, continue. 

In the file telgraf_demo.conf, `outputs.cratedb` is configured with `table_create = false`, because the plugin's 
auto-create cannot reproduce the `GENERATED` `geo_location` / `day` columns. Create the table first by running 
the `CREATE TABLE rtia.iot_data` statement from `sql/rtia_schema_create.sql` (a `COPY FROM` load of the other rtia tables is optional — only `iot_data` is needed for this demo).



---

## Step 2 — Start Telegraf

**File:** `telegraf_demo.conf`

Telegraf is a single self-contained binary from InfluxData (not a Python package). If you don't already have it, install it first — see the [official install guide](https://docs.influxdata.com/telegraf/latest/install/). Common options:

```bash
# macOS (Homebrew)
brew install telegraf

# Debian / Ubuntu
sudo apt-get update && sudo apt-get install telegraf

# Windows (Chocolatey) — or download the .zip from the install guide and add it to PATH
choco install telegraf

# Or download a standalone binary / container image:
# https://docs.influxdata.com/telegraf/latest/install/
telegraf --version          # confirm it's on your PATH
```

Then start it with the demo config:

```bash
telegraf --config telegraf_demo.conf
```

Telegraf opens an HTTP listener on port `8186` and waits. It will print something like:

```
2025-10-01T12:00:00Z I! [agent] Starting Telegraf
2025-10-01T12:00:00Z I! [inputs.http_listener_v2] Listening on [::]:8186
```

By default the output connects to `postgres://crate@localhost:5432/crate` (local CrateDB or Docker) and writes to `rtia.iot_data`. For CrateDB Cloud, edit the `[[outputs.cratedb]]` section in `telegraf_demo.conf` — keep the credentials out of the file with environment variables and require TLS:

```toml
[[outputs.cratedb]]
  url   = "postgres://${CRATE_USER}:${CRATE_PASSWORD}@<your-cluster>.cratedb.net:5432/crate?sslmode=require"
  table = "rtia.iot_data"
```

`CRATE_USER` / `CRATE_PASSWORD` are read from the environment (Telegraf substitutes `${VAR}` at load time), so no secret is written into the config file.

---

## Step 3 — Run the replay script

**File:** `replay_to_telegraf.py`

Open a second terminal and run:

```bash
# This is a new terminal, so activate the venv from Prerequisites first
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Quick demo: 5,000 records at a visible pace
python replay_to_telegraf.py --limit 5000 --delay 0.1

# Full dataset, as fast as possible
python replay_to_telegraf.py

# Single device — useful for showing one device's history flowing in
python replay_to_telegraf.py --device DEVICE_0042 --delay 0.05

# Custom Telegraf URL
python replay_to_telegraf.py --url http://my-telegraf-host:8186/telegraf
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--url` | `http://localhost:8186/telegraf` | Telegraf HTTP listener URL |
| `--input` | `data/iot_demo_dataset.json` | Path to NDJSON file (auto-downloaded from S3 if missing) |
| `--batch` | `100` | Records per progress update / `--delay` pause (each record is its own POST) |
| `--delay` | `0` | Seconds between batches (0 = full speed) |
| `--device` | — | Filter to a single `device_id` |
| `--limit` | — | Stop after N records |

### What the script does

The script reads `iot_demo_dataset.json` line by line and POSTs each record to Telegraf as a single JSON object. The `json_v2` parser's `object` block (`path = "@this"`) parses that object into one metric, reads the nested `tags{}` and `fields{}` keys, and the `outputs.cratedb` plugin writes it to `rtia.iot_data` over the PostgreSQL wire protocol. (This input + json_v2 only accepts one object per request — a JSON array, bare or wrapped, is rejected with HTTP 400 — so records are posted individually. `--batch` controls the progress/`--delay` cadence, not the HTTP payload.)

With `--delay 0.05` and `--batch 100`, you pause briefly every 100 records — slow enough to watch the row count climb in CrateDB Admin UI while the script is still running.

---

## Step 4 — Verify in CrateDB

Once the script starts, open CrateDB Admin UI and run:

```sql
-- Row count (refresh to watch it grow)
SELECT COUNT(*) FROM rtia.iot_data;

-- Records per device
SELECT tags['device_id'] AS device_id, tags['device_type'] AS device_type,
       COUNT(*) AS readings
FROM rtia.iot_data
GROUP BY tags['device_id'], tags['device_type']
ORDER BY readings DESC
LIMIT 10;

-- Latest reading per device
SELECT tags['device_id'] AS device_id, MAX("timestamp") AS last_seen,
       last(tags['status'], "timestamp") AS last_status
FROM rtia.iot_data
GROUP BY tags['device_id']
ORDER BY last_seen DESC
LIMIT 10;

-- Geo works on Telegraf-ingested rows: geo_location is GENERATED from
-- fields['geo_lon'] / fields['geo_lat'], so DISTANCE() queries run directly.
SELECT tags['device_id'] AS device_id, geo_location,
       DISTANCE(geo_location, [9.1819, 48.7843]) AS metres_from_stuttgart
FROM rtia.iot_data
WHERE geo_location IS NOT NULL
ORDER BY metres_from_stuttgart
LIMIT 5;
```

The table is **not** auto-created (Step 1 — `table_create = false`), so it already carries its `GENERATED` `geo_location` / `day` columns. New Telegraf rows get those columns computed server-side, the same as `COPY FROM` rows.

---

## How Telegraf maps the data

Each POST is a single record, already in `{hash_id, timestamp, name, tags{}, fields{}}` shape. The `object` block with `path = "@this"` parses that whole object into one metric:

```toml
[[inputs.http_listener_v2.json_v2.object]]
  path                 = "@this"          # parse the posted object
  timestamp_key        = "timestamp"      # event time
  timestamp_format     = "2006-01-02 15:04:05"
  timestamp_timezone   = "UTC"
  disable_prepend_keys = true             # tags.device_id -> device_id (not tags_device_id)
  included_keys = ["device_id", "status", "metric_value", "geo_lon"]  # …allow-list, rest dropped
  tags          = ["device_id", "status"]                             # …which of those are tags
  [inputs.http_listener_v2.json_v2.object.fields]
    metric_value = "float"
    geo_lon      = "float"
    geo_lat      = "float"
```

- **`path = "@this"`** selects the posted object. This input + json_v2 parses **one object per request** — a JSON array (bare `[ … ]` or wrapped `{"metrics":[ … ]}`) is rejected with HTTP 400 — so the replay script posts records individually.
- **`disable_prepend_keys = true`** flattens the nested `tags.*` / `fields.*` keys to their leaf names, so they match the column names the queries use.
- **`included_keys`** is the allow-list — only these keys become tags/fields. It drops the per-element `hash_id` and `name`, which the `outputs.cratedb` plugin sets itself (hash_id computed from name + tags; name from `measurement_name`).
- **`tags = [...]`** marks which included keys are tags (`tags['device_id']`, indexed for `WHERE` / `GROUP BY`); the rest are fields.
- The **`object.fields`** type map forces `float`, so Telegraf doesn't infer integer and lose precision — and `geo_lon` / `geo_lat` stay the doubles the `geo_location` GENERATED column needs.

`2006-01-02 15:04:05` is Go's reference time and matches the format in `iot_demo_dataset.json`; the timestamps are naive, so `timestamp_timezone = "UTC"` pins them.

---

## Telegraf config reference

**File:** `telegraf_demo.conf`

The config has three sections:

```
[agent]                       global settings — flush interval, precision
[[inputs.http_listener_v2]]   HTTP endpoint + json_v2 tags/fields mapping
[[outputs.cratedb]]           CrateDB destination (PostgreSQL wire, :5432)
```

To debug what Telegraf parsed before it hits CrateDB, uncomment the `[[outputs.file]]` block at the bottom of the config. With `data_format = "influx"` it prints each metric as line protocol to stdout, so you can see exactly which tags and fields were extracted.

---

## File reference

```
src_telegraf/
├── telegraf_demo.conf         Telegraf configuration (http_listener_v2 + outputs.cratedb)
├── replay_to_telegraf.py      Replay script (reads NDJSON, POSTs to Telegraf)
├── requirements.txt           Python dependency for the replay script (requests)
└── TELEGRAF_GUIDE.md          This file

sql/rtia_schema_create.sql     CREATE TABLE rtia.iot_data (run before Step 2)
```
