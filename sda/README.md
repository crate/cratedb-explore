# sda

Self-contained project tree for the **german-weather / `demo`-schema** work. See the
repo-root `README.md` and `CLAUDE.md` for build/run details and connection conventions.

| Directory | What it holds |
| --- | --- |
| `src/` | The source modules (load generator, KNN search CLI, MCP server, Kafka stream-load) — see `src/README.md`. |
| `sql/` | `demo`-schema DDL + DML (`climate_data`, `german_regions`, `geo_points`). |
| `data/` | Reference JSON datasets loaded into the `demo` schema. |
| `grafana/` | `german_weather_data.json` dashboard. |
| `doc/` | Canonical screenshots referenced from the READMEs (latency charts, etc.). |
