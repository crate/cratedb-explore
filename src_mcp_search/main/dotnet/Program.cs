// Licensed to Crate.io GmbH ("Crate") under one or more contributor
// license agreements.  See the NOTICE file distributed with this work for
// additional information regarding copyright ownership.  Crate licenses
// this file to you under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.  You may
// obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GermanWeather;

/// <summary>
/// .NET port of <c>GermanWeather.java</c> — ask Claude questions about
/// a CrateDB cluster holding German weather data. Mirrors the Java
/// port's design: instead of an MCP client we expose tools directly
/// via Anthropic's tool-use API. One tool per Grafana dashboard
/// panel, plus a generic <c>query_sql</c> tool.
///
/// The Anthropic Messages API is called over plain HTTP rather than
/// via a third-party SDK to keep the dependency surface small and
/// match the rest of the project's style.
/// </summary>
public static class Program
{
    private const string Model = "claude-opus-4-7";
    private const int MaxTurns = 30;
    private const int MaxTokens = 4096;
    private const string AnthropicVersion = "2023-06-01";
    private const string AnthropicEndpoint = "https://api.anthropic.com/v1/messages";

    private static readonly List<(string Title, string Prompt)> CannedPrompts = new()
    {
        ("Latest measurement timestamp in the dataset",
         "What is the latest measurement timestamp in demo.climate_data? "
         + "Use time_from=2025-01-01 and time_to=2025-12-31."),
        ("Min / Avg / Max temperature across all of Germany (Dec 2025)",
         "Show the min, average, and max temperature across all of Germany "
         + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."),
        ("Min / Avg / Max air pressure across all of Germany (Dec 2025)",
         "Show the min, average, and max air pressure across all of Germany "
         + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."),
        ("Min / Avg / Max wind speed across all of Germany (Dec 2025)",
         "Show the min, average, and max wind speed across all of Germany "
         + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."),
        ("Temperature snapshot across Germany at the most recent reading",
         "Show the temperature snapshot across all of Germany at the most "
         + "recent measurement time in December 2025 (time_from=2025-12-01, "
         + "time_to=2025-12-31)."),
        ("Pressure snapshot across Germany at the most recent reading",
         "Show the pressure snapshot across all of Germany at the most recent "
         + "measurement time in December 2025 (time_from=2025-12-01, "
         + "time_to=2025-12-31)."),
        ("Wind speed & direction snapshot across Germany",
         "Show the wind speed and direction snapshot across all of Germany at "
         + "the most recent measurement time in December 2025 "
         + "(time_from=2025-12-01, time_to=2025-12-31)."),
        ("Coldest place in Germany on 2025-12-31",
         "Where was the coldest place in Germany at the most recent "
         + "measurement on 2025-12-31? Report town/coordinates and temperature. "
         + "Restrict candidate points to those that fall inside a German "
         + "federal-state polygon by joining demo.climate_data to "
         + "demo.german_regions with WITHIN(c.geo_location, r.geo_coords) so "
         + "border points in Austria/Switzerland are excluded."),
    };

    private const string SystemPrompt =
        "You are a data analyst with access to a CrateDB cluster via tools. "
        + "CRITICAL RULE — GERMANY GEOGRAPHIC FILTER. For ANY question "
        + "asking 'where is the coldest / warmest / highest / lowest / "
        + "rainiest / driest / most extreme X place in Germany' (or any "
        + "ranking of locations), you MUST restrict candidate points via "
        + "WITHIN(c.geo_location, r.geo_coords) by joining "
        + "demo.climate_data c to demo.german_regions r. Do NOT use "
        + "demo.geo_points or DISTANCE() alone to decide whether a "
        + "location counts as 'in Germany' — geo_points contains "
        + "near-border foreign towns (Tannheim in Tyrol, for example), "
        + "so without the polygon filter your answer will be wrong. "
        + "Concretely: a grid cell at lat 47.5, lon 10.5 fails the "
        + "WITHIN filter — Tannheim is not in Germany; lat 47.5, "
        + "lon 10.25 (Ofterschwang, Bayern) is correct. "
        + "When answering questions, first discover the relevant schema (tables "
        + "and columns), then write SQL queries to answer the question. If the "
        + "data does not contain what's needed to answer, say so explicitly "
        + "rather than guessing. Show the SQL you ran. "
        + "Temperature columns in the data are in Kelvin. Every time you "
        + "display a temperature value — including raw query results, table "
        + "cells, SQL output echoes, and prose — show it as Celsius first, then "
        + "Kelvin in parentheses (e.g. '-8.99 C (264.16 K)'). Never report a "
        + "temperature in Kelvin alone. "
        + "Prefer panel tools (the ones generated from the Grafana dashboard) "
        + "when one matches the question; fall back to query_sql otherwise. "
        + "For Germany-wide queries on the *_for_region tools, pass "
        + "region='ALL'; the value 'Germany' will return no rows. "
        + "Before declaring that the data can't answer a question, you must "
        + "first run at least one discovery query — list tables via "
        + "information_schema.tables, or try the "
        + "keyword_relevance_to_search_categories panel with relevant terms. "
        + "Only after you've actually checked may you say the data doesn't "
        + "contain what's needed. "
        + "Note that the german_regions table has information on tourism, "
        + "economics, transportation and introduced species. "
        + "All demo tables live in the 'demo' schema. The HTTP transport "
        + "already sends Default-Schema: demo so unqualified table names "
        + "resolve there.";

    public static async Task<int> Main(string[] argv)
    {
        var cfg = Config.Parse(argv);
        var crate = new CrateDbClient(cfg.CratedbUrl);

        var dashboard = PanelTools.FindDashboard();
        if (dashboard == null)
        {
            Console.Error.WriteLine("Could not locate grafana/german_weather_data.json relative to cwd");
            return 1;
        }

        var panelTools = await PanelTools.LoadFromFileAsync(dashboard, crate).ConfigureAwait(false);
        Console.WriteLine($"[panels] registered {panelTools.Count} panel tool(s):");
        foreach (var t in panelTools)
        {
            Console.WriteLine("          - " + t.Name);
        }

        // Generic query_sql tool — what cratedb-mcp would expose.
        var querySql = new PanelTools.PanelTool(
            "query_sql",
            "Execute an arbitrary SQL statement against the CrateDB cluster "
            + "and return columns and rows. Use this when no panel tool fits.",
            new Dictionary<string, object>
            {
                ["query"] = new Dictionary<string, object>
                {
                    ["type"] = "string",
                    ["description"] = "SQL statement to execute against CrateDB.",
                },
            },
            new List<string> { "query" },
            async args =>
            {
                var sql = args.TryGetValue("query", out var v) ? v?.ToString() ?? "" : "";
                try
                {
                    var result = await crate.RunSqlAsync(sql).ConfigureAwait(false);
                    return CrateDbClient.FormatResult(sql, result);
                }
                catch (Exception exc)
                {
                    return "Error running SQL: " + exc.Message;
                }
            });

        var allTools = new List<PanelTools.PanelTool>(panelTools) { querySql };
        var byName = allTools.ToDictionary(t => t.Name, t => t);

        var prompt = ChoosePrompt();
        await RunAgentAsync(cfg.AnthropicApiKey, allTools, byName, prompt).ConfigureAwait(false);
        return 0;
    }

    // -------------------------- agent loop --------------------------

    private static async Task RunAgentAsync(
        string apiKey,
        List<PanelTools.PanelTool> tools,
        Dictionary<string, PanelTools.PanelTool> byName,
        string userPrompt)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(5) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("x-api-key", apiKey);
        http.DefaultRequestHeaders.TryAddWithoutValidation("anthropic-version", AnthropicVersion);

        var sdkTools = new JsonArray();
        foreach (var t in tools)
        {
            sdkTools.Add(new JsonObject
            {
                ["name"] = t.Name,
                ["description"] = t.Description,
                ["input_schema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = JsonSerializer.SerializeToNode(t.InputSchemaProperties),
                    ["required"] = JsonSerializer.SerializeToNode(t.Required),
                    ["additionalProperties"] = false,
                },
            });
        }

        var messages = new JsonArray
        {
            new JsonObject
            {
                ["role"] = "user",
                ["content"] = userPrompt,
            },
        };

        for (var turn = 0; turn < MaxTurns; turn++)
        {
            var requestBody = new JsonObject
            {
                ["model"] = Model,
                ["max_tokens"] = MaxTokens,
                ["system"] = SystemPrompt,
                ["tools"] = sdkTools.DeepClone(),
                ["messages"] = messages.DeepClone(),
            };

            using var req = new HttpRequestMessage(HttpMethod.Post, AnthropicEndpoint)
            {
                Content = new StringContent(requestBody.ToJsonString(), Encoding.UTF8, "application/json"),
            };

            using var resp = await http.SendAsync(req).ConfigureAwait(false);
            var bodyText = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode)
            {
                Console.Error.WriteLine($"Anthropic API error {(int)resp.StatusCode}: {bodyText}");
                return;
            }

            var responseObj = JsonNode.Parse(bodyText) as JsonObject
                ?? throw new InvalidOperationException("Anthropic response is not a JSON object");
            var contentBlocks = responseObj["content"] as JsonArray ?? new JsonArray();

            var assistantBlocks = new JsonArray();
            var toolUses = new List<JsonObject>();

            foreach (var blockNode in contentBlocks)
            {
                if (blockNode is not JsonObject block) continue;
                var type = block["type"]?.GetValue<string>();

                if (type == "text")
                {
                    var text = block["text"]?.GetValue<string>() ?? "";
                    Console.WriteLine(text);
                    assistantBlocks.Add(new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = text,
                    });
                }
                else if (type == "tool_use")
                {
                    var name = block["name"]?.GetValue<string>() ?? "";
                    var input = block["input"];
                    var inputJson = input?.ToJsonString() ?? "{}";
                    var preview = inputJson.Length > 200 ? inputJson[..200] + "…" : inputJson;
                    Console.WriteLine($"[tool] {name}({preview})");

                    toolUses.Add(block);
                    assistantBlocks.Add(new JsonObject
                    {
                        ["type"] = "tool_use",
                        ["id"] = block["id"]?.GetValue<string>(),
                        ["name"] = name,
                        ["input"] = input?.DeepClone(),
                    });
                }
            }

            if (toolUses.Count == 0)
            {
                Console.WriteLine("\n=== done ===");
                return;
            }

            messages.Add(new JsonObject
            {
                ["role"] = "assistant",
                ["content"] = assistantBlocks,
            });

            var resultBlocks = new JsonArray();
            foreach (var u in toolUses)
            {
                var name = u["name"]?.GetValue<string>() ?? "";
                var id = u["id"]?.GetValue<string>() ?? "";
                string resultText;
                if (!byName.TryGetValue(name, out var handler))
                {
                    resultText = "Unknown tool: " + name;
                }
                else
                {
                    var args = ExtractInput(u["input"]);
                    resultText = await handler.Handler(args).ConfigureAwait(false);
                }
                resultBlocks.Add(new JsonObject
                {
                    ["type"] = "tool_result",
                    ["tool_use_id"] = id,
                    ["content"] = resultText,
                });
            }
            messages.Add(new JsonObject
            {
                ["role"] = "user",
                ["content"] = resultBlocks,
            });
        }

        Console.WriteLine("\n=== max_turns reached, stopping ===");
    }

