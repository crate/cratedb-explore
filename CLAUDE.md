# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

`sda/src/src_weather/` and `sda/src/src_knn_search/` are each implemented three times (Java / Python / .NET) and are intentionally equivalent — when you change behaviour in one, check whether the others need the same change. `sda/src/src_mcp_search_german_weather/`, `rtia/src/src_mcp_search_rtia/`, and `sda/src/src_stream_load/` are each a single minimal Python example (no Java/.NET). `rtia/src/src_ml/` is the predictive-maintenance pipeline over the `rtia` schema (training + a FastAPI inference service); see `rtia/src/src_ml/README.md`. `rtia/src/src_rag/` is a Python-only RAG demo over `rtia.maintenance_log` (KNN retrieval + Claude generation) with a Streamlit UI; see `rtia/src/src_rag/README.md`. `rtia/src/src_behavior_search/` is its numeric counterpart — it featurizes each device's `iot_data` window into a behaviour vector and `KNN_MATCH`es them to find devices behaving alike (exposed in the src_rag loop as the `similar_devices` tool); see `rtia/src/src_behavior_search/README.md`.

| Area | Purpose | Modules |
| --- | --- | --- |
| [`sda/src/src_weather/`](sda/src/src_weather/) | Load generator driving CrateDB over the PostgreSQL wire protocol with a mix of WKT geo-proximity, REGION polygon-join, and FTS `MATCH` queries. Reports latency percentiles via HdrHistogram and writes `latency_histogram.png`. | `main/java`, `main/python`, `main/dotnet` |
| [`sda/src/src_knn_search/`](sda/src/src_knn_search/) | Interactive search CLI against `demo.german_regions` — semantic via OpenAI embeddings + `KNN_MATCH`, BM25 via `MATCH`. | `main/java`, `main/python`, `main/dotnet` |
| [`sda/src/src_mcp_search_german_weather/`](sda/src/src_mcp_search_german_weather/) | A minimal MCP server (`FastMCP`, official MCP Python SDK) exposing one `query_sql` tool over the `demo` schema via CrateDB's HTTP `_sql` endpoint. An MCP client (Claude Code / Desktop) connects to it. The only non-trivial rule — use `WITHIN` for "in Germany" questions — lives in the server's instructions + tool description. `GERMAN_WEATHER_MCP.md` is a draft cratedb.com how-to page. | `german_weather_mcp.py` (single file) |
| [`rtia/src/src_mcp_search_rtia/`](rtia/src/src_mcp_search_rtia/) | Sibling MCP server over the `rtia` industrial-IoT schema (same `query_sql`-over-`_sql` shape, `Default-Schema: rtia`). Adds four tools — `inference_health`, `score_device`, `score_batch`, `fleet_high_risk` — that proxy the `src_ml` FastAPI service (`realtime_inference.py`, on `http://localhost:8000` by default via `--inference-url` / `INFERENCE_URL`) so the client can trigger live ML scoring. `README.md` is the how-to walkthrough. | `rtia_mcp.py` (single file) |
| [`rtia/src/src_rag/`](rtia/src/src_rag/) | RAG over `rtia.maintenance_log`: embeds the question and KNN-matches the maintenance `notes`, then asks `claude-opus-4-8` for a grounded, work-order-citing answer. The query embedding (all-MiniLM-L6-v2, 384-dim) is cached in `rtia.knn_searches` as a read-through/write-back cache keyed by the `search_string` PRIMARY KEY — hit reuses the stored vector, miss embeds + upserts. Speaks the PostgreSQL wire protocol (5432, `psycopg`), default schema `rtia`. CLI plus a Streamlit UI that surfaces the cache hit/miss. The loop is agentic — Claude also has `run_sql` (read-only SELECT) and `similar_devices` (numeric behaviour KNN, backed by `src_behavior_search`) tools and routes per question. | `rtia_rag.py`, `rtia_rag_ui.py` |
| [`rtia/src/src_behavior_search/`](rtia/src/src_behavior_search/) | Numeric "find devices behaving alike" KNN — the counterpart to src_rag's notes search. Featurizes each device's `iot_data` window into a 9-stat behaviour vector (`mean/std/min/max/range/p05/p50/p95/trend_slope`, z-scored **within `device_type`**), stores one row per device in `rtia.device_behavior` (`FLOAT_VECTOR(9)`), and `KNN_MATCH`es scoped to the device's `device_type` (each device measures one metric; units differ across types). `status` counts ride along as a held-out validation label, never a feature. `validate.py` is a go/no-go harness (faulting-vs-healthy separation + clone-and-perturb self-match) — run it before building. Exposed in the src_rag loop as `similar_devices`. | `behavior_features.py`, `validate.py`, `backfill.py`, `similar.py` |
| [`sda/src/src_stream_load/`](sda/src/src_stream_load/) | A producer that streams the `climate_data` dataset (from S3) into Kafka, **split by latitude into three bands each in a different wire format** (northern→Avro, central→JSON, southern→Protobuf — one topic each), and a consumer that reads all three bands back out of Kafka into `demo.climate_data`. The stream is rate-limitable (producer) / optionally tailable (consumer). Avro+Protobuf always need a Schema Registry. The producer's destination sits behind a `StreamSink` ABC (`sinks.py`) so a non-Kafka platform can be swapped in. | `stream_load_into_kafka.py`, `stream_from_kafka_into_crate.py`, `sinks.py`, `serializers.py`, `schemas/` |

