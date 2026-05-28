"""
Expose each panel in `grafana/german_weather_data.json` as an MCP tool.

----------------------------------------------------------------------
How we get SQL out of the Grafana dashboard file
----------------------------------------------------------------------

A Grafana dashboard is just a JSON document. The top level has a
"panels" array; each panel is a tile on the dashboard (graph, table,
gauge, geomap, ...). For SQL data sources, each panel carries one or
more "targets" — query definitions. The actual SQL text lives at:

    panels[i].targets[j].rawSql

Some entries are not real panels but "rows" (collapsible section
headers): `panel["type"] == "row"`. Those have no targets and are
skipped.

The raw SQL contains Grafana template variables that must be
substituted before the query can be sent to the database:

  $var or ${var}          a user-supplied dashboard variable, e.g.
                          $region, $keyword, $latitude
  $__timeFilter(column)   Grafana macro that expands at render time
                          to a range predicate against `column`
                          based on the dashboard's time picker

This module's job is to:

  1. Read the dashboard JSON once.
  2. For every panel with a rawSql target, build an in-process MCP
     tool. The tool's name is derived from the panel title and its
     parameters are derived from the template variables found in the
     SQL.
  3. When Claude calls the tool, substitute the user-supplied
     arguments into the SQL template and POST it to the CrateDB
     HTTP `_sql` endpoint. Return the resulting rows as text.

Why an in-process MCP server?

  The claude-agent-sdk expects tools to live behind an MCP server.
  The SDK supports two kinds: external stdio subprocesses (used here
  for the official `cratedb-mcp` server) and in-process "SDK MCP"
  servers built from @tool-decorated async functions. The latter is
  what we use here — no subprocess, no IPC, just Python function
  calls. See `create_sdk_mcp_server`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

# Resolve dashboard path relative to this file so the script works
# regardless of the caller's working directory.
#   parents[3] = src_mcp_search/main/python -> src_mcp_search/main
#                 -> src_mcp_search -> repo root
DASHBOARD_PATH = (
    Path(__file__).resolve().parents[3] / "grafana" / "german_weather_data.json"
)

# Each Grafana template variable we know about maps to:
#   (MCP tool parameter name, python type, human-readable description)
#
# The python type is what we declare to the MCP schema, which is then
# advertised to Claude. The description is what Claude sees when it
# decides whether and how to call the tool — make it precise; vague
# descriptions lead to incorrect arguments. (See the 'region' entry
# below — its emphatic wording about 'ALL' was added after Claude
# kept guessing 'Germany'.)
_VAR_SPEC: dict[str, tuple[str, type, str]] = {
    "region": (
        "region",
        str,
        "Federal-state name (one of the 16 German Länder), or the literal "
        "string 'ALL' for the whole country. There is no row named 'Germany'.",
    ),
    "keyword": ("keyword", str, "Full-text search term"),
    "search_categories": (
        "search_categories",
        str,
        "A single fulltext-indexed column on demo.german_regions to "
        "MATCH against. Valid values: 'region_name', 'economics', "
        "'transportation', 'introduced_species'. Pass exactly one "
        "column name — comma-separated lists produce a SQL syntax "
        "error, and 'tourism' is not fulltext-indexed.",
    ),
    "latitude": ("latitude", float, "Latitude of the location"),
    "longitude": ("longitude", float, "Longitude of the location"),
}


def _slugify(title: str) -> str:
    """
    Turn a panel title into a valid MCP tool name.

    Steps:
      1. Strip the `$` and any `{}` from template variable references
         so '$region' becomes 'region', keeping the name meaningful.
      2. Replace every run of non-alphanumeric characters with a
         single underscore.
      3. Trim leading/trailing underscores and lowercase.

    Examples:
      "Min, Average and Max temperature for $region"
        -> "min_average_and_max_temperature_for_region"
      "Daily average wind speed for ${town_name}, ${latitude}, ${longitude}"
        -> "daily_average_wind_speed_for_town_name_latitude_longitude"
    """
    s = re.sub(r"\$\{?(\w+)\}?", r"\1", title)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "panel"


def _scan_sql(sql: str) -> tuple[set[str], bool]:
    """
    Inspect a SQL template and report which substitutions it needs.

    Returns:
      vars_used:  Set of bare variable names that appear as $var or
                  ${var}. Excludes Grafana built-ins prefixed `__`
                  (e.g. $__timeFilter, $__interval) which we handle
                  separately.
      uses_time:  True if the SQL contains a $__timeFilter(col) macro.
                  When true, the generated MCP tool also exposes
                  time_from / time_to parameters.

    Used by `_make_tool` to build the input schema that Claude sees.
    """
    vars_used: set[str] = set()
    for m in re.finditer(r"\$\{?(\w+)\}?", sql):
        name = m.group(1)
        if name != "__timeFilter" and not name.startswith("__"):
            vars_used.add(name)
    uses_time = "$__timeFilter" in sql
    return vars_used, uses_time


def _render_sql(sql: str, args: dict[str, Any]) -> str:
    """
    Substitute the tool-call arguments into the Grafana SQL template.

    Two passes:

    Pass 1 — `$__timeFilter("measurement_time")`
        Grafana would normally rewrite this against the dashboard's
        time picker. We don't have a UI, so we expect the caller to
        pass `time_from` and `time_to` (ISO strings) and we expand
        the macro to:
            (col >= 'time_from' AND col <= 'time_to')
        The regex captures everything between the parens so a
        quoted column ("measurement_time") is preserved.

    Pass 2 — every `$var` / `${var}` in _VAR_SPEC
        Plain text substitution. The caller is responsible for
        passing a value of the right type; we just str() it.

    Note on SQL injection: rendered SQL is sent to a CrateDB cluster
    the user controls and authenticates against. Values originate
    from Claude tool calls, not arbitrary end users. If you ever
    expose this to untrusted input, switch to parameterised queries
    using CrateDB's `args` parameter instead of inline substitution.
    """

    def time_repl(m: re.Match) -> str:
        col = m.group(1).strip()  # may include quotes, e.g. '"measurement_time"'
        tf = args.get("time_from")
        tt = args.get("time_to")
        if not tf or not tt:
            raise ValueError(
                "time_from and time_to are required for panels that use $__timeFilter"
            )
        return f"({col} >= '{tf}' AND {col} <= '{tt}')"

    sql = re.sub(r"\$__timeFilter\(\s*([^)]+?)\s*\)", time_repl, sql)

    # Replace ordinary template variables. We iterate _VAR_SPEC rather
    # than args so a stray/unknown arg can't accidentally rewrite the
    # SQL, and so the substitution is order-independent.
    for var, (param, _, _) in _VAR_SPEC.items():
        if param in args and args[param] is not None:
            sql = re.sub(r"\$\{?" + re.escape(var) + r"\}?", str(args[param]), sql)
    return sql


def _run_sql(cratedb_url: str, sql: str) -> dict[str, Any]:
    """
    Execute the rendered SQL against CrateDB's HTTP `_sql` endpoint.

    CrateDB exposes a simple JSON-over-HTTP query API: POST a body of
    `{"stmt": "..."}` to /_sql and get back JSON with `cols` (column
    names) and `rows` (list of value lists).

    The cratedb_url passed in here may carry inline credentials, e.g.
        http://scott:tiger@host:4200/
    `urlparse` extracts user/password into separate fields, leaving
    us to rebuild a credential-free URL and pass auth via the
    standard HTTP Basic mechanism. httpx accepts that as a (user,
    password) tuple.
    """
    parsed = urlparse(cratedb_url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    endpoint = f"{parsed.scheme}://{netloc}/_sql"
    auth = (parsed.username, parsed.password or "") if parsed.username else None
    r = httpx.post(endpoint, json={"stmt": sql}, auth=auth, timeout=60)
    r.raise_for_status()
    return r.json()


def _format_result(rendered: str, result: dict[str, Any]) -> str:
    """
    Render the CrateDB response into a single text block.

    The MCP tool protocol expects each tool result to be a list of
    content blocks. We emit a single text block containing the SQL
    that ran (so Claude can quote it back to the user) followed by
    column metadata and up to 50 rows. Larger result sets are
    truncated with a count of omitted rows.
    """
    cols = result.get("cols", [])
    rows = result.get("rows", [])
    lines = [f"SQL:\n{rendered}", "", f"Columns: {cols}", f"Row count: {len(rows)}"]
    for row in rows[:50]:
        lines.append(f"  {row}")
    if len(rows) > 50:
        lines.append(f"  ... ({len(rows) - 50} more rows omitted)")
    return "\n".join(lines)


def _make_tool(panel: dict, cratedb_url: str, used_names: set[str]) -> SdkMcpTool | None:
    """
    Build one MCP tool from one Grafana panel.

    Returns None if the panel has no rawSql target (e.g. a 'row'
    container or a panel using a different data-source kind).

    The flow per panel:

      1. Pull the rawSql out of the first target that has one.
         Most panels have exactly one target; the `Temperature`
         geomap (id=1) has two but only one carries rawSql.

      2. Slug the panel title into a tool name and deduplicate
         against names we've already minted. Collisions get a
         `_<panel_id>` suffix.

      3. Scan the SQL for template variables and decide the tool's
         input schema. Each variable in _VAR_SPEC becomes one
         parameter; $__timeFilter adds time_from / time_to.

      4. Define an async function that, when called by Claude:
            - renders the SQL with the supplied arguments
            - POSTs it to CrateDB
            - formats and returns the rows
         and wrap it with the @tool decorator. The decorator
         registers the function with the SDK and returns an
         SdkMcpTool instance.

    The async function closes over `sql_template`, `cratedb_url` and
    `title`, so each panel gets its own bound implementation.
    """
    targets = [t for t in (panel.get("targets") or []) if t.get("rawSql")]
    if not targets:
        return None
    sql_template = targets[0]["rawSql"]
    title = panel.get("title", f"panel_{panel.get('id')}")

    # Mint a unique tool name. The dashboard has two 'Temperature'
    # panels — one gauge, one geomap — so we need the dedupe fallback.
    name = _slugify(title)
    if name in used_names:
        name = f"{name}_{panel.get('id')}"
    used_names.add(name)

    # Discover which Grafana variables this panel uses and translate
    # them into a JSON-schema-ish dict that the @tool decorator
    # accepts. The SDK turns this into the input_schema field that
    # the API sends to Claude alongside the tool name + description.
    vars_used, uses_time = _scan_sql(sql_template)
    schema: dict[str, type] = {}
    for v in vars_used:
        if v in _VAR_SPEC:
            param, ty, _ = _VAR_SPEC[v]
            schema[param] = ty
    if uses_time:
        schema["time_from"] = str
        schema["time_to"] = str

    description = (
        f"Grafana panel '{title}'. Runs the panel's SQL against CrateDB and returns the rows."
    )

    @tool(name, description, schema)
    async def _impl(args: dict[str, Any]) -> dict[str, Any]:
        # Errors are returned as content blocks with is_error=True
        # rather than raised, so the agent loop sees the failure as
        # tool output it can reason about — not a crash.
        try:
            rendered = _render_sql(sql_template, args)
            result = _run_sql(cratedb_url, rendered)
            return {"content": [{"type": "text", "text": _format_result(rendered, result)}]}
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error running panel '{title}': {exc}"}],
                "is_error": True,
            }

    return _impl


def build_panel_server(cratedb_url: str, dashboard_path: Path = DASHBOARD_PATH):
    """
    Load the dashboard JSON and assemble an SDK MCP server out of it.

    Called once at process startup by `german_weather.py`. The
    returned `server` value is dropped straight into
    `ClaudeAgentOptions.mcp_servers`; the agent SDK then advertises
    every tool we registered to Claude on each API request.

    Returns:
      server: opaque config object accepted by ClaudeAgentOptions.
      names:  list of tool names registered, in dashboard order.
              Used by german_weather.py only for the startup log.
    """
    data = json.loads(dashboard_path.read_text())
    used: set[str] = set()
    tools: list[SdkMcpTool] = []
    for panel in data.get("panels", []):
        # 'row' panels are section headers — they group other panels
        # in the UI but carry no queries of their own.
        if panel.get("type") == "row":
            continue
        t = _make_tool(panel, cratedb_url, used)
        if t is not None:
            tools.append(t)
    server = create_sdk_mcp_server("german_weather_panels", tools=tools)
    return server, [t.name for t in tools]