    /// <summary>Convert the tool input JSON node into a plain dictionary.</summary>
    private static Dictionary<string, object?> ExtractInput(JsonNode? input)
    {
        var result = new Dictionary<string, object?>();
        if (input is not JsonObject obj) return result;
        foreach (var kv in obj)
        {
            result[kv.Key] = ConvertNode(kv.Value);
        }
        return result;
    }

    private static object? ConvertNode(JsonNode? node)
    {
        if (node == null) return null;
        if (node is JsonValue v)
        {
            if (v.TryGetValue<string>(out var s)) return s;
            if (v.TryGetValue<bool>(out var b)) return b;
            if (v.TryGetValue<long>(out var l)) return l;
            if (v.TryGetValue<double>(out var d)) return d;
            return v.ToString();
        }
        if (node is JsonArray a) return a.Select(ConvertNode).ToList();
        if (node is JsonObject o)
        {
            var dict = new Dictionary<string, object?>();
            foreach (var kv in o) dict[kv.Key] = ConvertNode(kv.Value);
            return dict;
        }
        return node.ToString();
    }

    // -------------------------- menu --------------------------

    private static string ChoosePrompt()
    {
        Console.WriteLine("\nSelect a question:");
        for (var i = 0; i < CannedPrompts.Count; i++)
        {
            Console.WriteLine($"  {i + 1}. {CannedPrompts[i].Title}");
        }
        Console.WriteLine($"  {CannedPrompts.Count + 1}. Enter your own question");

        Console.Write($"\nChoice [1-{CannedPrompts.Count + 1}]: ");
        var raw = Console.ReadLine() ?? throw new InvalidOperationException("no input");
        if (!int.TryParse(raw.Trim(), out var choice))
        {
            throw new InvalidOperationException("Invalid choice: " + raw);
        }
        if (choice >= 1 && choice <= CannedPrompts.Count)
        {
            var prompt = CannedPrompts[choice - 1].Prompt;
            Console.WriteLine("\n> " + prompt);
            return prompt;
        }
        if (choice == CannedPrompts.Count + 1)
        {
            Console.Write("Your question: ");
            var q = Console.ReadLine();
            if (string.IsNullOrWhiteSpace(q))
            {
                throw new InvalidOperationException("No prompt provided.");
            }
            return q.Trim();
        }
        throw new InvalidOperationException("Choice out of range: " + choice);
    }