The repo is split into two self-contained project trees, **`sda/`** (the german-weather / `demo`-schema work) and **`rtia/`** (the industrial-IoT / `rtia`-schema work); each holds its modules under `<project>/src/` plus its own `sql/`, `data/`, and `grafana/`. So `sda/sql/` carries the `demo`-schema DDL+DML and `sda/data/` its reference JSON; `rtia/sql/` carries `rtia_schema_create.sql` and friends, `rtia/data/` the `plants.json`/`devices.json`/`iot_demo_dataset.json` datasets, and `rtia/grafana/rtia.json` / `sda/grafana/german_weather_data.json` the dashboards, and `sda/doc/` the canonical screenshots referenced from READMEs (all sda/weather-related). Repo-wide files stay at the root: `README.md`, `LICENSE`, `env.example.sh`, and the parent Maven `pom.xml`.

## Build / run commands

**Java** — multi-module Maven from the root:
```bash
mvn compile                                      # builds both Java modules (weather + knn_search)
mvn -pl sda/src/src_weather/main/java compile            # one module
cd sda/src/src_weather/main/java && mvn -q exec:java -Dexec.args="<duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]"
```

**Python** — per-module venv:
```bash
cd sda/src/src_weather/main/python
source .venv/bin/activate                        # or python -m venv .venv && pip install -r requirements.txt
python query_crate.py <duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]
```

**.NET** — per-module csproj (targets net10.0):
```bash
cd sda/src/src_weather/main/dotnet
dotnet run -- <duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]
```

Load generators read credentials from `CRATEDB_USER` / `CRATEDB_PASSWORD` env vars (never CLI args). Each MCP server reads its CrateDB connection from `--cratedb-*` flags or `CRATEDB_*` env vars and defaults to `crate@localhost:4200`; it is launched by an MCP client:
- German weather: `pip install -r sda/src/src_mcp_search_german_weather/requirements.txt`; `claude mcp add german-weather -- python sda/src/src_mcp_search_german_weather/german_weather_mcp.py`
- rtia: `pip install -r rtia/src/src_mcp_search_rtia/requirements.txt`; `claude mcp add rtia -- python rtia/src/src_mcp_search_rtia/rtia_mcp.py`. Its scoring tools also need the `src_ml` service running (`cd rtia/src/src_ml && uvicorn realtime_inference:app --port 8000`); point the server elsewhere with `--inference-url` / `INFERENCE_URL`.

## CrateDB connection conventions

