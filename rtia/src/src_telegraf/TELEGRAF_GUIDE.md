# Telegraf Integration — Streaming the IoT Dataset into CrateDB

Telegraf is InfluxData's open-source collection agent. In a production IoT setup, Telegraf runs on each gateway or edge node, receives metrics from sensors, and forwards them to a time-series database. This guide shows how to use it with the demo dataset: a Python script replays the NDJSON file to a Telegraf HTTP listener, which writes to CrateDB with Telegraf's native **`outputs.cratedb`** plugin over the PostgreSQL wire protocol.

> **Despite what an LLM may tell you, CrateDB has no InfluxDB line-protocol endpoint.** There is no `/_ingest/influxdb` on port 4200, and `outputs.influxdb` cannot target CrateDB. The supported path is the `outputs.cratedb` plugin (Postgres wire, port 5432) — CrateDB's own "migrate from InfluxDB" guidance is to swap `outputs.influxdb` → `outputs.cratedb`.

This is the live ingestion path. For a one-time bulk load of the full dataset, `COPY FROM` also works against the same table — see the [RTIA section of the README](../../../README.md#real-time-industrial-analytics-rtia) (the `COPY FROM` statements in `rtia/sql/rtia_schema_create.sql`).

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

The `outputs.cratedb` plugin writes Telegraf's standard metric model — `hash_id`, `timestamp`, `name`, plus a `tags` OBJECT and a `fields` OBJECT — which is exactly the shape of `rtia.iot_data` (see `rtia/sql/rtia_schema_create.sql`). Both ingestion paths land in the **same table**:

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

If you have already done other parts of this, the table `rtia.iot_data` may already exist, in which case you may want to `TRUNCATE TABLE rtia.iot_data;` first, since
we're loading the same data set we originally used. If you don't have `rtia.iot_data`, continue.

In the file `telegraf_demo.conf`, `outputs.cratedb` is configured with `table_create = false`, because the plugin's 
auto-create cannot reproduce the `GENERATED` `geo_location` / `day` columns. Create the table first by running 
the `CREATE TABLE rtia.iot_data` statement from `rtia/sql/rtia_schema_create.sql` (a `COPY FROM` load of the other rtia tables is optional — only `iot_data` is needed for this demo).



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
  url   = "postgres://${CRATEDB_USER}:${CRATEDB_PASSWORD}@<your-cluster>.cratedb.net:5432/crate?sslmode=require"
  table = "rtia.iot_data"
```

`CRATEDB_USER` / `CRATEDB_PASSWORD` are read from the environment (Telegraf substitutes `${VAR}` at load time), so no secret is written into the config file.

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
| `--input` | `rtia/data/iot_demo_dataset.json` | Path to NDJSON file (auto-downloaded from S3 if missing) |
| `--batch` | `100` | Records per progress update / `--delay` pause (each record is its own POST) |
| `--delay` | `0` | Seconds between batches (0 = full speed) |
| `--device` | — | Filter to a single `device_id` |
| `--limit` | — | Stop after N records |

### What the script does

The script reads `iot_demo_dataset.json` line by line and POSTs each record to Telegraf as a single JSON object. The `json_v2` parser's explicit `tag` / `field` selectors parse it into one metric, reading the nested `tags{}` and `fields{}` keys, and the `outputs.cratedb` plugin writes it to `rtia.iot_data` over the PostgreSQL wire protocol. (This input + json_v2 only accepts one object per request — a JSON array, bare or wrapped, is rejected with HTTP 400 — so records are posted individually. `--batch` controls the progress/`--delay` cadence, not the HTTP payload.)

With `--delay 0.05` and `--batch 100`, you pause briefly every 100 records — slow enough to watch the row count climb in CrateDB Admin UI while the script is still running.

---

## Step 4 — Verify in CrateDB

Once the script starts, open CrateDB Admin UI and run:

```sql
-- Row count + device count. REFRESH makes just-written rows visible (CrateDB's
-- table refresh_interval defaults to 1s). A healthy load shows the full row
-- count AND >1 distinct device — if device count is 1 (or device_id is null),
-- the tags aren't reaching the metric (see "Reliability & gotchas").
REFRESH TABLE rtia.iot_data;
SELECT COUNT(*)                          AS rows,
       COUNT(DISTINCT tags['device_id']) AS devices
FROM rtia.iot_data;

-- Records per device
SELECT tags['device_id'] AS device_id, tags['device_type'] AS device_type,
       COUNT(*) AS readings
FROM rtia.iot_data
GROUP BY tags['device_id'], tags['device_type']
ORDER BY readings DESC
LIMIT 10;

-- Latest reading per device
 SELECT device_id, last_seen, status AS last_status
 FROM (
    SELECT tags['device_id'] AS device_id,
           "timestamp"        AS last_seen,
           tags['status']     AS status,
              ROW_NUMBER() OVER (PARTITION BY tags['device_id']
                              ORDER BY "timestamp" DESC) AS rn
    FROM rtia.iot_data
  ) t
  WHERE rn = 1
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

Each POST is a single record, already in `{hash_id, timestamp, name, tags{}, fields{}}` shape. Explicit `tag` / `field` selectors, each with a GJSON `path` into the nested objects, parse it into **one** metric carrying both the tags and the fields:

```toml
[[inputs.http_listener_v2.json_v2]]
  measurement_name   = "iot_data"
  timestamp_path     = "timestamp"
  timestamp_format   = "2006-01-02 15:04:05"
  timestamp_timezone = "UTC"

  [[inputs.http_listener_v2.json_v2.tag]]
    path   = "tags.device_id"        # GJSON into the nested tags{}
    rename = "device_id"             # -> tags['device_id'] in CrateDB
  # … one block per tag (device_type, plant_id, status, metric_unit, metadata_*) …

  [[inputs.http_listener_v2.json_v2.field]]
    path   = "fields.metric_value"   # GJSON into the nested fields{}
    rename = "metric_value"
    type   = "float"
  # … one block per field (quality_score, geo_lon, geo_lat) …
```

- **`tag` / `field` selectors with GJSON `path`** (`tags.device_id`, `fields.metric_value`) read straight into the nested objects; `rename` sets the CrateDB key. Crucially they all attach to the **same metric**, so the device tags travel with the data — `device_id` becomes part of the plugin's `hash_id`, keeping every device/timestamp row distinct.
- **`type = "float"`** forces `DOUBLE PRECISION`, so Telegraf doesn't infer integer and lose precision — and `geo_lon` / `geo_lat` stay the doubles the `geo_location` GENERATED column needs.
- **`timestamp_path`** reads the event time; `2006-01-02 15:04:05` is Go's reference time and matches `iot_demo_dataset.json`. The timestamps are naive, so `timestamp_timezone = "UTC"` pins them.

> **Two json_v2 traps to avoid** (both cost real debugging time here):
> 1. **Don't split into separate `path="tags"` / `path="fields"` object blocks.** json_v2 emits those as *two* metrics; the tags-only one has no field and is dropped, and the surviving data metric loses `device_id`. Every row then hashes to the same `hash_id`, and CrateDB collapses them to one row per timestamp — silent row loss.
> 2. **Don't use `path="@this"` with `included_keys`.** The allow-list doesn't match the flattened nested keys, so the metric ends up with no fields and is dropped — no data, no log lines.
>
> The explicit selectors above sidestep both.

---

## Telegraf config reference

**File:** `telegraf_demo.conf`

The config has three sections:

```
[agent]                       global settings — flush interval, precision
[[inputs.http_listener_v2]]   HTTP endpoint + json_v2 tags/fields mapping
[[outputs.cratedb]]           CrateDB destination (PostgreSQL wire, :5432)
```

To debug what Telegraf parsed before it hits CrateDB, uncomment the `[[outputs.file]]` block at the bottom of the config. With `data_format = "influx"` it prints each metric as line protocol to stdout, so you can see exactly which tags and fields were extracted (e.g. `iot_data,device_id=DEVICE_0001,status=normal,… metric_value=62.7,… <ts>`). If `device_id=` is missing from that line, the parser is wrong — see below.

---

## Reliability & gotchas

Hard-won notes — each of these caused a real "rows are missing" head-scratch:

**1. One metric per record — use explicit `tag`/`field` selectors.** The dataset nests `tags{}` and `fields{}`. Two json_v2 layouts *look* right but silently lose data:
- Separate `path="tags"` and `path="fields"` object blocks → json_v2 emits **two** metrics; the tags-only one is dropped (no field) and the data metric loses `device_id`, so every row hashes to the same `hash_id` and CrateDB keeps **one row per timestamp**.
- `path="@this"` + `included_keys` → the allow-list doesn't match the flattened nested keys, the metric has **no fields**, and is dropped — you get no rows and no log lines.

The working form is the explicit `tag`/`field` selectors with GJSON paths (`tags.device_id`, `fields.metric_value`) shown above. **Verify with `COUNT(DISTINCT tags['device_id'])` — it must be > 1.**

**2. First load into an empty table is slow (partition creation).** `rtia.iot_data` is `PARTITIONED BY (event_week)` and the dataset spans ~200 days, so the first flushes create ~30 weekly partitions in one insert. With a short output `timeout` those writes are killed mid-flight, retried, and the buffer overflows → dropped rows (`timeout: context deadline exceeded` in the Telegraf log). The config ships with headroom for this: `outputs.cratedb` `timeout = "120s"`, `flush_interval = "10s"`, `metric_batch_size = 10000`, `metric_buffer_limit = 500000`. To avoid the slow first load entirely, **pre-create the partitions** with the README `COPY FROM` bulk load first, or **rate-limit** the producer (`--delay 0.05+`).

**3. Counts lag — `REFRESH TABLE` before counting.** Telegraf flushes on an interval and writes asynchronously, and CrateDB's `refresh_interval` (default 1s) delays query visibility. A `COUNT(*)` run the instant the script finishes undercounts. Wait for the Telegraf `did not complete within its flush interval` warnings to stop, then `REFRESH TABLE rtia.iot_data;` and count.

**4. Plain `INSERT`, no upsert.** `outputs.cratedb` issues a plain `INSERT` (no `ON CONFLICT`), so a duplicate-PK row is rejected per-row while the rest of the batch succeeds. Re-running the same data is therefore safe (the already-present rows are rejected, missing ones fill in) — but it also means a botched earlier run can leave rows you should clear with `TRUNCATE TABLE rtia.iot_data;` before re-testing.

---

## File reference

```
rtia/src/src_telegraf/
├── telegraf_demo.conf         Telegraf configuration (http_listener_v2 + outputs.cratedb)
├── replay_to_telegraf.py      Replay script (reads NDJSON, POSTs to Telegraf)
├── requirements.txt           Python dependency for the replay script (requests)
└── TELEGRAF_GUIDE.md          This file

rtia/sql/rtia_schema_create.sql     CREATE TABLE rtia.iot_data (run before Step 2)
```
