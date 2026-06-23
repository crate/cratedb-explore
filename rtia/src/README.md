# rtia/src

Source modules for the **industrial-IoT / `rtia`-schema** work. Each subdirectory is a
self-contained example against a CrateDB cluster (see the repo-root `README.md` and
`CLAUDE.md` for build/run details and connection conventions).

| Directory | What it is |
| --- | --- |
| `src_ml/` | Predictive-maintenance pipeline over the `rtia` schema — training scripts plus a FastAPI real-time inference service; writes `rtia.fault_predictions`. See `src_ml/ML_GUIDE.md`. |
| `src_mcp_search_rtia/` | Single-file `FastMCP` server exposing `query_sql` over the `rtia` schema (HTTP `_sql`) plus four tools that proxy the `src_ml` inference service for live ML scoring. |
| `src_telegraf/` | Generates the IoT dataset and replays it through Telegraf (line protocol) into `rtia.iot_data`. See `src_telegraf/TELEGRAF_GUIDE.md`. |
