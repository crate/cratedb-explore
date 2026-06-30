# CrateDB Explore — environment variable template.
#
# Copy this to `env.sh` (gitignored), edit the values for your cluster, then
# `source` it before running any of the examples:
#
#     cp env.example.sh env.sh
#     # edit env.sh
#     source env.sh
#
# You set ONE server, ONE user, and ONE password (the "EDIT THESE" block). The
# protocol-specific URLs each module needs are DERIVED from those below — leave
# the derived block alone. Credentials live only in CRATEDB_USER /
# CRATEDB_PASSWORD and are never embedded in the URLs, so there is exactly one
# place to change them. Each `${VAR:-default}` keeps a value you have already
# exported instead of overwriting it.

# ─── EDIT THESE ──────────────────────────────────────────────────────────────
# Your single CrateDB server + credentials. Read by every module.
export CRATEDB_HOST="${CRATEDB_HOST:-localhost}"
export CRATEDB_USER="${CRATEDB_USER:-crate}"
export CRATEDB_PASSWORD="${CRATEDB_PASSWORD:-}"   # a local single-node CrateDB has no password
export CRATEDB_SCHEME="${CRATEDB_SCHEME:-http}"   # use https for CrateDB Cloud

# KNN search CLI (sda/src/src_knn_search) extras.
export CRATEDB_DB="${CRATEDB_DB:-demo}"
export CRATEDB_FULLTEXT_COLUMNS="${CRATEDB_FULLTEXT_COLUMNS:-tourism_info,transportation,economics,introduced_species}"
# Only the semantic (embedding) mode needs a key; fulltext mode runs without one.
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-REPLACE_ME}"
export OPENAI_EMBED_MODEL="${OPENAI_EMBED_MODEL:-text-embedding-3-small}"

# Agentic RAG (rtia/src/src_rag) — needed ONLY for the Claude generation step (the
# CLI, and the UI's "Agentic" mode). The local embedding model and the UI's
# retrieval-only mode need no key. Set a real key to run the agentic loop; leave it as
# NO_API_KEY to skip it — the code treats NO_API_KEY (or unset) as "no key" and fails
# gracefully with a clear message instead of a 401.
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-NO_API_KEY}"

# Kafka stream load (sda/src/src_stream_load).
export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"
export SCHEMA_REGISTRY_URL="${SCHEMA_REGISTRY_URL:-http://localhost:8081}"
export KAFKA_GROUP_ID="${KAFKA_GROUP_ID:-crate-loader}"

# Telegraf replay (rtia/src/src_telegraf).
export TELEGRAF_URL="${TELEGRAF_URL:-http://localhost:8186/telegraf}"

# rtia MCP server: base URL of the rtia/src/src_ml inference service it proxies.
export INFERENCE_URL="${INFERENCE_URL:-http://localhost:8000}"

# ─── DERIVED — don't edit ────────────────────────────────────────────────────
# Each module wants the same server in a different shape, all built from
# CRATEDB_HOST / CRATEDB_SCHEME above. Credentials stay in CRATEDB_USER /
# CRATEDB_PASSWORD (the modules add them to the connection themselves).
#
#   CRATEDB_CLUSTER_URL  MCP servers   — HTTP _sql endpoint  (port 4200)
#   CRATEDB_URL          stream_load   — HTTP endpoint        (port 4200)
#   CRATEDB_ALCHEMY_URL  rtia/src/src_ml        — SQLAlchemy crate://  (port 4200)
#   CRATEDB_PORT         knn CLI       — PostgreSQL wire      (port 5432)
#
# Setting CRATEDB_CLUSTER_URL makes the MCP servers ignore CRATEDB_PORT (5432),
# so the 4200-vs-5432 split between the HTTP and PG consumers doesn't clash.
export CRATEDB_CLUSTER_URL="${CRATEDB_SCHEME}://${CRATEDB_HOST}:4200"
export CRATEDB_URL="${CRATEDB_CLUSTER_URL}"
# CrateDB Cloud (https) needs ?ssl=true on the SQLAlchemy URL.
if [ "${CRATEDB_SCHEME}" = "https" ]; then
  export CRATEDB_ALCHEMY_URL="crate://${CRATEDB_HOST}:4200/?ssl=true"
else
  export CRATEDB_ALCHEMY_URL="crate://${CRATEDB_HOST}:4200"
fi
export CRATEDB_PORT="5432"
