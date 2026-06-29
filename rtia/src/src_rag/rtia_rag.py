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
rtia RAG demo (agentic)
=======================
A Retrieval-Augmented Generation pipeline over the `rtia` schema that lets Claude
choose how to retrieve, fusing this repo's two patterns — the src_rag KNN path and
the src_mcp_search_rtia query_sql path — into one agentic tool-use loop:

  semantic_search : embed the question with the SAME model that populated
                    notes_embedding (sentence-transformers/all-MiniLM-L6-v2,
                    384-dim), KNN_MATCH against `maintenance_log.notes_embedding`,
                    and return the descriptive columns + `notes` of the top-k work
                    orders. For "what kind of failure / find similar" questions.
  run_sql         : run a read-only SELECT over the rtia schema. For "which / how
                    many / by technician / totals" questions that need an exact set
                    or aggregate, which a KNN top-k sample cannot give.

Claude routes per question (and may use both), then answers grounded in the tool
results, citing the work orders / devices / aggregates it used. The query
embedding is still cached in rtia.knn_searches via get_query_embedding, so
semantic_search reuses stored vectors. rtia_mcp.py exposes the same query_sql
shape to an external MCP client; here it lives inside the program alongside live
KNN so a single CLI/UI handles free-form input end to end.

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
# model citable context for each work order. technician / cost_eur /
# duration_hours are included so semantic retrieval can answer "who did the
# work / how long / how much" without falling back to a SQL aggregation.
CONTENT_COLUMNS = [
    "work_order_id",
    "device_id",
    "plant_id",
    "maintenance_type",
    "technician",
    "completed_date",
    "duration_hours",
    "cost_eur",
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
    "You answer questions about an industrial-IoT maintenance operation in the "
    "CrateDB `rtia` schema. You have two tools and should choose per question:\n"
    "  - semantic_search: embed the question and KNN-match the free-text "
    "maintenance `notes`. Use it for 'what kind of failure / describe / find "
    "similar issues' questions, where meaning matters more than exact filters.\n"
    "  - run_sql: run a read-only SELECT over the `rtia` schema. Use it for "
    "'which / how many / list all / totals / group by / by technician or plant' "
    "questions — anything that needs an exact set, a count, or an aggregate, "
    "which KNN's top-k sample cannot give. You may call both (e.g. semantic_search "
    "to find the relevant failure mode, then run_sql to aggregate over it).\n\n"
    "Key tables: maintenance_log (work_order_id, device_id, plant_id, "
    "maintenance_type, technician, scheduled_date, completed_date, "
    "duration_hours, cost_eur, status, full-text `notes`, notes_embedding); "
    "devices (1 row per device: device_id, device_type, plant_id, line_id, ...); "
    "plants; iot_data (Telegraf shape: tags['device_id'], tags['status'], "
    "tags['metric_unit'], fields['metric_value'], fields['quality_score']); "
    "fault_predictions (ML output, denormalised). Before querying columns you are "
    "unsure of, check information_schema.columns for table_schema='rtia'.\n\n"
    "rtia data rules: sensor values are NOT Kelvin — each iot_data reading's unit "
    "is in tags['metric_unit'] (C, mm/s, bar, ...); report the value with its unit "
    "and never convert. With no time range, scope iot_data to each device's latest "
    "readings. Do NOT join fault_predictions to iot_data (it fans out ~1000x); "
    "join the 1-row-per-device devices table instead. End SQL with LIMIT 1000 "
    "unless asked otherwise.\n\n"
    "Ground every answer in tool results. If the data does not contain the answer, "
    "say so plainly rather than guessing. Cite the work_order_id / device_id (and "
    "any SQL aggregate) you drew on, in parentheses after the relevant claim."
)

# Tool surface handed to Claude. The model picks per question; the handlers below
# execute against the same CrateDB connection the CLI/UI already hold.
TOOLS = [
    {
        "name": "semantic_search",
        "description": (
            "KNN-match the question against maintenance_log.notes_embedding and "
            "return the top-k most semantically similar work orders (descriptive "
            "columns + notes). Use for 'what kind of failure / describe / find "
            "similar issues' questions. Returns a top-k SAMPLE, not an exhaustive "
            "or counted set — use run_sql for those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language description of the issue to match."},
                "k": {"type": "integer", "description": "How many work orders to return (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run a single READ-ONLY SQL statement (SELECT / WITH / EXPLAIN / SHOW) "
            "against the rtia schema and return columns + rows. Use for 'which / "
            "how many / list all / totals / group by' questions that need an exact "
            "set, count, or aggregate. Non-read statements are rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "A read-only SQL statement over the rtia schema."},
            },
            "required": ["statement"],
        },
    },
]

