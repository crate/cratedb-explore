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
Minimal MCP server over the CrateDB German-weather demo schema.

Exposes one tool, `query_sql`, that runs SQL against CrateDB's HTTP `_sql`
endpoint under the `demo` schema. Register it with any MCP client (Claude
Code, Claude Desktop, ...) over stdio — see README.md.

The one rule worth encoding: "in Germany" questions must be polygon-filtered
with WITHIN(...), because demo.geo_points holds near-border foreign towns. That
rule is stated in both the tool description and the server `instructions` so the
connecting model applies it.

Connection defaults to a local cluster (crate@localhost:4200) and can be
overridden with --cratedb-url / --cratedb-host / ... flags or the matching
CRATEDB_* environment variables (flags win).
"""

import argparse
import os
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# Fallbacks so the server runs with no arguments.
DEFAULTS = {
    "host": "localhost",
    "port": "4200",
    "user": "crate",
    "password": "a_password",
    "scheme": "http",
}


def parse_args() -> argparse.Namespace:
    """CLI flags. All default to None so environment variables can layer
    underneath them in resolve_endpoint.

    Uses parse_known_args so the module can be imported by the MCP CLI
    (`mcp dev`/`mcp run`/`mcp install`), which re-passes its own argv — the
    extra tokens are ignored instead of aborting at import time."""
    p = argparse.ArgumentParser(
        description="MCP server over the CrateDB German-weather demo schema.",
    )
    p.add_argument("--cratedb-url", help="Full URL, e.g. http://user:pw@host:4200/")
    p.add_argument("--cratedb-host", help="CrateDB host.")
    p.add_argument("--cratedb-port", help="CrateDB HTTP port (default 4200).")
    p.add_argument("--cratedb-user", help="CrateDB username.")
    p.add_argument("--cratedb-password", help="CrateDB password.")
    p.add_argument("--cratedb-scheme", help="http or https.")
    return p.parse_known_args()[0]


def resolve_endpoint(args: argparse.Namespace) -> tuple[str, tuple[str, str]]:
    """Resolve the `_sql` endpoint URL and HTTP Basic auth.

    Either a full --cratedb-url / CRATEDB_CLUSTER_URL is supplied, or the
    pieces are assembled from --cratedb-host / CRATEDB_HOST and friends.
    CLI flags always win over environment variables, and anything still
    missing falls back to a local cluster so the example runs out of the box.

    Precedence is, in order: the --cratedb-url flag; any individual
    --cratedb-* flag (which forces the host-parts path so a
    CRATEDB_CLUSTER_URL in the environment can't silently override it);
    CRATEDB_CLUSTER_URL; then host parts from CRATEDB_* env vars / defaults.

    Credentials are kept separate so there is a single source of truth: when a
    URL carries no userinfo, the user/password still come from CRATEDB_USER /
    CRATEDB_PASSWORD (then the defaults), so you never embed them in the URL.
    """
    part_flags = (
        args.cratedb_host,
        args.cratedb_port,
        args.cratedb_user,
        args.cratedb_password,
        args.cratedb_scheme,
    )
    url = args.cratedb_url or (
        os.environ.get("CRATEDB_CLUSTER_URL") if not any(part_flags) else None
    )
    if url:
        u = urlparse(url)
        scheme = u.scheme or DEFAULTS["scheme"]
        host = u.hostname or DEFAULTS["host"]
        port = str(u.port or DEFAULTS["port"])
        user = u.username or os.environ.get("CRATEDB_USER") or DEFAULTS["user"]
        password = u.password or os.environ.get("CRATEDB_PASSWORD") or DEFAULTS["password"]
    else:
        scheme = args.cratedb_scheme or os.environ.get("CRATEDB_SCHEME") or DEFAULTS["scheme"]
        host = args.cratedb_host or os.environ.get("CRATEDB_HOST") or DEFAULTS["host"]
        port = args.cratedb_port or os.environ.get("CRATEDB_PORT") or DEFAULTS["port"]
        user = args.cratedb_user or os.environ.get("CRATEDB_USER") or DEFAULTS["user"]
        password = args.cratedb_password or os.environ.get("CRATEDB_PASSWORD") or DEFAULTS["password"]
    return f"{scheme}://{host}:{port}/_sql", (user, password)


ENDPOINT, AUTH = resolve_endpoint(parse_args())

INSTRUCTIONS = (
    "Tools query a CrateDB cluster of German weather and regional data in the "
    "`demo` schema: climate_data (geo_location geo_point, measurement_time, "
    "data['temperature'] in Kelvin), german_regions (16 Laender with geo_coords "
    "polygons plus full-text columns economics, transportation and "
    "introduced_species - use MATCH() on these to answer questions about a "
    "region's industry (e.g. car factories), transport or wildlife), geo_points "
    "(station locations). "
    "MANDATORY FIRST STEP: never run a data query without first confirming the "
    "actual table and column names. Before any SELECT against the data, query "
    "information_schema (e.g. SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'demo', then SELECT column_name, data_type FROM "
    "information_schema.columns WHERE table_schema = 'demo' AND table_name = "
    "'<table>') and write your query using only the table and column names that "
    "those results return. The schema summary above is guidance, not a "
    "substitute for this check. "
    "Temperatures are Kelvin - always show Celsius first, Kelvin in "
    "parentheses, e.g. -8.99 C (264.16 K). "
    "For ANY 'where in Germany' / most-extreme-place question you MUST "
    "restrict candidates with WITHIN(c.geo_location, r.geo_coords) by joining "
    "climate_data c to german_regions r; geo_points alone leaks near-border "
    "foreign towns (e.g. Tannheim in Tyrol). "
    "When a query touches geo_points and the user gives no time range, limit it "
    "to the latest data with measurement_time = (SELECT MAX(d2.measurement_time) "
    "FROM demo.climate_data d2). "
    "End every SQL statement with LIMIT 1000 unless the user instructs you "
    "otherwise."
)

mcp = FastMCP("german-weather", instructions=INSTRUCTIONS)


@mcp.tool()
def query_sql(statement: str) -> str:
    """Run a read-only SQL statement against the CrateDB `demo` schema and
    return columns + rows.

    UNDER NO CIRCUMSTANCES query the data before checking the table and column
    names first. Your first calls for any task must inspect the schema via
    information_schema (SELECT table_name FROM information_schema.tables WHERE
    table_schema = 'demo'; then SELECT column_name, data_type FROM
    information_schema.columns WHERE table_schema = 'demo' AND table_name =
    '<table>'). Only after you have confirmed the real names from those results
    may you build SELECTs against the data, and only with names that appear in
    them.

    Beyond weather readings, german_regions carries full-text economics,
    transportation and introduced_species columns - answer questions about a
    region's industry (e.g. car factories), transport or wildlife with
    MATCH(<column>, '<terms>') rather than assuming the data is weather-only.

    "In Germany" / most-extreme-place questions MUST polygon-filter candidates
    with WITHIN(c.geo_location, r.geo_coords), joining demo.climate_data c to
    demo.german_regions r - do NOT use geo_points or DISTANCE() alone as the
    country filter (geo_points contains near-border foreign towns). Temperatures
    are Kelvin: report Celsius first with Kelvin in parentheses.

    When a query touches geo_points and the user gives no time range, limit it
    to the latest data with
    measurement_time = (SELECT MAX(d2.measurement_time) FROM demo.climate_data d2).

    End every SQL statement with LIMIT 1000 unless the user instructs you
    otherwise.
    """
    # CrateDB's HTTP _sql endpoint is stateless, so the persistent equivalent
    # of `SET search_path TO demo` is the Default-Schema header on each request.
    r = httpx.post(
        ENDPOINT,
        json={"stmt": statement},
        auth=AUTH,
        headers={"Default-Schema": "demo"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    cols, rows = data.get("cols", []), data.get("rows", [])
    lines = [f"columns: {cols}", f"row count: {len(rows)}"]
    lines += [f"  {row}" for row in rows[:50]]
    if len(rows) > 50:
        lines.append(f"  ... {len(rows) - 50} more rows omitted")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
