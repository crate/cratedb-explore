#!/usr/bin/env python3
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

"""Run every statement in rtia/sql/rtia_first_queries.sql and
rtia/sql/rtia_advanced_queries.sql against CrateDB's HTTP _sql endpoint (schema rtia)
and report pass/fail per query. Connection comes from env (source env.sh first):

    CRATEDB_HOST   (default localhost)   CRATEDB_USER
    CRATEDB_PORT   (default 4200)        CRATEDB_PASSWORD
    CRATEDB_SCHEME (default http)

Usage:  python rtia/verify_rtia_queries.py   (runs from any cwd)
"""
import base64
import json
import os
import re
import sys
import urllib.request

HOST = os.environ.get("CRATEDB_HOST", "localhost")
PORT = os.environ.get("CRATEDB_PORT", "4200")
SCHEME = os.environ.get("CRATEDB_SCHEME", "http")
USER = os.environ.get("CRATEDB_USER")
PASSWORD = os.environ.get("CRATEDB_PASSWORD", "")
URL = f"{SCHEME}://{HOST}:{PORT}/_sql"

# Resolve the SQL files relative to this script (rtia/), so it runs from any cwd.
SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")
FILES = [os.path.join(SQL_DIR, "rtia_first_queries.sql"),
         os.path.join(SQL_DIR, "rtia_advanced_queries.sql")]


def split_statements(path):
    """Drop full-line comments / blanks, then split on ';'."""
    code = []
    for line in open(path):
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        code.append(line.rstrip("\n"))
    text = "\n".join(code)
    return [st.strip() for st in text.split(";") if st.strip()]


def run(stmt):
    body = json.dumps({"stmt": stmt}).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Default-Schema", "rtia")
    if USER:
        token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main():
    print(f"Target: {URL}  (schema=rtia, user={USER or '(none)'})\n")
    total = passed = 0
    failures = []
    for path in FILES:
        stmts = split_statements(path)
        print(f"=== {path}  ({len(stmts)} statements) ===")
        for i, stmt in enumerate(stmts, 1):
            first = " ".join(stmt.split())[:70]
            total += 1
            try:
                res = run(stmt)
                rows = res.get("rowcount", len(res.get("rows", [])))
                cols = len(res.get("cols", []))
                print(f"  [{i:2}] PASS  rows={rows:<6} cols={cols:<3} | {first}")
                passed += 1
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                try:
                    detail = json.loads(detail)["error"]["message"]
                except Exception:
                    pass
                print(f"  [{i:2}] FAIL  {first}\n         -> {detail}")
                failures.append((path, i, first, detail))
            except Exception as e:  # noqa: BLE001
                print(f"  [{i:2}] FAIL  {first}\n         -> {e}")
                failures.append((path, i, first, str(e)))
        print()
    print(f"RESULT: {passed}/{total} statements ran OK")
    if failures:
        print("\nFailures:")
        for path, i, first, detail in failures:
            print(f"  - {path} #{i}: {first}\n      {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
