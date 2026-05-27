<img src="doc/crate-logo.svg" alt="CrateDB" width="200">

# CrateDB Explore

<img src="doc/screenshot1.png" alt="Weather monitoring dashboard" width="100%">
<img src="doc/screenshot2.png" alt="Weather monitoring key indicators" width="100%">

This project accompanies the [CrateDB Explore: IoT Analytics](https://cratedb.com/explore/iot-analytics?use-case=iot) hands-on demo. That demo walks you through real-time IoT analytics using weather monitoring data — 260k timestamped readings from 80 weather stations across Germany with temperature, humidity, and pressure values. You run hourly aggregations in under a second, execute geographic SQL queries, and connect a live Grafana dashboard, all in about 30 minutes.

The load generators in this repository let you drive that same dataset with a configurable mix of geo-proximity, multi-table join, and full-text search queries over the PostgreSQL wire protocol. Each implementation produces identical workloads and reports latency percentiles via [HdrHistogram](https://github.com/HdrHistogram/HdrHistogram).

## Weather Load Generators

| Language | Directory | Driver |
| -------- | --------- | ------ |
| [Java](src_weather/main/java/README.md) | `src_weather/main/java/` | JDBC (`postgresql`) |
| [Python](src_weather/main/python/README.md) | `src_weather/main/python/` | [psycopg2](https://www.psycopg.org/) |
| [.NET (C#)](src_weather/main/dotnet/README.md) | `src_weather/main/dotnet/` | [Npgsql](https://www.npgsql.org/) |

## KNN Search CLI

Interactive search tool for CrateDB's `german_regions` table. Supports semantic search via OpenAI embeddings + `KNN_MATCH`, and BM25 fulltext search via `MATCH` — no OpenAI key needed for fulltext mode.

| Language | Directory | Driver |
| -------- | --------- | ------ |
| [Java](src_knn_search/main/java/README.md) | `src_knn_search/main/java/` | JDBC (`postgresql`) + [Gson](https://github.com/google/gson) |
| [Python](src_knn_search/main/python/README.md) | `src_knn_search/main/python/` | [psycopg](https://www.psycopg.org/) + [OpenAI](https://github.com/openai/openai-python) |
| [.NET (C#)](src_knn_search/main/dotnet/README.md) | `src_knn_search/main/dotnet/` | [Npgsql](https://www.npgsql.org/) |

## Prerequisites

- Network access to your CrateDB cluster on port 5432
- The following tables populated in a `demo` schema:
  - `climate_data` — with `geo_location`, `measurement_time`, and `data` (object with a `temperature` field)
  - `german_regions` — with `region_name`, `geo_coords` (polygon), and `economics` (full-text indexed)
  - `geo_points` — with `geo_location` and `nearest_town`

See each implementation's README for language-specific setup and usage instructions.

## License

Apache License 2.0. See the [LICENSE](LICENSE) file.
