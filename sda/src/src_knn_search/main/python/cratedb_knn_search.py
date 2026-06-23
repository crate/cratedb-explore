#
# Licensed to Crate.io GmbH ("Crate") under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  Crate licenses
# this file to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.  You may
# obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
# However, if you have executed another commercial license agreement
# with Crate these terms will supersede the license and you may use the
# software solely pursuant to the terms of the relevant commercial agreement.

"""
CrateDB Search CLI
==================
Interactive search against the `german_regions` table. Two modes:

  KNN (default)  : semantic search via OpenAI embeddings + KNN_MATCH on a
                   FLOAT_VECTOR column. Requires an OpenAI API key.
  Fulltext       : BM25 match() against fulltext-indexed text columns.
                   No OpenAI key needed. Enabled with --fulltext.

Connection parameters can come from CLI flags or environment variables.
Precedence: CLI flag > env var > built-in default.

Usage:
    # Semantic KNN
    python cratedb_knn_search.py --host 10.13.1.19 --user scott --password tiger

    # Fulltext BM25 (no OpenAI)
    python cratedb_knn_search.py --host 10.13.1.19 --user scott --password tiger --fulltext

    echo "wine country" | python cratedb_knn_search.py --host ... --fulltext

Output:
    Result table on stdout. All status messages on stderr.
"""

import argparse
import os
import sys

import psycopg


TABLE = "german_regions"

DEFAULTS = {
    "port": 5432,
    "user": "crate",
    "password": "",
    "database": "demo",
    "top_k": 5,
    "model": "text-embedding-3-small",
    "fulltext_columns": "tourism_info,transportation,economics,introduced_species",
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Interactive search against a CrateDB table (KNN or fulltext).",
    )
    p.add_argument("--host", default=os.getenv("CRATEDB_HOST"),
                   help="CrateDB host (env: CRATEDB_HOST)")
    p.add_argument("--port", type=int,
                   default=int(os.getenv("CRATEDB_PORT", DEFAULTS["port"])),
                   help=f"CrateDB PostgreSQL port (env: CRATEDB_PORT, default {DEFAULTS['port']})")
    p.add_argument("--user", default=os.getenv("CRATEDB_USER", DEFAULTS["user"]),
                   help=f"CrateDB user (env: CRATEDB_USER, default {DEFAULTS['user']})")
    p.add_argument("--password", default=os.getenv("CRATEDB_PASSWORD", DEFAULTS["password"]),
                   help="CrateDB password (env: CRATEDB_PASSWORD)")
    p.add_argument("--database", default=os.getenv("CRATEDB_DB", DEFAULTS["database"]),
                   help=f"CrateDB database (env: CRATEDB_DB, default {DEFAULTS['database']})")
    p.add_argument("--top-k", type=int, default=DEFAULTS["top_k"],
                   help=f"Number of results to return (default {DEFAULTS['top_k']})")
    p.add_argument("--name-column", default="region_name",
                   help="Column holding the human-readable row name (default region_name)")
    p.add_argument("--fulltext", action="store_true",
                   help="Use BM25 fulltext match() instead of KNN. No OpenAI key needed.")
    p.add_argument("--fulltext-columns",
                   default=os.getenv("CRATEDB_FULLTEXT_COLUMNS", DEFAULTS["fulltext_columns"]),
                   help=("Comma-separated fulltext-indexed columns to match against "
                         "(env: CRATEDB_FULLTEXT_COLUMNS, default "
                         f"{DEFAULTS['fulltext_columns']})"))
    p.add_argument("--openai-key", default=os.getenv("OPENAI_API_KEY"),
                   help="OpenAI API key (env: OPENAI_API_KEY). Required unless --fulltext.")
    p.add_argument("--model", default=os.getenv("OPENAI_EMBED_MODEL", DEFAULTS["model"]),
                   help=f"OpenAI embedding model (env: OPENAI_EMBED_MODEL, default {DEFAULTS['model']})")

    args = p.parse_args()

    missing = []
    if not args.host:
        missing.append("--host / CRATEDB_HOST")
    if not args.fulltext and not args.openai_key:
        missing.append("--openai-key / OPENAI_API_KEY (or use --fulltext)")
    if missing:
        print("Error: missing required parameter(s): " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)

    if not _is_safe_ident(args.name_column):
        print(f"Error: invalid column name {args.name_column!r}", file=sys.stderr)
        sys.exit(2)

    args.fulltext_column_list = [c.strip() for c in args.fulltext_columns.split(",") if c.strip()]
    if args.fulltext and not args.fulltext_column_list:
        print("Error: --fulltext-columns is empty", file=sys.stderr)
        sys.exit(2)
    for col in args.fulltext_column_list:
        if not _is_safe_ident(col):
            print(f"Error: invalid fulltext column name {col!r}", file=sys.stderr)
            sys.exit(2)

    return args


