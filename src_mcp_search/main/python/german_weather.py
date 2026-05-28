"""
Ask Claude questions about a German-weather CrateDB cluster.

----------------------------------------------------------------------
What this script does
----------------------------------------------------------------------

We give Claude access to two MCP (Model Context Protocol) servers:

  1. `cratedb-mcp` — an external stdio subprocess shipped by Crate.io
     that exposes generic CrateDB tools (`query_sql`,
     `get_table_columns`, etc.). The agent SDK launches it for us.

  2. `gw` — an in-process MCP server we build at startup from the
     panels in `grafana/german_weather_data.json`. Every dashboard
     panel becomes one tool whose name is derived from its title and
     whose SQL is the panel's rawSql. See `german_weather_panels.py`
     for the construction logic.

The user picks one of 8 canned prompts or types their own, and the
agent SDK runs the agent loop until Claude either calls tools, prints
text, or finishes with a final answer. We stream events as they
arrive so the user can see SQL being issued in real time.

----------------------------------------------------------------------
Configuration
----------------------------------------------------------------------

Host/API details can be supplied via command-line flags or environment
variables. Command-line flags take precedence.

  Either provide a full CrateDB URL:
      --cratedb-url / CRATEDB_CLUSTER_URL
          e.g. http://scott:tiger@10.13.1.19:4200/

  Or assemble it from parts:
      --cratedb-host     / CRATEDB_HOST       (required if no URL)
      --cratedb-port     / CRATEDB_PORT       (default: 4200)
      --cratedb-user     / CRATEDB_USER       (optional)
      --cratedb-password / CRATEDB_PASSWORD   (optional)
      --cratedb-scheme   / CRATEDB_SCHEME     (default: http)

  Anthropic API key:
      --anthropic-api-key / ANTHROPIC_API_KEY (required)

Prerequisites:
    pip install claude-agent-sdk
    uv tool install --upgrade cratedb-mcp

Run:
    python german_weather.py --cratedb-host 10.13.1.19 \
        --cratedb-user scott --cratedb-password tiger
"""

import argparse
import asyncio
import os
from urllib.parse import quote, urlparse

# The agent SDK is the high-level orchestrator. `query(...)` runs the
# entire agent loop and yields messages as they arrive. The Block /
# Message dataclasses below are what we get back — see the streaming
# loop at the bottom of run() for the dispatch.
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from german_weather_panels import build_panel_server


def parse_args() -> argparse.Namespace:
    """Wire up the CLI flags. Defaults are intentionally all None so
    that we can layer them on top of environment variables in
    `resolve_cratedb_url` — argparse defaults would clobber the env."""
    parser = argparse.ArgumentParser(
        description="Query a CrateDB cluster via Claude + cratedb-mcp.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--cratedb-url", help="Full CrateDB URL (overrides individual parts).")
    parser.add_argument("--cratedb-host", help="CrateDB host (e.g. 10.13.1.19).")
    parser.add_argument("--cratedb-port", help="CrateDB port.")
    parser.add_argument("--cratedb-user", help="CrateDB username.")
    parser.add_argument("--cratedb-password", help="CrateDB password.")
    parser.add_argument("--cratedb-scheme", help="CrateDB scheme (http or https).")
    parser.add_argument("--anthropic-api-key", help="Anthropic API key.")
    return parser.parse_args()


