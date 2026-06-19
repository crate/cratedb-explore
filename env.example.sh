# CrateDB Explore — environment variable template.
#
# Copy this to `env.sh` (gitignored), edit the values for your cluster, then
# `source` it before running any of the examples:
#
#     cp env.example.sh env.sh
#     # edit env.sh
#     source env.sh
#
# Every value below is a MOCK / local-cluster default — nothing here is secret.
# Real credentials belong only in your own `env.sh`, never in this template.
# Each module reads only the variables it needs, so unused exports are harmless.

# ─── Shared CrateDB credentials ──────────────────────────────────────────────
# Read by: the weather load generators (src_weather), the KNN search CLI
# (src_knn_search), src_ml, and the stream_load consumer. Kept out of argv on
# purpose so they never land in shell history or `ps` output.
export CRATEDB_USER="crate"
export CRATEDB_PASSWORD=""            # a local single-node CrateDB has no password

# ─── KNN search CLI (src_knn_search) ─────────────────────────────────────────
# PostgreSQL wire protocol on 5432, demo schema.
export CRATEDB_HOST="localhost"
export CRATEDB_PORT="5432"
export CRATEDB_DB="demo"
export CRATEDB_FULLTEXT_COLUMNS="tourism_info,transportation,economics,introduced_species"
# Only the semantic (embedding) mode needs a key; fulltext mode runs without one.
export OPENAI_API_KEY="sk-REPLACE_ME"
export OPENAI_EMBED_MODEL="text-embedding-3-small"

# ─── MCP servers (src_mcp_search_german_weather / src_mcp_search_rtia) ────────
# These use CrateDB's HTTP `_sql` endpoint on 4200. This URL takes precedence
# over CRATEDB_HOST/PORT above (which the KNN CLI points at 5432), so the two
# ports don't clash. For a secured cluster embed the credentials in the URL,
# e.g. http://user:password@host:4200 — the MCP servers read auth from the URL,
# not from CRATEDB_USER/CRATEDB_PASSWORD.
export CRATEDB_CLUSTER_URL="http://localhost:4200"
# rtia server only: base URL of the src_ml inference service it proxies.
export INFERENCE_URL="http://localhost:8000"

# ─── ML pipeline (src_ml) ────────────────────────────────────────────────────
# SQLAlchemy `crate://` dialect URL (distinct from the HTTP URLs above). The
# src_ml scripts inject CRATEDB_USER / CRATEDB_PASSWORD into the connection, so
# leave credentials out of this URL.
export CRATEDB_ALCHEMY_URL="crate://localhost:4200"

# ─── Kafka stream load (src_stream_load) ─────────────────────────────────────
export KAFKA_BOOTSTRAP_SERVERS="localhost:9092"
export SCHEMA_REGISTRY_URL="http://localhost:8081"
export KAFKA_GROUP_ID="crate-loader"
# The consumer writes over CrateDB's HTTP endpoint (uses CRATEDB_USER/PASSWORD
# above for HTTP basic auth). This is an HTTP URL, not a SQLAlchemy one.
export CRATEDB_URL="http://localhost:4200"

# ─── Telegraf replay (src_telegraf) ──────────────────────────────────────────
export TELEGRAF_URL="http://localhost:8186/telegraf"
