# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

Three functional areas, each implemented three times (Java / Python / .NET). Implementations are intentionally equivalent — when you change behaviour in one, check whether the others need the same change.

| Area | Purpose | Modules |
| --- | --- | --- |
| `src_weather/` | Load generator driving CrateDB over the PostgreSQL wire protocol with a mix of WKT geo-proximity, REGION polygon-join, and FTS `MATCH` queries. Reports latency percentiles via HdrHistogram and writes `latency_histogram.png`. | `main/java`, `main/python`, `main/dotnet` |
| `src_knn_search/` | Interactive search CLI against `demo.german_regions` — semantic via OpenAI embeddings + `KNN_MATCH`, BM25 via `MATCH`. | `main/java`, `main/python`, `main/dotnet` |
| `src_mcp_search/` | Lets Claude answer questions about the weather dataset by exposing each Grafana dashboard panel as a tool (built dynamically from `grafana/german_weather_data.json`) plus a generic `query_sql` tool. Python uses MCP via `claude-agent-sdk`; Java and .NET call Anthropic Messages directly and skip MCP. | `main/java`, `main/python`, `main/dotnet` |

Shared assets: `sql/` (DDL + DML for the `demo` schema), `data/` (JSON reference data), `grafana/german_weather_data.json` (dashboard, also the source of MCP-search panel tools), `doc/` (canonical screenshots referenced from READMEs).

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

Load generators read credentials from `CRATE_USER` / `CRATE_PASSWORD` env vars (never CLI args). MCP-search reads `ANTHROPIC_API_KEY` and CrateDB connection from flags or `CRATEDB_*` env vars.

## CrateDB connection conventions

- **Load generators** speak the PostgreSQL wire protocol on **port 5432** (Npgsql / psycopg2 / JDBC). DB name is `crate`.
- **MCP-search** uses CrateDB's **HTTP `_sql` endpoint on port 4200** instead — simpler for tool-use and matches `cratedb-mcp`'s transport. Every HTTP request sends a **`Default-Schema: demo`** header; HTTP is stateless so `SET search_path` doesn't persist across calls.
- The `demo` schema holds `climate_data` (with `geo_location geo_point`, `measurement_time timestamp`, `data['temperature'] kelvin`), `german_regions` (16 Länder with `geo_coords` polygons and full-text-indexed `economics` / `transportation` / `introduced_species` columns), and `geo_points` (727 weather-station locations with `nearest_town`).

## Critical SQL rules (mirrored in the MCP system prompt)

- **"In Germany" must be polygon-filtered**: `demo.geo_points` contains some near-border foreign towns (e.g. Tannheim in Tyrol). For any "where in Germany" question, restrict candidates with `WITHIN(c.geo_location, r.geo_coords)` joining `climate_data` to `german_regions`. Don't use `geo_points` or `DISTANCE()` alone as the country filter.
- **Temperatures are stored in Kelvin**. Always display Celsius first with Kelvin in parentheses (e.g. `-8.99 C (264.16 K)`). Never report Kelvin alone.
- **`region='ALL'`** is the Germany-wide sentinel for `*_for_region` panel tools; `'Germany'` returns no rows.

## Latency chart conventions

After a workload run, each load generator writes `latency_histogram.png` in cwd (gitignored — canonical copies live in `doc/latency_histogram_{java,python,dotnet}.png`). Same conventions across runtimes:

- X = percentile, plotted at `log10(1/(1-p/100))`, labeled `50%`, `90%`, `99%`, `99.9%`, `99.99%`.
- Y = latency in ms on log scale, with explicit 1/2/5-family ticks (`1, 2, 5, 10, 20, 50, 100, …`). Y values are clamped to a 1ms minimum so HdrHistogram's integer-ms zeros don't break the log.
- JFreeChart and ScottPlot don't honour custom tick sets through their built-in log axes, so both runtimes use a linear axis over log10-transformed data with manual tick generators. matplotlib uses native `set_xscale("log")` + `set_yscale("log")` with `FixedLocator` overrides.

## MCP-search: panels become tools

`PanelTools` (Java/.NET) and `german_weather_panels.py` walk `grafana/german_weather_data.json`, slugify each panel title into a tool name, and turn the panel's `rawSql` into a template. Grafana template variables (`$region`, `$keyword`, `$latitude`, `$longitude`, `$search_categories`) become JSON-schema parameters; the `$__timeFilter("col")` macro is rewritten at call time to `(col >= 'time_from' AND col <= 'time_to')` and adds `time_from` / `time_to` parameters.

The Python implementation routes through MCP (one stdio subprocess for `cratedb-mcp`, one in-process FastMCP server for the panel tools). The Java and .NET ports talk to Anthropic Messages directly and dispatch tools in-process — same effect, but tools are advertised under bare names instead of `mcp__cratedb__*` / `mcp__gw__*`.

## Working with this repo

- The auto-mode rule `Bash(git push origin main)` is allowed in `.claude/settings.local.json` (gitignored), so direct pushes to main don't prompt. Other risky git operations still require confirmation.
- Don't commit the per-run `src_weather/main/*/latency_histogram.png` files — they're gitignored. Update `doc/latency_histogram_*.png` instead when refreshing chart screenshots in READMEs.