def resolve_cratedb_url(args: argparse.Namespace) -> tuple[str | None, list[str]]:
    """
    Build the CrateDB connection URL from CLI args + env vars.

    Resolution rules (first non-empty wins per row):

        flag --cratedb-url  >  env CRATEDB_CLUSTER_URL
        flag --cratedb-host >  env CRATEDB_HOST
        ...etc.

    If a complete URL is supplied directly we return it unchanged.
    Otherwise we assemble the URL from parts, URL-encoding the
    username and password so special characters like `@`, `/`, `:`
    inside credentials don't break the URL grammar.

    Returns:
      (url, missing) — url is None when we couldn't build one. The
      caller (resolve_config) aggregates the `missing` list across
      all required pieces so the user sees every problem at once,
      not one error per re-run.
    """
    missing: list[str] = []

    # Easy path: a full URL was given. Still verify that it embeds
    # credentials — an unauthenticated URL would silently 401 every
    # request later. Treating this as a config error gives a clear
    # message at startup instead of a cryptic tool failure mid-run.
    url = args.cratedb_url or os.environ.get("CRATEDB_CLUSTER_URL")
    if url:
        parsed = urlparse(url)
        if not parsed.username:
            missing.append(
                "CrateDB URL must include a username, e.g. "
                "http://user:password@host:4200/ "
                "(--cratedb-url or CRATEDB_CLUSTER_URL)"
            )
        if not parsed.password:
            missing.append(
                "CrateDB URL must include a password, e.g. "
                "http://user:password@host:4200/ "
                "(--cratedb-url or CRATEDB_CLUSTER_URL)"
            )
        if missing:
            return None, missing
        return url, missing

    # Otherwise we need at least a host.
    host = args.cratedb_host or os.environ.get("CRATEDB_HOST")
    if not host:
        missing.append("CrateDB host (--cratedb-host or CRATEDB_HOST), or a full --cratedb-url / CRATEDB_CLUSTER_URL")
        return None, missing

    port = args.cratedb_port or os.environ.get("CRATEDB_PORT") or "4200"
    scheme = args.cratedb_scheme or os.environ.get("CRATEDB_SCHEME") or "http"
    user = args.cratedb_user or os.environ.get("CRATEDB_USER")
    password = args.cratedb_password or os.environ.get("CRATEDB_PASSWORD")

    # Both user and password are required. Anonymous CrateDB access
    # would 401 immediately on every tool call; better to fail loudly
    # here than to debug 401s further downstream.
    if not user:
        missing.append("CrateDB user (--cratedb-user or CRATEDB_USER) is required")
    if not password:
        missing.append("CrateDB password (--cratedb-password or CRATEDB_PASSWORD) is required")
    if missing:
        return None, missing

    # Assemble the userinfo segment, URL-encoding to keep the URL
    # well-formed. quote(..., safe="") escapes everything that isn't
    # a plain unreserved character.
    auth = quote(user, safe="") + ":" + quote(password, safe="") + "@"

    return f"{scheme}://{auth}{host}:{port}/", missing


def resolve_config(args: argparse.Namespace) -> tuple[str, str]:
    """
    Validate every required piece of config and exit clearly if any
    are missing. The aim is to give the user one consolidated error
    message rather than a cryptic KeyError partway through startup.
    """
    missing: list[str] = []

    cratedb_url, url_missing = resolve_cratedb_url(args)
    missing.extend(url_missing)

    api_key = args.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        missing.append("Anthropic API key (--anthropic-api-key or ANTHROPIC_API_KEY)")

    if missing:
        print("Missing required configuration:")
        for item in missing:
            print(f"  - {item}")
        raise SystemExit(1)

    # The assert is a static-check hint: by this point cratedb_url
    # must be non-None or `missing` would have been non-empty.
    assert cratedb_url is not None
    return cratedb_url, api_key


# Canned questions presented as menu options 1..N. Each tuple is
# (display label, full prompt sent to Claude). Keeping the prompts
# verbose — explicitly naming columns/tables, time windows, the
# 'ALL' sentinel — makes Claude's first tool call much more likely
# to be correct without round-trips to discover the schema.
CANNED_PROMPTS: list[tuple[str, str]] = [
    (
        "Latest measurement timestamp in the dataset",
        "What is the latest measurement timestamp in demo.climate_data? "
        "Use time_from=2025-01-01 and time_to=2025-12-31.",
    ),
    (
        "Min / Avg / Max temperature across all of Germany (Dec 2025)",
        "Show the min, average, and max temperature across all of Germany "
        "for December 2025 (time_from=2025-12-01, time_to=2025-12-31).",
    ),
    (
        "Min / Avg / Max air pressure across all of Germany (Dec 2025)",
        "Show the min, average, and max air pressure across all of Germany "
        "for December 2025 (time_from=2025-12-01, time_to=2025-12-31).",
    ),
    (
        "Min / Avg / Max wind speed across all of Germany (Dec 2025)",
        "Show the min, average, and max wind speed across all of Germany "
        "for December 2025 (time_from=2025-12-01, time_to=2025-12-31).",
    ),
    (
        "Temperature snapshot across Germany at the most recent reading",
        "Show the temperature snapshot across all of Germany at the most "
        "recent measurement time in December 2025 (time_from=2025-12-01, "
        "time_to=2025-12-31).",
    ),
    (
        "Pressure snapshot across Germany at the most recent reading",
        "Show the pressure snapshot across all of Germany at the most recent "
        "measurement time in December 2025 (time_from=2025-12-01, "
        "time_to=2025-12-31).",
    ),
    (
        "Wind speed & direction snapshot across Germany",
        "Show the wind speed and direction snapshot across all of Germany at "
        "the most recent measurement time in December 2025 "
        "(time_from=2025-12-01, time_to=2025-12-31).",
    ),
    (
        "Coldest place in Germany on 2025-12-31",
        "Where was the coldest place in Germany at the most recent "
        "measurement on 2025-12-31? Report town/coordinates and temperature. "
        # The WITHIN-polygon constraint stops border points (e.g.
        # Tannheim, Tyrol) from leaking in via the geo_points join.
        "Restrict candidate points to those that fall inside a German "
        "federal-state polygon by joining demo.climate_data to "
        "demo.german_regions with WITHIN(c.geo_location, r.geo_coords) so "
        "border points in Austria/Switzerland are excluded.",
    ),
]


