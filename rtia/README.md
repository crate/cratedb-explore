# rtia

Self-contained project tree for the **industrial-IoT / `rtia`-schema** work. See the
repo-root `README.md` and `CLAUDE.md` for build/run details and connection conventions.

| Directory | What it holds |
| --- | --- |
| `src/` | The source modules (ML pipeline, MCP server, Telegraf ingest) — see `src/README.md`. |
| `sql/` | `rtia`-schema DDL (`rtia_schema_create.sql`) plus example query files. |
| `data/` | The `plants.json` / `devices.json` / `iot_demo_dataset.json` datasets. |
| `grafana/` | `rtia.json` dashboard. |
