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
rtia RAG demo
=============
A minimal Retrieval-Augmented Generation pipeline over `rtia.maintenance_log`,
wiring the rtia tree's two retrieval halves into one flow:

  Retrieve : embed the question with the SAME model that populated
             notes_embedding (sentence-transformers/all-MiniLM-L6-v2, 384-dim),
             KNN_MATCH against `maintenance_log.notes_embedding`, and pull the
             descriptive columns + the free-text `notes` of the top-k work
             orders.
  Generate : hand those work orders to Claude as grounding context and ask it
             to answer the question, citing the work orders / devices it used.

This is the live-embedding sibling of the rtia_mcp.py KNN path. rtia_mcp.py
sidesteps live embedding by looking a query vector up in rtia.knn_searches by
search_string (works only for canned queries); here we embed arbitrary
questions locally so the demo handles free-form input. See knn_searches for the
zero-dependency canned-query alternative.

Auth:
    CrateDB  : CRATEDB_USER / CRATEDB_PASSWORD (+ --host etc.).
    Anthropic: ANTHROPIC_API_KEY (generation) — read from the env by the SDK,
               never passed on the CLI.
    Embedding model runs locally — no API key.

Usage:
    python rtia_rag.py --host 10.13.1.19
    echo "What thermal failures have we seen on temperature sensors?" | python rtia_rag.py --host ...

Output:
    The grounded answer on stdout; retrieval/diagnostic messages on stderr.