def choose_prompt() -> str:
    """
    Render a 1..(N+1) menu of canned prompts and return the user's
    selection. The N+1th option is "Enter your own question" and
    triggers a second input() call.

    SystemExit is used rather than raising ValueError so an invalid
    choice produces a clean exit code without a traceback — this
    matches the spirit of resolve_config's user-facing errors.
    """
    print("\nSelect a question:")
    for i, (label, _) in enumerate(CANNED_PROMPTS, start=1):
        print(f"  {i}. {label}")
    print(f"  {len(CANNED_PROMPTS) + 1}. Enter your own question")

    raw = input(f"\nChoice [1-{len(CANNED_PROMPTS) + 1}]: ").strip()
    try:
        choice = int(raw)
    except ValueError:
        raise SystemExit(f"Invalid choice: {raw!r}")

    if 1 <= choice <= len(CANNED_PROMPTS):
        prompt = CANNED_PROMPTS[choice - 1][1]
        # Echo the chosen prompt so it's visible alongside the answer
        # — useful when piping the output into a log.
        print(f"\n> {prompt}")
        return prompt
    if choice == len(CANNED_PROMPTS) + 1:
        prompt = input("Your question: ").strip()
        if not prompt:
            raise SystemExit("No prompt provided.")
        return prompt
    raise SystemExit(f"Choice out of range: {choice}")


