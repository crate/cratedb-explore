<img src="../doc/crate-logo.svg" alt="CrateDB" width="200">


# Model Context Protocol and CrateDB — ask Claude to query a CrateDB cluster 

<p align="center">
  <img src="../doc/mcp_server.png" alt="MCP search CLI in action" width="100%"
       style="border: 1px solid #d0d7de; border-radius: 6px; padding: 4px; background: #fff;">
</p>
Screenshot of our 'GermanWeather' MCP Java program running.

---

[CrateDB Explore](https://github.com/crate/cratedb-explore/tree/main) has a Model Context Protocol example, in addition to 
several other features. **MCP Search** is a command-line application that lets [Claude](https://www.anthropic.com/claude)
answer plain-English questions about a CrateDB cluster holding German
weather data. Instead of writing SQL yourself, you ask a question — *"Where
was the coldest place in Germany on New Year's Eve?"* — and Claude decides
which tools to call, runs the queries against CrateDB, and explains the
answer.

It is part of the [CrateDB Explore](https://github.com/crate/cratedb-explore/tree/main)
project. The full source lives at
**[github.com/crate/cratedb-explore](https://github.com/crate/cratedb-explore/tree/main)**,
under [`src_mcp_search/`](https://github.com/crate/cratedb-explore/tree/main/src_mcp_search).

---

## What makes it interesting

The clever part is where the tools come from. Rather than hand-writing a
fixed set of queries, MCP Search reads the project's
[Grafana dashboard](https://github.com/crate/cratedb-explore/blob/main/grafana/german_weather_data.json)
and turns **every dashboard panel into a callable tool**. The panel's title
becomes the tool name, and the panel's SQL becomes the query template —
so the questions Claude can answer stay in sync with whatever the dashboard
already visualises. When we wrote this we asked Claude to use the dashboard 
as a basis for a set of tools. We **thought** Claude would do a single pass, 
scrape the SQL, and get to work. Instead Claude came up with this...

On top of those panel tools, Claude also has a generic SQL tool for
anything the panels don't cover. Two layers, one conversation:

1. **Panel tools** — one per Grafana panel, with parameters derived from the
   panel's Grafana template variables (`$region`, `$keyword`, `$latitude`,
   `$longitude`, time range). Preferred, because they carry the dashboard's
   vetted SQL.
2. **A generic SQL tool** — `query_sql` (or the official
   [`cratedb-mcp`](https://github.com/crate/cratedb-mcp) server in the Python
   build) for arbitrary fallback queries.

Claude is steered by a [system prompt](https://github.com/crate/cratedb-explore/blob/main/src_mcp_search/main/java/GermanWeather.java#L126) that encodes the dataset's quirks — for
example, temperatures are stored in **Kelvin** and always shown in Celsius
first, and *"in Germany"* questions are polygon-filtered so near-border
foreign towns don't sneak into the results.

---

## Three implementations - Python, Java and .NET

The application is implemented three times, producing the same behaviour from
the same Grafana dashboard and the same SQL. Pick whichever runtime you're
most comfortable with.

| Language | Directory | How it talks to Claude & CrateDB |
| -------- | --------- | -------------------------------- |
| [Python](main/python/README.md) | [`main/python/`](https://github.com/crate/cratedb-explore/tree/main/src_mcp_search/main/python) | [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) with real **MCP** — spawns [`cratedb-mcp`](https://github.com/crate/cratedb-mcp) as a subprocess and runs the panel tools as an in-process MCP server |
| [Java](main/java/README.md) | [`main/java/`](https://github.com/crate/cratedb-explore/tree/main/src_mcp_search/main/java) | [Anthropic Java SDK](https://github.com/anthropics/anthropic-sdk-java); tools dispatched in-process (no MCP), CrateDB over HTTP `_sql` |
| [.NET (C#)](main/dotnet/README.md) | [`main/dotnet/`](https://github.com/crate/cratedb-explore/tree/main/src_mcp_search/main/dotnet) | Plain `HttpClient` to the Anthropic Messages API (no SDK, no MCP), CrateDB over HTTP `_sql` |

> **MCP vs. no MCP.** Only the Python build routes through the Model Context
> Protocol, because the `claude-agent-sdk` bundles an MCP client. The Java and
> .NET builds wire their tools directly through the Anthropic Messages API and
> advertise them under bare names instead of `mcp__cratedb__*` / `mcp__gw__*`.
> The end result is identical — every version can reach every table.

---

## Prerequisites

- **A reachable CrateDB cluster** with the demo dataset loaded, exposing its
  HTTP `_sql` endpoint on **port 4200**. MCP Search uses HTTP (not the
  PostgreSQL wire protocol the load generators use), sending a
  `Default-Schema: demo` header on every request.
- **An Anthropic API key** (`sk-ant-...`). This is *not* part of a normal
  Claude.ai subscription (Free, Pro, or Max) — those plans cover the chat
  apps, not programmatic API access. The API is billed separately, pay-as-you-go
  per token, and you need to add credit before the key will work. Sign up and
  create a key in the [Anthropic Console](https://console.anthropic.com/), under
  **Settings → API keys**; see the [pricing page](https://www.anthropic.com/pricing#api)
  for current per-token rates.
- The runtime for your chosen implementation: Python 3 + `pip`, Java 17+ and
  Maven, or the .NET 10 SDK.

The demo dataset (the `demo` schema with `climate_data`, `german_regions`,
and `geo_points`) is set up from the
[`sql/`](https://github.com/crate/cratedb-explore/tree/main/sql) directory at
the repository root — run the DDL then the DML script. See the
[main project README](https://github.com/crate/cratedb-explore/tree/main#data-and-schema)
for details.

---

## Setup

Clone the repository and move into this directory:

```bash
git clone https://github.com/crate/cratedb-explore.git
cd cratedb-explore/src_mcp_search
```

Then follow the install step for your runtime:

**Python**
```bash
cd main/python
pip install -r requirements.txt
uv tool install --upgrade cratedb-mcp     # the official CrateDB MCP server
```

**Java**
```bash
cd main/java
mvn compile
```

**.NET**
```bash
cd main/dotnet
dotnet build
```

---

## Configuration

Every implementation reads the same settings, and each value can be supplied
as a **command-line flag** or an **environment variable** (the flag wins).
Credentials are required up front — anonymous CrateDB access would return
`401` on every tool call, so the program refuses to start without them.

| Flag | Environment variable | Required? | Default |
| ---- | -------------------- | --------- | ------- |
| `--cratedb-url` | `CRATEDB_CLUSTER_URL` | one of url / host (url must embed `user:password@`) | — |
| `--cratedb-host` | `CRATEDB_HOST` | one of url / host | — |
| `--cratedb-port` | `CRATEDB_PORT` | no | `4200` |
| `--cratedb-user` | `CRATEDB_USER` | yes (with `--cratedb-host`) | — |
| `--cratedb-password` | `CRATEDB_PASSWORD` | yes (with `--cratedb-host`) | — |
| `--cratedb-scheme` | `CRATEDB_SCHEME` | no | `http` |
| `--anthropic-api-key` | `ANTHROPIC_API_KEY` | yes | — |

---

## Usage

Set your Anthropic key once, then start your chosen implementation with the
CrateDB connection details:

**Python**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python german_weather.py \
    --cratedb-host 10.13.1.19 \
    --cratedb-user scott \
    --cratedb-password tiger
```

**Java**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
mvn exec:java -Dexec.args="\
    --cratedb-host 10.13.1.19 \
    --cratedb-user scott \
    --cratedb-password tiger"
```

**.NET**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
dotnet run -- \
    --cratedb-host 10.13.1.19 \
    --cratedb-user scott \
    --cratedb-password tiger
```

At startup the program lists the panel tools it registered from the Grafana
dashboard, then offers a menu of canned questions (or your own):

```
[panels] registered 15 panel tool(s):
          - keyword_relevance_to_search_categories
          - last_measurement_time
          ...

Select a question:
  1. Latest measurement timestamp in the dataset
  2. Min / Avg / Max temperature across all of Germany (Dec 2025)
  ...
  8. Coldest place in Germany on 2025-12-31
  9. Enter your own question

Choice [1-9]:
```

Pick a number (option **9** prompts you for free-text). As Claude works,
every tool call is traced to the terminal:

```
[tool] last_measurement_time({'time_from': '2025-12-01', 'time_to': '2025-12-31'})
```

and the final, human-readable answer is printed at the end.

---

## How it works, end to end

```
   Your question
        │
        ▼
   ┌─────────────────────────────────────────────┐
   │  MCP Search CLI                             │
   │   • loads grafana/german_weather_data.json  │
   │   • builds one tool per dashboard panel     │
   │   • adds a generic SQL tool                 │
   │   • sends question + tools to Claude        │
   └───────────────┬─────────────────────────────┘
                   │ tool calls
        ┌──────────┴───────────┐
        ▼                      ▼
   ┌──────────┐        ┌──────────────────────┐
   │  Claude  │        │  CrateDB (HTTP :4200) │
   │   API    │        │   demo.climate_data   │
   └──────────┘        │   demo.german_regions │
                       │   demo.geo_points     │
                       └──────────────────────┘
```

1. On startup the CLI parses the Grafana dashboard JSON, skips the row/section
   headers, and turns each remaining panel's `rawSql` into a tool template.
   Grafana template variables become JSON-schema parameters, and the
   `$__timeFilter("col")` macro becomes a `time_from` / `time_to` range
   predicate.
2. Your question, the system prompt, and the full tool catalogue go to Claude.
3. Claude calls tools as needed; the CLI substitutes the arguments into the
   SQL template and POSTs it to CrateDB's `_sql` endpoint.
4. Results flow back to Claude, which repeats until it can answer, then writes
   the answer to the terminal.

Each implementation's README documents the mechanics in full, including the
slugification rules that turn panel titles into tool names, the agent loop,
and the SQL conventions baked into the system prompt:

- **[Python README](main/python/README.md)** — MCP architecture, panel-to-tool pipeline, system-prompt rationale
- **[Java README](main/java/README.md)** — manual agent loop on the Anthropic Java SDK
- **[.NET README](main/dotnet/README.md)** — direct Messages-API integration over `HttpClient`

---

## Related

- 🌐 **Project repository:** [github.com/crate/cratedb-explore](https://github.com/crate/cratedb-explore/tree/main)
- 📊 [Weather load generators](https://github.com/crate/cratedb-explore/tree/main/src_weather) — drive the same dataset over the PostgreSQL wire protocol
- 🔎 [KNN Search CLI](https://github.com/crate/cratedb-explore/tree/main/src_knn_search) — semantic + BM25 search over `german_regions`
- 📈 [Grafana dashboard](https://github.com/crate/cratedb-explore/blob/main/grafana/german_weather_data.json) — the source of the panel tools

## License

Apache License 2.0. See the [LICENSE](https://github.com/crate/cratedb-explore/blob/main/LICENSE) file.