"""

import argparse
import os
import sys

import psycopg

# rtia rules (CLAUDE.md): values carry their own unit in tags['metric_unit'] and
# are NOT Kelvin — never convert. That applies to iot_data, not maintenance
# notes, but the system prompt keeps the model from inventing conversions.
TABLE = "rtia.maintenance_log"

# Descriptive columns we retrieve alongside the free-text `notes` to give the
# model citable context for each work order.
CONTENT_COLUMNS = [
    "work_order_id",
    "device_id",
    "plant_id",
    "maintenance_type",
    "completed_date",
    "status",
    "notes",
]

# The model that produced notes_embedding — query vectors MUST come from the
# same model (and the same normalization) or KNN_MATCH scores are meaningless.
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

DEFAULTS = {
    "port": 5432,
    "user": "crate",
    "password": "",
    "database": "rtia",  # CrateDB maps the pg database name to the default schema
    "top_k": 5,
    "chat_model": "claude-opus-4-8",
}

SYSTEM_PROMPT = (
    "You answer questions about industrial maintenance using ONLY the work-order "
    "rows provided in the user message. Each row is one maintenance work order. "
    "If the context does not contain the answer, say so plainly rather than "
    "guessing. Cite the work_order_id (and device_id) you drew on, in "
    "parentheses, after the relevant claim. Report any sensor values with the "
    "units given; never convert them."
)


def parse_args():
    p = argparse.ArgumentParser(description="RAG over rtia.maintenance_log (KNN retrieve + Claude generate).")
    p.add_argument("--host", default=os.getenv("CRATEDB_HOST"),
                   help="CrateDB host (env: CRATEDB_HOST)")
    p.add_argument("--port", type=int, default=int(os.getenv("CRATEDB_PORT", DEFAULTS["port"])),
                   help=f"CrateDB PostgreSQL port (env: CRATEDB_PORT, default {DEFAULTS['port']})")
    p.add_argument("--user", default=os.getenv("CRATEDB_USER", DEFAULTS["user"]),
                   help=f"CrateDB user (env: CRATEDB_USER, default {DEFAULTS['user']})")
    p.add_argument("--password", default=os.getenv("CRATEDB_PASSWORD", DEFAULTS["password"]),
                   help="CrateDB password (env: CRATEDB_PASSWORD)")
    p.add_argument("--database", default=os.getenv("CRATEDB_DB", DEFAULTS["database"]),
                   help=f"CrateDB database / default schema (env: CRATEDB_DB, default {DEFAULTS['database']})")
    p.add_argument("--top-k", type=int, default=DEFAULTS["top_k"],
                   help=f"Work orders to retrieve as context (default {DEFAULTS['top_k']})")
    p.add_argument("--embed-model", default=os.getenv("RTIA_EMBED_MODEL", EMBED_MODEL),
                   help=f"sentence-transformers model (default {EMBED_MODEL}) — must match notes_embedding")
    p.add_argument("--chat-model", default=os.getenv("ANTHROPIC_MODEL", DEFAULTS["chat_model"]),
                   help=f"Anthropic model for generation (default {DEFAULTS['chat_model']})")

    args = p.parse_args()

    missing = []
    if not args.host:
        missing.append("--host / CRATEDB_HOST")
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY (env)")
    if missing:
        print("Error: missing required parameter(s): " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)
    return args


def connect(args):
    return psycopg.connect(host=args.host, port=args.port, user=args.user,
                           password=args.password, dbname=args.database, autocommit=True)


def embed(model, text):
    """Embed the question to a 384-dim vector with all-MiniLM-L6-v2.

    normalize_embeddings=True matches the stored corpus: rtia_schema_create.sql
    notes that notes_embedding / knn_searches were precomputed by
    generate_embeddings.py "(normalized)". Encode the same way or KNN_MATCH
    distances won't line up.
    """
    return model.encode(text.replace("\n", " ").strip(), normalize_embeddings=True).tolist()


def normalize_key(text):
    """Canonical cache key: lowercased, whitespace-collapsed. Lookup and store
    MUST use the same normalization, or the exact-match PRIMARY KEY rarely hits.
    """
    return " ".join(text.lower().split())


def get_query_embedding(conn, model, text):
    """Read-through / write-back cache over rtia.knn_searches.

    Returns (vector, cache_hit). On a hit it's a PRIMARY KEY lookup on
    search_string; on a miss we embed and upsert so the next identical query
    hits. query_name is left NULL for UI-generated entries (the curated rows
    keep theirs). The upsert is idempotent — concurrent first-time misses on the
    same string both compute the same vector and the ON CONFLICT dedupes.
    """
    key = normalize_key(text)
    with conn.cursor() as cur:
        cur.execute("SELECT embedding FROM rtia.knn_searches WHERE search_string = %s", (key,))
        row = cur.fetchone()
        if row:
            return row[0], True
        vec = embed(model, key)
        cur.execute(
            "INSERT INTO rtia.knn_searches (query_name, search_string, embedding) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (search_string) DO UPDATE SET embedding = excluded.embedding",
            (None, key, vec),
        )
        return vec, False


def retrieve(conn, vec, top_k):
    """KNN_MATCH for the nearest work orders, returning descriptive cols + notes."""
    cols = ", ".join(CONTENT_COLUMNS)
    sql = (
        f"SELECT {cols}, _score "
        f"FROM   {TABLE} "
        f"WHERE  KNN_MATCH(notes_embedding, %s, %s) "
        f"ORDER  BY _score DESC "
        f"LIMIT  %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (vec, top_k, top_k))
        rows = cur.fetchall()
    names = [*CONTENT_COLUMNS, "_score"]
    return [dict(zip(names, r)) for r in rows]


def build_context(rows):
    """Render retrieved work orders as a plain-text context block for the prompt."""
    blocks = []
    for r in rows:
        header = (f"## {r['work_order_id']}  device={r['device_id']} "
                  f"plant={r['plant_id']} type={r['maintenance_type']} "
                  f"status={r['status']} completed={r['completed_date']} "
                  f"(score {r['_score']:.4f})")
        notes = r.get("notes") or "(no notes)"
        blocks.append(f"{header}\nnotes: {notes}")
    return "\n\n".join(blocks)


def generate(model, question, context):
    """Ask Claude to answer the question grounded in the retrieved work orders."""
    import anthropic  # imported lazily so retrieval-only use needs no SDK

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the env
    user_message = (
        f"Maintenance work orders:\n\n{context}\n\n"
        f"---\nQuestion: {question}\n\n"
        "Answer using only the work orders above, citing the work_order_id "
        "(and device_id) you used."
    )
    # A single grounded Q&A call. max_tokens is modest because answers are a few
    # short paragraphs; raise it (and switch to client.messages.stream) if you
    # expand the context or want long write-ups. Add thinking={"type":"adaptive"}
    # for harder cross-work-order synthesis.
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    if resp.stop_reason == "refusal":
        return "[model declined to answer this request]"
    return next((b.text for b in resp.content if b.type == "text"), "")


def read_question():
    try:
        if sys.stdin.isatty():
            return input("Ask: ").strip()
        return sys.stdin.readline().strip()
    except EOFError:
        return ""


def main():
    args = parse_args()
    question = read_question()
    if not question:
        print("[info] empty question, exiting", file=sys.stderr)
        return 0

    # Load the embedding model once (first call downloads it to the HF cache).
    from sentence_transformers import SentenceTransformer
    print(f"[info] loading embedding model {args.embed_model}", file=sys.stderr)
    embed_model = SentenceTransformer(args.embed_model)

    conn = connect(args)
    try:
        print(f'[info] embedding + retrieving top {args.top_k} for: "{question}"', file=sys.stderr)
        vec, hit = get_query_embedding(conn, embed_model, question)
        print(f"[info] cache {'HIT' if hit else 'MISS — embedded + stored'}", file=sys.stderr)
        rows = retrieve(conn, vec, args.top_k)
    finally:
        conn.close()

    if not rows:
        print("[info] no rows retrieved — is maintenance_log.notes_embedding populated?", file=sys.stderr)
        return 1

    print(f"[info] retrieved: {', '.join(r['work_order_id'] for r in rows)}", file=sys.stderr)
    answer = generate(args.chat_model, question, build_context(rows))
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
