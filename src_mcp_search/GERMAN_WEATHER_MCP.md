# Query IoT Data in CrateDB With an MCP Server

CrateDB stores high-volume IoT and time-series sensor data and lets you query it
in real time with SQL. The Model Context Protocol (MCP) takes this a step
further: it connects an AI assistant directly to your cluster, so you can ask
questions in plain English and let the assistant write the SQL.

This guide builds a small MCP server over a German weather dataset — a stream of
real-time sensor readings of the kind a typical IoT deployment produces. In a
few minutes you will have an assistant that answers questions such as "What was
the coldest place in Germany yesterday?" against live data.

## What You'll Build

A single Python file that runs as an MCP server and exposes one tool,
`query_sql`. The tool sends statements to CrateDB's HTTP `_sql` endpoint and
returns the rows. Any MCP-capable assistant — Claude Code or Claude Desktop, for
example — can then discover the tool and use it to answer your questions.

## Prerequisites

- Python 3.10 or later.
- A CrateDB cluster with the German weather demo data loaded, reachable on its
  HTTP port (`4200` by default).
- An MCP-capable AI assistant, such as Claude Code or Claude Desktop.

## The Dataset

The demo data lives in the `demo` schema and models a weather sensor network:

- `climate_data` — the sensor readings. Each row has a `geo_location`
  (a `geo_point`), a `measurement_time`, and a `data` object whose
  `temperature` is stored in Kelvin, alongside pressure and wind.
- `german_regions` — the 16 German federal states, each with a `geo_coords`
  polygon that describes its boundary.
- `geo_points` — the weather station locations, with the nearest town for each.

## Step 1 — Install the Dependencies

Install the MCP Python SDK and an HTTP client:

```bash
pip install "mcp[cli]" httpx
```

## Step 2 — Create the Server

Save the following as [`german_weather_mcp.py`](https://github.com/crate/cratedb-explore/blob/main/src_mcp_search/german_weather_mcp.py).
It connects to CrateDB, then defines a single tool that runs SQL against the
`demo` schema.

```python
import httpx
from mcp.server.fastmcp import FastMCP

ENDPOINT = "http://10.13.1.19:4200/_sql"
AUTH = ("scott", "tiger")

INSTRUCTIONS = (
    "Tools query German weather data in the `demo` schema: climate_data "
    "(geo_location, measurement_time, data['temperature'] in Kelvin), "
    "german_regions (16 states with geo_coords polygons), and geo_points "
    "(station locations). Temperatures are Kelvin, so always show Celsius "
    "first with Kelvin in parentheses, e.g. -8.99 C (264.16 K). For any "
    "'where in Germany' question you must restrict candidates with "
    "WITHIN(c.geo_location, r.geo_coords), joining climate_data to "
    "german_regions, because geo_points includes near-border foreign towns. "
    "When a query touches geo_points and the user gives no time range, limit it "
    "to the latest data with measurement_time = (SELECT MAX(d2.measurement_time) "
    "FROM demo.climate_data d2)."
)

mcp = FastMCP("german-weather", instructions=INSTRUCTIONS)


@mcp.tool()
def query_sql(statement: str) -> str:
    """Run a read-only SQL statement against the CrateDB `demo` schema."""
    response = httpx.post(
        ENDPOINT,
        json={"stmt": statement},
        auth=AUTH,
        headers={"Default-Schema": "demo"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return f"columns: {data['cols']}\nrows: {data['rows']}"


if __name__ == "__main__":
    mcp.run()
```

Three details make this work against CrateDB:

- The server posts to the HTTP `_sql` endpoint on port `4200` and reads the
  `cols` and `rows` from the JSON response.
- Each request carries a `Default-Schema: demo` header. The `_sql` endpoint is
  stateless, so this header is the durable equivalent of `SET search_path TO
  demo` and lets the assistant use unqualified table names.
- The `instructions` and the tool description carry the data rules — Kelvin
  display, the geographic filter, and a default to the latest data — so the
  assistant applies them without being reminded in every prompt.

When a question about station locations gives no time range, the assistant
scopes the query to the most recent readings rather than the whole history:

```sql
WHERE measurement_time = (SELECT MAX(d2.measurement_time) FROM demo.climate_data d2)
```

## Step 3 — Register the Server

For Claude Code, add the server from the command line:

```bash
claude mcp add german-weather -- python /path/to/german_weather_mcp.py
```

For Claude Desktop, add an entry to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "german-weather": {
      "command": "python",
      "args": ["/path/to/german_weather_mcp.py"]
    }
  }
}
```

Restart the assistant so it picks up the new server.

## Step 4 — Ask a Question

With the server registered, ask a question in natural language:

> What is the single lowest temperature reading anywhere inside Germany?

The assistant writes and runs the SQL through `query_sql`, then answers with the
temperature in Celsius first and Kelvin in parentheses. Run against the demo
cluster, it returns:

> The single lowest temperature reading anywhere inside Germany is
> **-10.41 C (262.74 K)**, at `[12.75, 50.5]` in the Vogtland area of Saxony,
> near the Czech border.

## Filtering by Geography

One rule is worth calling out, because it is easy to get wrong. The
`geo_points` table includes a handful of towns just across the border — for
instance, Tannheim in Tyrol. A query that uses `geo_points` or `DISTANCE()`
alone to decide what counts as "in Germany" will quietly include them and report
the wrong answer.

The correct approach is a polygon join: keep only the readings whose location
falls inside a German state boundary.

```sql
SELECT g.nearest_town, c.data['temperature'] AS kelvin
FROM demo.climate_data c
JOIN demo.german_regions r ON WITHIN(c.geo_location, r.geo_coords)
JOIN demo.geo_points g ON g.geo_location = c.geo_location
ORDER BY kelvin ASC
LIMIT 1;
```

Because the rule lives in the server's instructions, the assistant adds the
`WITHIN` join on its own whenever a question is scoped to Germany. The
lowest-temperature question above shows it working: the three coldest values in
the raw data — down to 261.08 K (-12.07 C) — all sit at `[10.5, 47.5]`, which is
Tannheim in Tyrol, just outside the German polygons. The polygon join excludes
them, so the lowest reading that is genuinely inside Germany is the -10.41 C
(262.74 K) value reported above.

## Performance

The latest-data default is not only about correctness; it keeps geospatial
questions fast. Measured against the demo cluster — a network of 727 stations —
over the HTTP `_sql` endpoint, the round-trip latencies are:

| Query | p50 | p90 | p99 |
| --- | --- | --- | --- |
| Region metadata lookup | 2.5 ms | 3.0 ms | 4.9 ms |
| Coldest stations, scoped to the latest reading | 97 ms | 102 ms | 107 ms |
| Stations per state, no time filter | 7.65 s | 7.72 s | 7.76 s |

The point-in-polygon `WITHIN` join is the expensive part of any "in Germany"
query. Scoping `climate_data` to the latest snapshot before that join, as the
server's instructions ask, keeps the coldest-stations question near 100 ms —
roughly 75 times faster than the unscoped polygon scan in the bottom row.

## Next Steps

- Read the [CrateDB MCP documentation](https://cratedb.com/docs/guide/integrate/mcp/cratedb-mcp.html)
  for the full picture, including documentation retrieval.
- Explore [`cratedb-mcp`](https://github.com/crate/cratedb-mcp), the
  ready-to-run MCP server from Crate.io, for production use.