- **`env.example.sh`** (repo root) is the canonical template of *every* env var the repo reads (CrateDB creds/URLs, OpenAI, Kafka, Telegraf). Users `cp env.example.sh env.sh` (gitignored), edit, and `source env.sh`. It has a **single source of truth**: you set `CRATEDB_HOST`/`CRATEDB_USER`/`CRATEDB_PASSWORD`/`CRATEDB_SCHEME` once (the *EDIT THESE* block), and the protocol-specific vars are **derived** from them (the *DERIVED* block) — when you add or rename a var, update the right block. The three CrateDB URL vars are intentionally distinct in *format*: `CRATEDB_CLUSTER_URL` (MCP servers, HTTP `_sql`), `CRATEDB_ALCHEMY_URL` (`src_ml`, SQLAlchemy `crate://`), `CRATEDB_URL` (`src_stream_load`, HTTP) — all derived from the one host. Setting `CRATEDB_CLUSTER_URL` makes the MCP servers ignore `CRATEDB_HOST`/`CRATEDB_PORT` (which the KNN CLI points at 5432), so the 4200/5432 split doesn't clash.
- **Credentials are one pair, everywhere**: every module (load generators, KNN CLI, `src_ml`, `src_stream_load`, and the MCP servers) reads auth from `CRATEDB_USER`/`CRATEDB_PASSWORD` and adds it to the connection itself — credentials are never embedded in the URL vars. The MCP servers' `resolve_endpoint` and `rtia/src/src_ml/data_source.make_engine` both inject these env creds only when the URL carries no userinfo.
- **Load generators** speak the PostgreSQL wire protocol on **port 5432** (Npgsql / psycopg2 / JDBC). DB name is `crate`.
- **The MCP servers** use CrateDB's **HTTP `_sql` endpoint on port 4200** instead — simpler for tool-use and matches `cratedb-mcp`'s transport. Every HTTP request sends a **`Default-Schema`** header (`demo` for german-weather, `rtia` for rtia); HTTP is stateless so `SET search_path` doesn't persist across calls.
- The `demo` schema holds `climate_data` (with `geo_location geo_point`, `measurement_time timestamp`, `data['temperature'] kelvin`), `german_regions` (16 Länder with `geo_coords` polygons and full-text-indexed `economics` / `transportation` / `introduced_species` columns), and `geo_points` (726 weather-station locations with `nearest_town`).
- The `rtia` schema (see `rtia/sql/rtia_schema_create.sql`) holds `plants`, `devices`, `maintenance_log` (full-text `notes` + `notes_embedding FLOAT_VECTOR(384)`), `iot_data` (Telegraf line-protocol shape: `tags['device_id']`/`tags['status']`/`tags['metric_unit']`, `fields['metric_value']`/`fields['quality_score']`, generated `geo_location`, `PARTITIONED BY (event_week)`), `locations` (points + a Bavaria `geo_area` polygon), `knn_searches`, `fault_predictions` (ML output), and `device_behavior` (one `behavior_vector FLOAT_VECTOR(9)` per device for the `src_behavior_search` similarity KNN, populated by that module's `backfill.py`).

## Critical SQL rules (mirrored in the MCP servers' instructions + tool descriptions)

`demo` schema (german-weather server):
- **"In Germany" must be polygon-filtered**: `demo.geo_points` contains some near-border foreign towns (e.g. Tannheim in Tyrol). For any "where in Germany" question, restrict candidates with `WITHIN(c.geo_location, r.geo_coords)` joining `climate_data` to `german_regions`. Don't use `geo_points` or `DISTANCE()` alone as the country filter.
- **Temperatures are stored in Kelvin**. Always display Celsius first with Kelvin in parentheses (e.g. `-8.99 C (264.16 K)`). Never report Kelvin alone.

`rtia` schema (rtia server) — note these intentionally differ from the `demo` rules:
- **Values are NOT Kelvin**: each `iot_data` reading's unit is in `tags['metric_unit']` (C, mm/s, bar, …). Report the value with its unit; never convert.
- **Default to the latest readings**: with no time range, scope `iot_data` to each device's most recent rows — the injected faults are the latest data.
- **Don't fan out `fault_predictions`**: it's denormalised (one row per device per run); joining it to `iot_data` multiplies results ~1,000×. Join the 1-row-per-device `devices` for extra asset columns.
- **Geo via `rtia.locations`**: use `WITHIN(geo_location, l.geo_area)` (e.g. Bavaria) or `DISTANCE()` against a location point rather than guessing coordinates.

## Latency chart conventions

After a workload run, each load generator writes `latency_histogram.png` in cwd (gitignored — canonical copies live in `sda/doc/latency_histogram_{java,python,dotnet}.png`). Same conventions across runtimes:

- X = percentile, plotted at `log10(1/(1-p/100))`, labeled `50%`, `90%`, `99%`, `99.9%`, `99.99%`.
- Y = latency in ms on log scale, with explicit 1/2/5-family ticks (`1, 2, 5, 10, 20, 50, 100, …`). Y values are clamped to a 1ms minimum so HdrHistogram's integer-ms zeros don't break the log.
- JFreeChart and ScottPlot don't honour custom tick sets through their built-in log axes, so both runtimes use a linear axis over log10-transformed data with manual tick generators. matplotlib uses native `set_xscale("log")` + `set_yscale("log")` with `FixedLocator` overrides.

## MCP-search: the server

`sda/src/src_mcp_search_german_weather/german_weather_mcp.py` is a single-file `FastMCP` stdio server. It resolves the CrateDB connection (`--cratedb-*` flags or `CRATEDB_*` env vars, demo-cluster defaults), then exposes one `query_sql` tool that POSTs `{"stmt": ...}` to the `_sql` endpoint with the `Default-Schema: demo` header and returns the columns + rows. The data rules (Kelvin display, the `WITHIN` geo-filter) live in the server's `instructions` and the tool docstring, so the connecting model applies them. No Grafana-panel parsing, no agent loop, no Anthropic SDK.

## Working with this repo

- The auto-mode rule `Bash(git push origin main)` is allowed in `.claude/settings.local.json` (gitignored), so direct pushes to main don't prompt. Other risky git operations still require confirmation.
- Don't commit the per-run `sda/src/src_weather/main/*/latency_histogram.png` files — they're gitignored. Update `sda/doc/latency_histogram_*.png` instead when refreshing chart screenshots in READMEs.
