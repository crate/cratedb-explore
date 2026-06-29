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
rtia RAG — Streamlit UI (agentic)
=================================
A small web front-end over rtia_rag.py. You type a maintenance question; Claude
picks between two tools per question and the app renders the trace:

  - semantic_search → embed the question (cached in rtia.knn_searches: normalized
    exact-match PRIMARY KEY lookup, reuse on hit / embed + store on miss),
    KNN_MATCH maintenance_log.notes_embedding, return the top-k work orders.
  - run_sql → a read-only SELECT over the rtia schema, for exact sets / counts /
    aggregates that a KNN top-k sample can't give.

The cache hit/miss and each tool call are surfaced in the UI — that's the part
this demo exists to show. All the real logic lives in rtia_rag.py; this file is
just the chrome. Unchecking "agentic" falls back to a single semantic_search
(no Claude, no key needed) so you can still inspect retrieval + the cache.

Run:
    pip install -r requirements.txt
    export CRATEDB_HOST=... CRATEDB_USER=... CRATEDB_PASSWORD=... ANTHROPIC_API_KEY=...
    streamlit run rtia_rag_ui.py

Connection + Anthropic creds come from the same env vars as rtia_rag.py.
"""

import os

import psycopg
import streamlit as st

import rtia_rag as rag  # same directory; Streamlit puts the script dir on sys.path


@st.cache_resource(show_spinner="Loading embedding model…")
def load_model(name):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


@st.cache_resource(show_spinner="Connecting to CrateDB…")
def get_conn(host, port, user, password, database):
    # Cached across reruns. If it goes stale, use the sidebar "Reconnect" button.
    return psycopg.connect(host=host, port=port, user=user, password=password,
                           dbname=database, autocommit=True)


def cache_size(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM rtia.knn_searches")
        return cur.fetchone()[0]


def render_tool_event(kind, payload):
    """on_event callback for rag.generate — render each tool call/result inline.

    Streamlit runs top-to-bottom, so writing here surfaces the trace in order as
    the agentic loop executes. semantic_search calls also flag the cache hit/miss
    (that's the demo's point) and show the matched work orders.
    """
    if kind == "tool_use":
        if payload["name"] == "semantic_search":
            st.markdown(f"🔎 **semantic_search** — query: “{payload['input'].get('query', '')}”")
        else:
            st.markdown("🗃️ **run_sql**")
            st.code(payload["input"].get("statement", ""), language="sql")
        return

    meta = payload["meta"]
    if payload["name"] == "semantic_search":
        st.caption("Cache **HIT** — reused the stored embedding." if meta.get("cache_hit")
                   else "Cache **MISS** — embedded with all-MiniLM-L6-v2 and stored it in knn_searches.")
        if meta.get("rows"):
            st.dataframe(meta["rows"], use_container_width=True)
    else:  # run_sql
        if meta.get("error"):
            st.warning("run_sql rejected (read-only statements only).")
        elif meta.get("rows"):
            st.dataframe([dict(zip(meta["cols"], r)) for r in meta["rows"]], use_container_width=True)
        else:
            st.caption("run_sql returned no rows.")


st.set_page_config(page_title="rtia maintenance RAG", layout="wide")
st.title("rtia maintenance-log search")
st.caption("Claude picks per question: semantic_search (KNN over notes, cached in rtia.knn_searches) or run_sql (read-only SQL).")

with st.sidebar:
    st.header("Connection")
    host = st.text_input("CrateDB host", os.getenv("CRATEDB_HOST", ""))
    port = int(os.getenv("CRATEDB_PORT", "5432"))
    user = os.getenv("CRATEDB_USER", "crate")
    password = os.getenv("CRATEDB_PASSWORD", "")
    database = os.getenv("CRATEDB_DB", "rtia")
    st.caption("User/password and ANTHROPIC_API_KEY are read from env vars.")
    if st.button("Reconnect / clear caches"):
        st.cache_resource.clear()
        st.rerun()

    st.header("Options")
    top_k = st.slider("Results (k)", 1, 20, 5)
    agentic = st.checkbox("Agentic (let Claude pick tools — calls Claude)", value=True)

question = st.text_input(
    "Ask about maintenance history",
    placeholder="e.g. which technicians worked on sensor problems?",
)

if st.button("Search", type="primary") and question.strip():
    if not host:
        st.error("Set a CrateDB host (sidebar or CRATEDB_HOST).")
        st.stop()
    if agentic and not os.getenv("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY is not set — either set it or uncheck 'Agentic'.")
        st.stop()

    model = load_model(os.getenv("RTIA_EMBED_MODEL", rag.EMBED_MODEL))
    conn = get_conn(host, port, user, password, database)

    if agentic:
        # Let Claude route between semantic_search and run_sql; render the trace
        # via the on_event callback as the loop runs, then the grounded answer.
        st.subheader("Retrieval & query trace")
        with st.spinner("Asking Claude…"):
            answer = rag.generate(
                os.getenv("ANTHROPIC_MODEL", rag.DEFAULTS["chat_model"]),
                question,
                conn,
                model,
                top_k,
                on_event=render_tool_event,
            )
        st.caption(f"knn_searches holds {cache_size(conn)} cached query embeddings.")
        st.subheader("Grounded answer")
        st.markdown(answer)
    else:
        # Retrieval-only: a single semantic_search, no Claude (no key needed).
        text, meta = rag.tool_semantic_search(conn, model, question, top_k)
        st.success("Cache **HIT** — reused the stored embedding." if meta["cache_hit"]
                   else "Cache **MISS** — embedded and stored it in knn_searches.")
        st.caption(f"knn_searches holds {cache_size(conn)} cached query embeddings.")
        if not meta["rows"]:
            st.warning("No matches — is maintenance_log.notes_embedding populated?")
            st.stop()
        st.subheader(f"Top {len(meta['rows'])} matching work orders")
        st.dataframe(meta["rows"], use_container_width=True)