def _is_safe_ident(s: str) -> bool:
    if not s:
        return False
    return all(c.isalnum() or c == "_" for c in s)


def connect(args):
    return psycopg.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        dbname=args.database,
        autocommit=True,
    )


def get_embedding(client, text: str, model: str) -> list:
    text = text.replace("\n", " ").strip()
    response = client.embeddings.create(input=text, model=model)
    return response.data[0].embedding


def preflight_knn(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM   information_schema.columns
            WHERE  table_name  = 'german_regions'
            AND    column_name = 'embedding'
        """)
        if cur.fetchone()[0] == 0:
            print("Error: table 'german_regions' has no 'embedding' column", file=sys.stderr)
            sys.exit(1)

        cur.execute("SELECT COUNT(*) FROM german_regions WHERE embedding IS NOT NULL")
        n = cur.fetchone()[0]
        if n == 0:
            print("Error: table 'german_regions' has no rows with a populated embedding", file=sys.stderr)
            sys.exit(1)
        print(f"[info] german_regions: {n} row(s) with embeddings", file=sys.stderr)


def preflight_fulltext(conn, columns: list) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_name = 'german_regions'
        """)
        present = {r[0] for r in cur.fetchall()}
    missing = [c for c in columns if c not in present]
    if missing:
        print(f"Error: table 'german_regions' is missing fulltext column(s): {missing}", file=sys.stderr)
        sys.exit(1)
    print(f"[info] fulltext match across: {', '.join(columns)}", file=sys.stderr)


def knn_search(conn, client, args, query: str):
    print(f'[info] embedding query: "{query}"', file=sys.stderr)
    vec = get_embedding(client, query, args.model)

    sql = (
        f"SELECT {args.name_column}, _score "
        f"FROM   {TABLE} "
        f"WHERE  KNN_MATCH(embedding, %s, %s) "
        f"ORDER  BY _score DESC "
        f"LIMIT  %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (vec, args.top_k, args.top_k))
        rows = cur.fetchall()

    _print_results(rows)


def fulltext_search(conn, args, query: str):
    print(f'[info] fulltext query: "{query}"', file=sys.stderr)
    columns_sql = ", ".join(args.fulltext_column_list)

    sql = (
        f"SELECT {args.name_column}, _score "
        f"FROM   {TABLE} "
        f"WHERE  match(({columns_sql}), %s) "
        f"ORDER  BY _score DESC "
        f"LIMIT  %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (query, args.top_k))
        rows = cur.fetchall()

    _print_results(rows)


def _print_results(rows):
    print(f"{'Region':<30}  Score")
    print(f"{'-' * 30}  -----")
    for rank, (name, score) in enumerate(rows, 1):
        print(f"#{rank}  {str(name):<28}  {score:.4f}")


def read_query() -> str:
    try:
        if sys.stdin.isatty():
            return input("Search: ").strip()
        return sys.stdin.readline().strip()
    except EOFError:
        return ""


def main():
    args = parse_args()

    query = read_query()
    if not query:
        print("[info] empty query, exiting", file=sys.stderr)
        return 0

    conn = connect(args)
    try:
        if args.fulltext:
            preflight_fulltext(conn, args.fulltext_column_list)
            fulltext_search(conn, args, query)
        else:
            from openai import OpenAI
            client = OpenAI(api_key=args.openai_key)
            preflight_knn(conn)
            knn_search(conn, client, args, query)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
