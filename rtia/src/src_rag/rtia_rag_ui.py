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
rtia RAG — Streamlit UI
=======================
A small web front-end over rtia_rag.py. You type a maintenance question; the app

  1. checks it against `search_string` in rtia.knn_searches (a normalized,
     exact-match PRIMARY KEY lookup),
  2. reuses the cached 384-dim embedding on a hit, or generates one with
     all-MiniLM-L6-v2 and stores it back on a miss,
  3. KNN_MATCHes that vector against maintenance_log.notes_embedding, and
  4. optionally asks Claude for a grounded answer over the matched work orders.

The cache hit/miss is surfaced in the UI — that's the part this demo exists to
show. All the real logic lives in rtia_rag.py; this file is just the chrome.

Run:
    pip install -r requirements.txt
    export CRATEDB_HOST=... CRATEDB_USER=... CRATEDB_PASSWORD=... ANTHROPIC_API_KEY=...


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


st.set_page_config(page_title="rtia maintenance RAG", layout="wide")
st.title("rtia maintenance-log search")
st.caption("Embed the question (cached in rtia.knn_searches) → KNN-match maintenance notes → ask Claude.")

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
    do_generate = st.checkbox("Generate grounded answer (calls Claude)", value=True)

question = st.text_input(
    "Ask about maintenance history",
    placeholder="e.g. thermal runaway on temperature sensors",
)

if st.button("Search", type="primary") and question.strip():
    if not host:
        st.error("Set a CrateDB host (sidebar or CRATEDB_HOST).")
        st.stop()
    if do_generate and not os.getenv("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY is not set — either set it or uncheck 'Generate grounded answer'.")
        st.stop()

    model = load_model(os.getenv("RTIA_EMBED_MODEL", rag.EMBED_MODEL))
    conn = get_conn(host, port, user, password, database)

    # 1–2. cache lookup / generate-and-store
    key = rag.normalize_key(question)
    vec, hit = rag.get_query_embedding(conn, model, question)
    if hit:
        st.success(f"Cache **HIT** — reused the stored embedding for “{key}”.")
    else:
        st.info(f"Cache **MISS** — embedded “{key}” with all-MiniLM-L6-v2 and stored it in knn_searches.")
    st.caption(f"knn_searches holds {cache_size(conn)} cached query embeddings.")

    # 3. retrieve
    rows = rag.retrieve(conn, vec, top_k)
    if not rows:
        st.warning("No matches — is maintenance_log.notes_embedding populated?")
        st.stop()

    st.subheader(f"Top {len(rows)} matching work orders")
    st.dataframe(rows, use_container_width=True)

    # 4. generate
    if do_generate:
        with st.spinner("Asking Claude…"):
            answer = rag.generate(
                os.getenv("ANTHROPIC_MODEL", rag.DEFAULTS["chat_model"]),
                question,
                rag.build_context(rows),
            )
        st.subheader("Grounded answer")
        st.markdown(answer)
