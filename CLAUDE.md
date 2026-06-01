# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

Three functional areas. `src_weather/` and `src_knn_search/` are each implemented three times (Java / Python / .NET) and are intentionally equivalent — when you change behaviour in one, check whether the others need the same change. `src_mcp_search/` is a single minimal Python example (no Java/.NET).

| Area | Purpose | Modules |
| --- | --- | --- |
| `src_weather/` | Load generator driving CrateDB over the PostgreSQL wire protocol with a mix of WKT geo-proximity, REGION polygon-join, and FTS `MATCH` queries. Reports latency percentiles via HdrHistogram and writes `latency_histogram.png`. | `main/java`, `main/python`, `main/dotnet` |
| `src_knn_search/` | Interactive search CLI against `demo.german_regions` — semantic via OpenAI embeddings + `KNN_MATCH`, BM25 via `MATCH`. | `main/java`, `main/python`, `main/dotnet` |
| `src_mcp_search/` | A minimal MCP server (`FastMCP`, official MCP Python SDK) exposing one `query_sql` tool over the `demo` schema via CrateDB's HTTP `_sql` endpoint. An MCP client (Claude Code / Desktop) connects to it. The only non-trivial rule — use `WITHIN` for "in Germany" questions — lives in the server's instructions + tool description. `GERMAN_WEATHER_MCP.md` is a draft cratedb.com how-to page. | `german_weather_mcp.py` (single file) |

Shared assets: `sql/` (DDL + DML for the `demo` schema), `data/` (JSON reference data), `grafana/german_weather_data.json` (Grafana dashboard), `doc/` (canonical screenshots referenced from READMEs).

## Build / run commands

**Java** — multi-module Maven from the root:
```bash
mvn compile                                      # builds all three modules
mvn -pl src_weather/main/java compile            # one module
cd src_weather/main/java && mvn -q exec:java -Dexec.args="<duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]"
```

**Python** — per-module venv:
```bash
cd src_weather/main/python
source .venv/bin/activate                        # or python -m venv .venv && pip install -r requirements.txt
python query_crate.py <duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]
```

**.NET** — per-module csproj (targets net10.0):
```bash
cd src_weather/main/dotnet
dotnet run -- <duration-s> <host> <rps> <sslmode> [TYPE:COUNT ...]
```

Load generators read credentials from `CRATE_USER` / `CRATE_PASSWORD` env vars (never CLI args). The MCP server reads its CrateDB connection from `--cratedb-*` flags or `CRATEDB_*` env vars and defaults to the demo cluster; it is launched by an MCP client (`pip install -r src_mcp_search/requirements.txt`; `claude mcp add german-weather -- python src_mcp_search/german_weather_mcp.py`).

## CrateDB connection conventions

- **Load generators** speak the PostgreSQL wire protocol on **port 5432** (Npgsql / psycopg2 / JDBC). DB name is `crate`.
- **The MCP server** uses CrateDB's **HTTP `_sql` endpoint on port 4200** instead — simpler for tool-use and matches `cratedb-mcp`'s transport. Every HTTP request sends a **`Default-Schema: demo`** header; HTTP is stateless so `SET search_path` doesn't persist across calls.
- The `demo` schema holds `climate_data` (with `geo_location geo_point`, `measurement_time timestamp`, `data['temperature'] kelvin`), `german_regions` (16 Länder with `geo_coords` polygons and full-text-indexed `economics` / `transportation` / `introduced_species` columns), and `geo_points` (727 weather-station locations with `nearest_town`).

## Critical SQL rules (mirrored in the MCP server's instructions + tool description)

- **"In Germany" must be polygon-filtered**: `demo.geo_points` contains some near-border foreign towns (e.g. Tannheim in Tyrol). For any "where in Germany" question, restrict candidates with `WITHIN(c.geo_location, r.geo_coords)` joining `climate_data` to `german_regions`. Don't use `geo_points` or `DISTANCE()` alone as the country filter.
- **Temperatures are stored in Kelvin**. Always display Celsius first with Kelvin in parentheses (e.g. `-8.99 C (264.16 K)`). Never report Kelvin alone.

## Latency chart conventions

After a workload run, each load generator writes `latency_histogram.png` in cwd (gitignored — canonical copies live in `doc/latency_histogram_{java,python,dotnet}.png`). Same conventions across runtimes:

- X = percentile, plotted at `log10(1/(1-p/100))`, labeled `50%`, `90%`, `99%`, `99.9%`, `99.99%`.
- Y = latency in ms on log scale, with explicit 1/2/5-family ticks (`1, 2, 5, 10, 20, 50, 100, …`). Y values are clamped to a 1ms minimum so HdrHistogram's integer-ms zeros don't break the log.
- JFreeChart and ScottPlot don't honour custom tick sets through their built-in log axes, so both runtimes use a linear axis over log10-transformed data with manual tick generators. matplotlib uses native `set_xscale("log")` + `set_yscale("log")` with `FixedLocator` overrides.

## MCP-search: the server

`src_mcp_search/german_weather_mcp.py` is a single-file `FastMCP` stdio server. It resolves the CrateDB connection (`--cratedb-*` flags or `CRATEDB_*` env vars, demo-cluster defaults), then exposes one `query_sql` tool that POSTs `{"stmt": ...}` to the `_sql` endpoint with the `Default-Schema: demo` header and returns the columns + rows. The data rules (Kelvin display, the `WITHIN` geo-filter) live in the server's `instructions` and the tool docstring, so the connecting model applies them. No Grafana-panel parsing, no agent loop, no Anthropic SDK.

## Working with this repo

- The auto-mode rule `Bash(git push origin main)` is allowed in `.claude/settings.local.json` (gitignored), so direct pushes to main don't prompt. Other risky git operations still require confirmation.
- Don't commit the per-run `src_weather/main/*/latency_histogram.png` files — they're gitignored. Update `doc/latency_histogram_*.png` instead when refreshing chart screenshots in READMEs.
