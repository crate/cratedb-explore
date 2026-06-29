# src_rag — agentic RAG over the rtia schema

In this example we show Retrieval-Augmented Generation over the `rtia` schema where 
**Claude chooses how and what to retrieve**. It gets three tools and routes per 
question — fusing this repo's patterns (the src_rag KNN path, the `src_mcp_search_rtia` `query_sql` path, and the `src_behavior_search` device-similarity KNN)
into one agentic tool-use loop:

- **`semantic_search`** — embed the question (all-MiniLM-L6-v2, 384-dim) and
  `KNN_MATCH(notes_embedding, …)` the free-text maintenance `notes`. For "what
  kind of failure / describe / find similar" questions.
- **`run_sql`** — a read-only `SELECT` over the `rtia` schema. For "which / how
  many / by technician / totals" questions that need an exact set or aggregate,
  which a KNN top-k sample can't give.
- **`similar_devices`** — given a `device_id`, `KNN_MATCH` over `rtia.device_behavior`
  for the devices whose recent sensor behaviour is most similar, scoped to the same
  `device_type` (with each neighbour's plant/line and fault counts). For "which
  devices behave like X / others with this profile" questions — the numeric
  counterpart to `semantic_search`, built by the `src_behavior_search` module.

Claude may use either or both, then answers grounded in the tool results
(`claude-opus-4-8`, adaptive thinking). Two entry points share one module:

| File | What it is |
| --- | --- |
| `rtia_rag.py` | The pipeline + a CLI. Manual tool-use loop with the three tool handlers; `run_sql` and `similar_devices` execute over the same psycopg connection (read-only guard / KNN over `device_behavior`) and `semantic_search` reuses the query-embedding cache. |
| `rtia_rag_ui.py` | A Streamlit front-end that renders the tool trace (cache hit/miss, matched work orders, SQL + rows, similar-device neighbours) and the grounded answer. Unchecking "agentic" falls back to a single `semantic_search` (no Claude). |

## Prerequisites and a minor warning

In order to use this you need an Anthropic API key, exported as `ANTHROPIC_API_KEY`
in your environment. You also need `CRATEDB_HOST`, `CRATEDB_USER` and
`CRATEDB_PASSWORD` set to real values.

There are two separate steps here, and one of them costs money. *Your money*, to be precise:

- **Embedding the question is free and runs locally.** The all-MiniLM-L6-v2 model
  (sentence-transformers) runs on your machine — no API key, no charge — to turn
  the question into a 384-dim vector, which `KNN_MATCH` then compares against
  `notes_embedding` in `rtia.maintenance_log`. The `rtia.knn_searches` cache stores
  that local vector keyed by the query string, so a repeated question skips
  **recomputing the embedding** (it does not affect the Anthropic bill).
- **The Anthropic API is what costs *you* money.** Each question runs an agentic
  `claude-opus-4-8` loop that reasons and calls the tools — and *that* is billed
  per call. It is **not** cached by `knn_searches`, so every question you ask pays
  for fresh Claude calls (uncheck "agentic" in the UI for a free, embedding-only
  search).

## The query-embedding cache

`rtia.knn_searches` (`search_string TEXT PRIMARY KEY`, `embedding FLOAT_VECTOR(384)`)
doubles as a read-through / write-back cache. `get_query_embedding()`:

1. normalizes the question (lowercase, whitespace-collapsed) into the cache key,
2. looks it up by `search_string` — a hit reuses the stored vector,
3. on a miss, embeds with all-MiniLM-L6-v2 (normalized, to match the stored
   corpus) and upserts the row so the next identical query hits.

The curated rows (`thermal_event`, `calibration_drift`, …) keep their
`query_name`; UI-generated rows leave it `NULL`.

## Run

```bash
pip install -r requirements.txt
export CRATEDB_HOST=... CRATEDB_USER=... CRATEDB_PASSWORD=... ANTHROPIC_API_KEY=...

# CLI
python rtia_rag.py --host "$CRATEDB_HOST"
echo "thermal runaway on temperature sensors" | python rtia_rag.py --host "$CRATEDB_HOST"

# Web UI
streamlit run rtia_rag_ui.py
```

Connection uses the PostgreSQL wire protocol on **5432** (`psycopg`), default
schema `rtia`. Anthropic and CrateDB credentials come from env vars; the
embedding model runs locally (no key). The password var is **`CRATEDB_PASSWORD`**
(not `CRATEDB_PASS`) — a mismatch connects with an empty password and the query
fails. Run `streamlit` from this directory so it picks up `.streamlit/config.toml`
(it disables the source watcher to silence the transformers/torchvision import noise).

The `similar_devices` tool reads `rtia.device_behavior`, so build that table first
with the `src_behavior_search` module's `backfill.py` (see its README); without it,
the tool returns a "device not found" error while the other two tools still work.

## Caveats

- **Cache-key normalization is exact-match.** `lower()` + whitespace-collapse
  only — paraphrases ("bearing failure" vs "bearings failing") still miss. That
  sets the hit rate; tune `normalize_key()` if you want more aggressive folding.
- **Vector round-trips rely on psycopg adapting a Python `float` list** to/from
  `FLOAT_VECTOR(384)` for both `KNN_MATCH` params and the cache `INSERT`. Verified
  against a live cluster: both the read and write paths work; the stored vector
  round-trips to within float32 precision (~1e-8).
- **CrateDB is eventually consistent**: a freshly stored embedding isn't
  queryable for ~1s, and the `knn_searches` count can lag by one. Harmless — the
  request already holds the vector in memory.
