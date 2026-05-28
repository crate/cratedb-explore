        # german_weather — ask Claude questions about a CrateDB weather cluster

A small Python program that hands Claude two ways to query a CrateDB
cluster holding German weather data:

1. **`cratedb-mcp`** — the official Crate.io MCP server, launched as a
   subprocess. Exposes generic tools like `query_sql` and
   `get_table_columns`.
2. **`gw`** — an in-process MCP server we build at startup by parsing
   `grafana/german_weather_data.json`. Every dashboard panel becomes a
   tool whose name comes from the panel title and whose SQL is the
   panel's `rawSql`.

At runtime you pick one of 8 canned questions or type your own, and
Claude streams tool calls and answers back to the terminal.

---

## Files

| File | Purpose |
|---|---|
| `german_weather.py` | Entry point. CLI parsing, config resolution, menu, agent loop. |
| `german_weather_panels.py` | Reads the Grafana JSON and turns each panel into an MCP tool. |
| `requirements.txt` | Python dependencies. |

The Grafana dashboard itself lives outside this directory at
`../../../grafana/german_weather_data.json` (resolved via
`Path(__file__).resolve().parents[3]`).

---

## Prerequisites

```bash
pip install -r requirements.txt
uv tool install --upgrade cratedb-mcp
```

You will also need:

- A reachable CrateDB cluster (default port `4200`).
- An Anthropic API key (`sk-ant-...`).

---

## Configuration

Every required value can be passed as a CLI flag *or* an environment
variable. CLI flags win.

| Flag | Env var | Required? | Default |
|---|---|---|---|
| `--cratedb-url` | `CRATEDB_CLUSTER_URL` | one of url / host (must embed user:password) | — |
| `--cratedb-host` | `CRATEDB_HOST` | one of url / host | — |
| `--cratedb-port` | `CRATEDB_PORT` | no | `4200` |
| `--cratedb-user` | `CRATEDB_USER` | yes (when using --cratedb-host) | — |
| `--cratedb-password` | `CRATEDB_PASSWORD` | yes (when using --cratedb-host) | — |
| `--cratedb-scheme` | `CRATEDB_SCHEME` | no | `http` |
| `--anthropic-api-key` | `ANTHROPIC_API_KEY` | yes | — |

Credentials are mandatory: anonymous CrateDB access would 401 on
every tool call, so the script refuses to start without a user and
password. If you supply `--cratedb-url` it must embed `user:password@`;
otherwise pass `--cratedb-user` and `--cratedb-password` alongside
`--cratedb-host`. If anything required is missing the program prints
every missing item at once and exits with code 1.

---

## Running

Provide either a full URL or the host parts. The Anthropic key can
come from the environment.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python german_weather.py \
    --cratedb-host 10.13.1.19 \
    --cratedb-user scott \
    --cratedb-password tiger
```

You will see a startup banner, the list of registered panel tools,
the MCP connection statuses, and then the question menu:

```
[panels] registered 15 panel tool(s):
          - keyword_relevance_to_search_categories
          - search_categories_and_region
          - last_measurement_time
          ...
[mcp] cratedb: connected
[mcp] gw: connected

Select a question:
  1. Latest measurement timestamp in the dataset
  2. Min / Avg / Max temperature across all of Germany (Dec 2025)
  3. Min / Avg / Max air pressure across all of Germany (Dec 2025)
  4. Min / Avg / Max wind speed across all of Germany (Dec 2025)
  5. Temperature snapshot across Germany at the most recent reading
  6. Pressure snapshot across Germany at the most recent reading
  7. Wind speed & direction snapshot across Germany
  8. Coldest place in Germany on 2025-12-31
  9. Enter your own question

