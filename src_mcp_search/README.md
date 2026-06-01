# german_weather_mcp — a minimal MCP server over CrateDB

A single-file [Model Context Protocol](https://modelcontextprotocol.io) server
that exposes one tool, `query_sql`, over the German-weather demo data in
CrateDB. Point any MCP client (Claude Code, Claude Desktop, …) at it and ask
questions about the data in plain English.

It speaks to CrateDB's HTTP `_sql` endpoint on port 4200, sending a
`Default-Schema: demo` header on every request so unqualified table names
resolve under the `demo` schema. It is built on the official MCP Python SDK
(`FastMCP`), the same foundation as [`cratedb-mcp`](https://github.com/crate/cratedb-mcp).

## Files

| File | Purpose |
|---|---|
| `german_weather_mcp.py` | The MCP server. Config resolution + one `query_sql` tool. |
| `requirements.txt` | Dependencies: `mcp[cli]`, `httpx`. |
| `GERMAN_WEATHER_MCP.md` | Draft cratedb.com how-to page for this example. |

## Install

```bash
pip install -r requirements.txt
```

## Connection

Defaults to the demo cluster `scott/tiger@10.13.1.19:4200`, so it runs with no
arguments. Override with CLI flags or the matching `CRATEDB_*` environment
variables — flags win.

| Flag | Env var | Default |
|---|---|---|
| `--cratedb-url` | `CRATEDB_CLUSTER_URL` | — (overrides the parts below) |
| `--cratedb-host` | `CRATEDB_HOST` | `10.13.1.19` |
| `--cratedb-port` | `CRATEDB_PORT` | `4200` |
| `--cratedb-user` | `CRATEDB_USER` | `scott` |
| `--cratedb-password` | `CRATEDB_PASSWORD` | `tiger` |
| `--cratedb-scheme` | `CRATEDB_SCHEME` | `http` |

## Try it with the MCP Inspector

The `mcp` dev CLI launches the server with a web Inspector so you can call the
tool by hand:

```bash
mcp dev german_weather_mcp.py
```

Call `query_sql` with `SELECT 1` to confirm connectivity, then try a real query
such as `SELECT region_name FROM german_regions ORDER BY 1`.

## Register with an MCP client

**Claude Code:**

```bash
claude mcp add german-weather -- python /abs/path/to/src_mcp_search/german_weather_mcp.py
```

Pass connection flags after the script if you are not using the demo cluster:

```bash
claude mcp add german-weather -- python /abs/path/german_weather_mcp.py \
    --cratedb-host my-host --cratedb-user me --cratedb-password secret
```

**Claude Desktop** — add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "german-weather": {
      "command": "python",
      "args": ["/abs/path/to/src_mcp_search/german_weather_mcp.py"]
    }
  }
}
```

Then ask, for example: *"What was the coldest place in Germany on 2025-12-31?"*

## The one rule: filter Germany with WITHIN

`demo.geo_points` contains some near-border foreign towns (for example,
Tannheim in Tyrol). A "where in Germany" question answered with `geo_points` or
`DISTANCE()` alone will wrongly include them. The server's `instructions` and
the `query_sql` tool description tell the model to restrict candidates with a
polygon join instead:

```sql
SELECT g.nearest_town, c.data['temperature'] AS kelvin
FROM demo.climate_data c
JOIN demo.german_regions r ON WITHIN(c.geo_location, r.geo_coords)
JOIN demo.geo_points g ON g.geo_location = c.geo_location
WHERE c.measurement_time = '2025-12-31T23:00:00'
ORDER BY kelvin ASC
LIMIT 1;
```

Temperatures are stored in Kelvin; the model is asked to report Celsius first
with Kelvin in parentheses, e.g. `-8.99 C (264.16 K)`.

## Default to the latest data

When a query touches `geo_points` and you don't ask for a specific time range,
the model is told to restrict it to the most recent readings rather than scan
the whole history:

```sql
WHERE measurement_time = (SELECT MAX(d2.measurement_time) FROM demo.climate_data d2)
```

This keeps "where is it coldest right now?"-style questions fast and scoped to
the latest snapshot.