# Statements run_sql will execute — keep the model's SQL read-only since the
# connection is autocommit (unlike rtia_mcp.py, which relies on instructions).
_READ_ONLY_PREFIXES = ("select", "with", "explain", "show")


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


def tool_semantic_search(conn, embed_model, query, k):
    """semantic_search handler: cache-aware embed → KNN retrieve → context block.

    Returns (text_for_model, meta) where meta carries the cache hit flag and the
    retrieved rows so a UI can render the trace. Reuses get_query_embedding so the
    rtia.knn_searches cache (and its hit/miss) still applies.
    """
    vec, hit = get_query_embedding(conn, embed_model, query)
    rows = retrieve(conn, vec, k)
    print(f"[tool] semantic_search(k={k}) cache {'HIT' if hit else 'MISS'} — "
          f"{len(rows)} rows: {', '.join(r['work_order_id'] for r in rows) or '(none)'}",
          file=sys.stderr)
    text = build_context(rows) if rows else "(no matching work orders)"
    return text, {"cache_hit": hit, "rows": rows, "k": k}


def tool_run_sql(conn, statement):
    """run_sql handler: execute a read-only SELECT and return columns + rows.

    Rejects anything that isn't SELECT/WITH/EXPLAIN/SHOW because the connection is
    autocommit — we don't want the model mutating the demo data.
    """
    if statement.lstrip().split(None, 1)[0].lower() not in _READ_ONLY_PREFIXES:
        return "ERROR: only read-only statements (SELECT/WITH/EXPLAIN/SHOW) are allowed.", {"error": True}
    with conn.cursor() as cur:
        cur.execute(statement)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    print(f"[tool] run_sql → {len(rows)} rows :: {' '.join(statement.split())[:120]}", file=sys.stderr)
    lines = [f"columns: {cols}", f"row count: {len(rows)}"]
    lines += [f"  {list(r)}" for r in rows[:50]]
    if len(rows) > 50:
        lines.append(f"  ... {len(rows) - 50} more rows omitted")
    text = "\n".join(lines)
    return text, {"error": False, "cols": cols, "rows": rows, "statement": statement}


def generate(model, question, conn, embed_model, default_k, on_event=None):
    """Agentic answer: give Claude semantic_search + run_sql and let it route.

    Manual tool-use loop (Anthropic Python SDK): call the model, execute any
    tool_use blocks, feed the results back, and repeat until it stops calling
    tools. Adaptive thinking is on because the routing/synthesis is genuinely
    multi-step; appending the full response.content each turn preserves the
    thinking and tool_use blocks the API needs on the next request.

    on_event(kind, payload) — optional callback so a UI can render the trace
    ("tool_use" with name/input, "tool_result" with the handler meta).
    """
    import anthropic  # imported lazily so retrieval-only use needs no SDK

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the env
    messages = [{"role": "user", "content": question}]

    for _ in range(6):  # cap tool round-trips so a confused model can't loop forever
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            return "[model declined to answer this request]"
        if resp.stop_reason != "tool_use":
            return next((b.text for b in resp.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if on_event:
                on_event("tool_use", {"name": block.name, "input": block.input})
            if block.name == "semantic_search":
                text, meta = tool_semantic_search(
                    conn, embed_model, block.input["query"], block.input.get("k", default_k))
            elif block.name == "run_sql":
                text, meta = tool_run_sql(conn, block.input["statement"])
            else:
                text, meta = f"ERROR: unknown tool {block.name}", {"error": True}
            if on_event:
                on_event("tool_result", {"name": block.name, "meta": meta})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": text,
                "is_error": bool(meta.get("error")),
            })
        messages.append({"role": "user", "content": results})

    return "[stopped: reached the tool-call limit without a final answer]"


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
        print(f'[info] answering agentically (semantic_search + run_sql): "{question}"', file=sys.stderr)
        answer = generate(args.chat_model, question, conn, embed_model, args.top_k)
    finally:
        conn.close()

    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