    // -------------------------- CLI parsing --------------------------

    private sealed class Config
    {
        public string CratedbUrl { get; }
        public string AnthropicApiKey { get; }

        private Config(string cratedbUrl, string anthropicApiKey)
        {
            CratedbUrl = cratedbUrl;
            AnthropicApiKey = anthropicApiKey;
        }

        public static Config Parse(string[] argv)
        {
            string? cratedbUrl = null;
            string? host = null;
            var port = "4200";
            string? user = null;
            string? password = null;
            var scheme = "http";
            string? apiKey = null;

            for (var i = 0; i < argv.Length; i++)
            {
                switch (argv[i])
                {
                    case "--cratedb-url":      cratedbUrl = argv[++i]; break;
                    case "--cratedb-host":     host = argv[++i]; break;
                    case "--cratedb-port":     port = argv[++i]; break;
                    case "--cratedb-user":     user = argv[++i]; break;
                    case "--cratedb-password": password = argv[++i]; break;
                    case "--cratedb-scheme":   scheme = argv[++i]; break;
                    case "--anthropic-api-key": apiKey = argv[++i]; break;
                    case "--help":
                    case "-h":
                        PrintUsageAndExit(0);
                        break;
                    default:
                        Console.Error.WriteLine("Unknown argument: " + argv[i]);
                        PrintUsageAndExit(1);
                        break;
                }
            }

            cratedbUrl ??= Environment.GetEnvironmentVariable("CRATEDB_CLUSTER_URL");
            host ??= Environment.GetEnvironmentVariable("CRATEDB_HOST");
            var envPort = Environment.GetEnvironmentVariable("CRATEDB_PORT");
            if (!string.IsNullOrEmpty(envPort)) port = envPort;
            user ??= Environment.GetEnvironmentVariable("CRATEDB_USER");
            password ??= Environment.GetEnvironmentVariable("CRATEDB_PASSWORD");
            var envScheme = Environment.GetEnvironmentVariable("CRATEDB_SCHEME");
            if (!string.IsNullOrEmpty(envScheme)) scheme = envScheme;
            apiKey ??= Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY");

            var missing = new List<string>();
            string? resolvedUrl = null;

            if (!string.IsNullOrEmpty(cratedbUrl))
            {
                var u = new Uri(cratedbUrl);
                var userInfo = u.UserInfo;
                if (string.IsNullOrEmpty(userInfo) || !userInfo.Contains(':'))
                {
                    missing.Add("CrateDB URL must include user:password@host, e.g. http://user:pw@host:4200/");
                }
                else
                {
                    resolvedUrl = cratedbUrl;
                }
            }
            else
            {
                if (string.IsNullOrEmpty(host))
                    missing.Add("CrateDB host (--cratedb-host or CRATEDB_HOST), or --cratedb-url");
                if (string.IsNullOrEmpty(user))
                    missing.Add("CrateDB user (--cratedb-user or CRATEDB_USER)");
                if (string.IsNullOrEmpty(password))
                    missing.Add("CrateDB password (--cratedb-password or CRATEDB_PASSWORD)");
                if (missing.Count == 0)
                {
                    var encUser = Uri.EscapeDataString(user!);
                    var encPw = Uri.EscapeDataString(password!);
                    resolvedUrl = $"{scheme}://{encUser}:{encPw}@{host}:{port}/";
                }
            }
            if (string.IsNullOrEmpty(apiKey))
            {
                missing.Add("Anthropic API key (--anthropic-api-key or ANTHROPIC_API_KEY)");
            }
            if (missing.Count > 0)
            {
                Console.Error.WriteLine("Missing required configuration:");
                foreach (var m in missing) Console.Error.WriteLine("  - " + m);
                Environment.Exit(1);
            }
            return new Config(resolvedUrl!, apiKey!);
        }
    }

    private static void PrintUsageAndExit(int code)
    {
        Console.WriteLine(
            "Usage: GermanWeather [options]\n"
            + "  --cratedb-url URL          Full CrateDB URL (must embed user:password@).\n"
            + "  --cratedb-host HOST        CrateDB host.\n"
            + "  --cratedb-port PORT        CrateDB port (default 4200).\n"
            + "  --cratedb-user USER        CrateDB username.\n"
            + "  --cratedb-password PWD     CrateDB password.\n"
            + "  --cratedb-scheme SCHEME    http or https (default http).\n"
            + "  --anthropic-api-key KEY    Anthropic API key.\n"
            + "\nAll flags fall back to matching CRATEDB_* / ANTHROPIC_API_KEY env vars.\n");
        Environment.Exit(code);
    }
}
