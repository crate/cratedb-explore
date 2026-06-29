# src_rag — RAG over the rtia maintenance log

Retrieval-Augmented Generation over `rtia.maintenance_log`: embed a question,
KNN-match it against the free-text maintenance `notes`, and let Claude answer
over the matched work orders. Two entry points share one module:

| File | What it is |
| --- | --- |
| `rtia_rag.py` | The pipeline + a CLI. Embed (all-MiniLM-L6-v2, 384-dim) → `KNN_MATCH(notes_embedding, …)` → `claude-opus-4-8` grounded answer. Also holds the query-embedding cache. |
| `rtia_rag_ui.py` | A Streamlit front-end over `rtia_rag.py` that surfaces the cache hit/miss. |

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
embedding model runs locally (no key).

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
