# CrateDB KNN Search — Python

Interactive search CLI for CrateDB's `german_regions` table. Supports two search modes:

- **KNN (default)** — semantic search via OpenAI embeddings and CrateDB's `KNN_MATCH` on a `FLOAT_VECTOR` column.
- **Fulltext** — BM25 relevance search via CrateDB's `MATCH` predicate against fulltext-indexed text columns. No OpenAI key needed.

Connects over the PostgreSQL wire protocol using [psycopg](https://www.psycopg.org/).

## How it works

1. Reads a search query from an interactive prompt or stdin.
2. Connects to CrateDB and runs a preflight check (verifies the required columns exist and contain data).
3. Executes the search:

   **KNN mode** — sends the query text to the OpenAI embeddings API, then runs a `KNN_MATCH` query to find the closest vectors:
   ```sql
   SELECT region_name, _score
   FROM   german_regions
   WHERE  KNN_MATCH(embedding, <vector>, <top_k>)
   ORDER  BY _score DESC
   LIMIT  <top_k>
   ```

   **Fulltext mode** — runs a BM25 `MATCH` query across one or more fulltext-indexed columns:
   ```sql
   SELECT region_name, _score
   FROM   german_regions
   WHERE  match((tourism_info, transportation, economics, introduced_species), <query>)
   ORDER  BY _score DESC
   LIMIT  <top_k>
   ```

4. Prints a ranked results table to stdout.

## Prerequisites

- Python 3.10+
- Network access to your CrateDB cluster on port 5432
- The `german_regions` table in a `demo` schema with:
  - `region_name` — human-readable region identifier
  - `embedding` (`FLOAT_VECTOR`) — pre-computed OpenAI embeddings (KNN mode only)
  - `tourism_info`, `transportation`, `economics`, `introduced_species` — fulltext-indexed text columns (fulltext mode)
- An [OpenAI API key](https://platform.openai.com/api-keys) (KNN mode only)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install psycopg openai
```

## Usage

Connection parameters can be passed as CLI flags or environment variables. Precedence: CLI flag > env var > built-in default.

### KNN (semantic) search

```bash
python cratedb_knn_search.py --host <host> --user <user> --password <password>
```

### Fulltext (BM25) search

```bash
python cratedb_knn_search.py --host <host> --user <user> --password <password> --fulltext
```

### Piped input

```bash
echo "wine country" | python cratedb_knn_search.py --host <host> --fulltext
```

### All options

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--host` | `CRATEDB_HOST` | *(required)* | CrateDB hostname |
| `--port` | `CRATEDB_PORT` | `5432` | PostgreSQL wire-protocol port |
| `--user` | `CRATEDB_USER` | `crate` | Database user |
| `--password` | `CRATEDB_PASSWORD` | *(empty)* | Database password |
| `--database` | `CRATEDB_DB` | `demo` | Database / schema name |
| `--top-k` | — | `5` | Number of results to return |
| `--name-column` | — | `region_name` | Column holding the human-readable row name |
| `--fulltext` | — | off | Use BM25 fulltext search instead of KNN |
| `--fulltext-columns` | `CRATEDB_FULLTEXT_COLUMNS` | `tourism_info,transportation,economics,introduced_species` | Comma-separated fulltext-indexed columns to match against |
| `--openai-key` | `OPENAI_API_KEY` | *(required for KNN)* | OpenAI API key |
| `--model` | `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model |

## Sample output

```
[info] german_regions: 16 row(s) with embeddings
[info] embedding query: "wine country"
Region                          Score
------------------------------  -----
#1  Rheinland-Pfalz               0.7231
#2  Baden-Württemberg              0.6894
#3  Hessen                         0.6512
#4  Sachsen                        0.6201
#5  Bayern                         0.6087
```

## License

Apache License 2.0. See the [LICENSE](../../../LICENSE) file.