Choice [1-9]:
```

Pick a number. For option 9 you'll be prompted again for the
question text.

While the agent runs, every tool call shows up on stdout as

```
[tool] mcp__gw__last_measurement_time({'time_from': '2025-12-01', ...})
```

and the final answer is printed under `=== answer ===`.

---

## How Grafana panels become MCP tools

`german_weather_panels.py` does this in three steps every time the
program starts.

### 1. Parse the dashboard JSON

The Grafana dashboard is a JSON document. The relevant shape is:

```jsonc
{
  "panels": [
    {
      "id": 9,
      "type": "timeseries",
      "title": "Min, Average and Max temperature for $region",
      "targets": [
        {
          "refId": "A",
          "rawSql": "SELECT measurement_time AS time, ... WHERE $__timeFilter(\"measurement_time\") ..."
        }
      ]
    },
    { "type": "row", "title": "Change over time" }
  ]
}
```

We iterate `panels`, skip `"type": "row"` (those are just collapsible
section headers — no queries), and pull `targets[0].rawSql` from each
remaining panel.

### 2. Discover what arguments each panel needs

The rawSQL contains Grafana template variables that don't exist in
real SQL:

| In SQL | Meaning |
|---|---|
| `$region`, `${region}` | A user-supplied dashboard variable. |
| `$latitude`, `$longitude` | Coordinate variables. |
| `$keyword`, `$search_categories` | Free-text search inputs. |
| `$__timeFilter("col")` | Macro: range predicate against `col` based on the dashboard's time picker. |

`_scan_sql()` regexes the SQL to figure out which variables are
referenced. Those become parameters on the MCP tool. If
`$__timeFilter()` is present, we also add `time_from` and `time_to`
parameters (ISO strings).

### 3. Register one MCP tool per panel

`_make_tool()` builds an `@tool`-decorated async function whose body:

1. Calls `_render_sql()` to substitute the caller's arguments into
   the template — replacing `$__timeFilter("col")` with `(col >= '...' AND col <= '...')`
   and `$var` / `${var}` with the matching argument.
2. POSTs the rendered SQL to CrateDB's HTTP `_sql` endpoint via
   `httpx`.
3. Returns the rows formatted as a single MCP text content block.

The tool's *name* is derived from the panel title via
`_slugify()`. For example:

| Panel title | Tool name |
|---|---|
| `Min, Average and Max temperature for $region` | `min_average_and_max_temperature_for_region` |
| `Last Measurement Time` | `last_measurement_time` |
| `'$keyword' relevance to '$search_categories'` | `keyword_relevance_to_search_categories` |

If two panels slugify to the same name (the dashboard has two
panels both titled "Temperature"), the second one gets a
`_<panel_id>` suffix.

Once all panels are processed we hand the list of tools to
`create_sdk_mcp_server("german_weather_panels", tools=tools)` and
return the server object. `german_weather.py` registers it under
the namespace key `"gw"`, so Claude sees the tools as
`mcp__gw__<slug>`.

---

## How Claude knows about the tools

The system prompt does **not** list the tools. Tool advertisement
goes through a separate field in the Anthropic API request:

```
POST /v1/messages
{
  "system":   "<system prompt text>",
  "tools":    [ { "name": "mcp__gw__last_measurement_time",
                  "description": "Grafana panel '...'. ...",
                  "input_schema": { ... } }, ... ],
  "messages": [ ... ]
}
```

The agent SDK fills `tools` by asking each registered MCP server
for its catalogue:

- `cratedb-mcp` (stdio subprocess) — replies over its JSON-RPC pipe.
- `gw` (in-process) — reads the `SdkMcpTool` list passed to
  `create_sdk_mcp_server`.

Claude chooses tools based on name + description + input_schema. The
system prompt is for steering *behaviour* — preferring `mcp__gw__*`
over raw SQL, converting Kelvin to Celsius, probing before declining,
etc. See the comment block above `system_prompt=` in
`german_weather.py` for the rationale behind each rule.

---

## Architecture diagram

```
        ┌──────────────────────────────────────────────────────────────┐
        │  german_weather.py                                           │
        │                                                              │
        │   parse_args ─► resolve_config ─► choose_prompt ─► run()     │
        │                                                              │
        │   run() builds ClaudeAgentOptions and calls query(...)       │
        │                                                              │
        └──────┬──────────────────────────────────────────┬────────────┘
               │                                          │
               ▼                                          ▼
   ┌───────────────────────────┐         ┌──────────────────────────────┐
   │  claude-agent-sdk         │         │  german_weather_panels.py    │
   │                           │         │                              │
   │  spawns cratedb-mcp,      │         │  reads grafana/...json       │
   │  streams events back      │         │  builds 15 @tool functions   │
   │                           │         │  → create_sdk_mcp_server     │
   └────┬─────────────┬────────┘         └──────────────────────────────┘
        │             │                          ▲
        │             │ tool calls               │ tool calls (in-proc)
        ▼             ▼                          │
  ┌───────────┐  ┌──────────────────────────────────────────────────┐
  │  Claude   │  │   CrateDB cluster                                │
  │  API      │  │   - demo.climate_data                            │
  └───────────┘  │   - demo.german_regions                          │
                 │   - demo.geo_points                              │
                 └──────────────────────────────────────────────────┘
```

---

## Adapting it to another dashboard

The panels module is mostly dashboard-agnostic. To point it at a
different Grafana JSON:

1. Change `DASHBOARD_PATH` in `german_weather_panels.py`.
2. Add any new template-variable names to `_VAR_SPEC` with a
   parameter name, Python type, and description.
3. Update the system prompt in `german_weather.py` so Claude knows
   what the dataset contains and which tools to prefer.

Macros beyond `$__timeFilter` (e.g. `$__interval`, `$__unixEpochFilter`)
are not handled; add cases to `_render_sql` if you need them.
