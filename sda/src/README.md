# sda/src

Source modules for the **german-weather / `demo`-schema** work. Each subdirectory is a
self-contained example against a CrateDB cluster (see the repo-root `README.md` and
`CLAUDE.md` for build/run details and connection conventions).

| Directory | What it is |
| --- | --- |
| `src_weather/` | Load generator (Java / Python / .NET) driving CrateDB over the PostgreSQL wire protocol with geo-proximity, polygon-join, and full-text `MATCH` queries; reports latency percentiles. |
| `src_knn_search/` | Interactive search CLI (Java / Python / .NET) over `demo.german_regions` — semantic via OpenAI embeddings + `KNN_MATCH`, BM25 via `MATCH`. |
| `src_mcp_search_german_weather/` | Single-file `FastMCP` server exposing one `query_sql` tool over the `demo` schema via CrateDB's HTTP `_sql` endpoint. |
| `src_stream_load/` | Streams the `climate_data` dataset into Kafka split into three wire formats (Avro / JSON / Protobuf) and consumes it back into `demo.climate_data`. |
