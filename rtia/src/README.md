# rtia/src

Source modules for the **industrial-IoT / `rtia`-schema** work. Each subdirectory is a
self-contained example against a CrateDB cluster (see the repo-root `README.md` and
`CLAUDE.md` for build/run details and connection conventions).

| Directory | What it is |
| --- | --- |
| [`src_ml/`](src_ml/README.md) | Predictive-maintenance pipeline over the `rtia` schema — training scripts plus a FastAPI real-time inference service; writes `rtia.fault_predictions`. |
| [`src_mcp_search_rtia/`](src_mcp_search_rtia/RTIA_MCP.md) | Single-file `FastMCP` server exposing `query_sql` over the `rtia` schema (HTTP `_sql`) plus four tools that proxy the `src_ml` inference service for live ML scoring. |
| [`src_rag/`](src_rag/README.md) | Agentic RAG over the `rtia` schema: `claude-opus-4-8` routes per question between `semantic_search` (cached KNN over `maintenance_log.notes_embedding`), `run_sql` (read-only SQL), and `similar_devices` (device-behaviour KNN). CLI + Streamlit UI. See `src_rag/README.md`. |
| [`src_behavior_search/`](src_behavior_search/README.md) | Numeric device-behaviour similarity — the counterpart to `src_rag`'s notes search. Featurizes each device's `iot_data` window into a `FLOAT_VECTOR(9)` in `rtia.device_behavior` and KNN-matches within `device_type`; exposed in the `src_rag` loop as `similar_devices`. See `src_behavior_search/README.md`. |
| [`src_telegraf/`](src_telegraf/README.md) | Generates the IoT dataset and replays it through Telegraf (line protocol) into `rtia.iot_data`. See `src_telegraf/TELEGRAF_GUIDE.md`. |