async def run(cratedb_url: str, prompt: str) -> None:
    """
    Configure the agent and stream its response to the chosen prompt.

    Steps:
      1. Build the `gw` MCP server from the Grafana dashboard JSON.
         (One tool per panel — see german_weather_panels.py.)
      2. Configure ClaudeAgentOptions with both MCP servers, the
         allowed tool patterns, the system prompt, and a turn cap.
      3. Call query() and iterate the async stream of events,
         printing tool calls and intermediate text as they arrive.
    """
    # The panel server is built once per process. Re-reading the
    # JSON on every tool call would be wasteful and would also make
    # tool identity inconsistent if the file changed mid-run.
    panel_server, panel_tool_names = build_panel_server(cratedb_url)
    print(f"[panels] registered {len(panel_tool_names)} panel tool(s):")
    for n in panel_tool_names:
        print(f"          - {n}")

    options = ClaudeAgentOptions(
        # mcp_servers is keyed by namespace. The key here ("cratedb",
        # "gw") becomes the middle segment of every tool name Claude
        # sees: mcp__cratedb__query_sql, mcp__gw__last_measurement_time,
        # etc. The "mcp__" prefix and "__" separator are imposed by
        # the SDK and cannot be removed.
        mcp_servers={
            "cratedb": {
                # External stdio MCP server. The SDK forks the
                # command, talks JSON-RPC over its stdio pipes, and
                # routes tool calls to it.
                "type": "stdio",
                "command": "cratedb-mcp",
                "args": ["serve", "--transport=stdio"],
                "env": {"CRATEDB_CLUSTER_URL": cratedb_url},
            },
            # In-process SDK MCP server. No subprocess, no IPC —
            # tool calls dispatch directly to the @tool-decorated
            # Python functions in german_weather_panels.py.
            "gw": panel_server,
        },
        # Glob patterns. Anything matching is freely callable; anything
        # else triggers a permission prompt (or is denied in headless
        # mode). Keep these wide here because we trust both servers.
        allowed_tools=[
            "mcp__cratedb__*",
            "mcp__gw__*",
        ],
        # Hard cap on agent loop iterations to keep cost bounded if
        # Claude gets stuck calling tools without making progress.
        # A bit more headroom — Claude will likely:
        #   1. list tables to find weather/temperature data
        #   2. inspect columns on the candidate table
        #   3. run the actual query
        #   4. summarize
        max_turns=15,
        # System prompt — invisible to the API's `tools` discovery
        # mechanism (that's a separate field) but used to steer
        # behaviour. Each rule below was added in response to a
        # specific failure mode seen during development:
        #   - "Prefer mcp__gw__*"            : Claude wrote raw SQL even when a panel matched.
        #   - "region='ALL'"                 : Claude passed 'Germany' and got 0 rows.
        #   - "discover before declining"    : Claude refused a question without probing.
        #   - "Kelvin -> Celsius (K) prose"  : Claude left raw Kelvin values in SQL echoes.
        #   - "german_regions has tourism..." : Pointer so Claude knows that table holds
        #                                       prose data beyond region names.
        system_prompt=(
            "You are a data analyst with access to a CrateDB cluster via "
            "MCP tools. When answering questions, first discover the "
            "relevant schema, (tables and columns) by reading https://github.com/crate/cratedb-explore/blob/main/src_weather/main/python/README.md ,"
            " then write SQL queries to answer the question. If the data does "
            "not contain what's needed to answer, say so explicitly "
            "rather than guessing. Show the SQL you ran. "
            "Temperature columns in the data are in Kelvin. Every time you "
            "display a temperature value — including raw query results, "
            "table cells, SQL output echoes, and prose — show it as Celsius "
            "first, then Kelvin in parentheses (e.g. '-8.99 C (264.16 K)'). "
            "Never report a temperature in Kelvin alone. "
            "Prefer `mcp__gw__*` panel tools when one matches the question; "
            "fall back to `mcp__cratedb__query_sql` otherwise. "
            "For Germany-wide queries on `mcp__gw__*_for_region` tools, "
            "pass region='ALL'; the value 'Germany' will return no rows. "
            "Before declaring that the data can't answer a question, you "
            "must first run at least one discovery query — list tables via "
            "information_schema.tables, or try the "
            "keyword_relevance_to_search_categories panel with relevant "
            "terms. Only after you've actually checked may you say the data "
            "doesn't contain what's needed. "
            "Note that the german_regions table has information on tourism, "
            "economics, transportation and introduced species."
        ),
    )

    # `query()` returns an async generator. Each item is a Message
    # object representing one event in the agent loop. We dispatch by
    # message subtype:
    #
    #   SystemMessage(subtype="init")  - emitted once at startup with
    #                                    MCP connection status.
    #   AssistantMessage               - the model's turn: a list of
    #                                    content blocks (text, tool
    #                                    calls, thinking, etc.).
    #   ResultMessage(subtype=...)     - terminal event; contains the
    #                                    final answer text.
    #
    # Anything we don't explicitly handle (UserMessage echoes, etc.)
    # is silently dropped — only what the operator cares about ends
    # up on stdout.
    async for message in query(prompt=prompt, options=options):
        # Startup banner: print the MCP connection status. If any
        # server failed to connect we exit immediately because the
        # rest of the run is doomed.
        if isinstance(message, SystemMessage) and message.subtype == "init":
            for server in message.data.get("mcp_servers", []):
                status = server.get("status")
                name = server.get("name")
                print(f"[mcp] {name}: {status}")
                if status != "connected":
                    raise SystemExit(f"MCP server {name} failed to connect")

        # The assistant's turns can contain multiple content blocks
        # of different types. We trace tool calls (with truncated
        # input) and print any text the model emits inline.
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name.startswith("mcp__"):
                    # Truncate long SQL in the trace so the output stays readable.
                    inp = str(block.input)
                    if len(inp) > 200:
                        inp = inp[:200] + "…"
                    print(f"[tool] {block.name}({inp})")
                elif isinstance(block, TextBlock):
                    print(block.text)

        # Terminal event. The SDK guarantees exactly one of these
        # per run, after which the async generator finishes.
        if isinstance(message, ResultMessage) and message.subtype == "success":
            print("\n=== answer ===")
            print(message.result)


def main() -> None:
    """
    Entry point: parse CLI, validate config, pick a prompt, kick off
    the asyncio event loop and run the agent.
    """
    args = parse_args()
    cratedb_url, api_key = resolve_config(args)
    # Ensure the SDK and any subprocesses see the API key. The agent
    # SDK reads ANTHROPIC_API_KEY from the environment, as does the
    # cratedb-mcp subprocess if it ever needs Anthropic creds for
    # its own purposes. Setting the env var unifies the two.
    os.environ["ANTHROPIC_API_KEY"] = api_key
    prompt = choose_prompt()
    asyncio.run(run(cratedb_url, prompt))


if __name__ == "__main__":
    main()
