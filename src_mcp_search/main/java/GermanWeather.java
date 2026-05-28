/*
 * Licensed to Crate.io GmbH ("Crate") under one or more contributor
 * license agreements.  See the NOTICE file distributed with this work for
 * additional information regarding copyright ownership.  Crate licenses
 * this file to you under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.  You may
 * obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 */

import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.core.JsonValue;
import com.anthropic.models.messages.ContentBlock;
import com.anthropic.models.messages.ContentBlockParam;
import com.anthropic.models.messages.Message;
import com.anthropic.models.messages.MessageCreateParams;
import com.anthropic.models.messages.Model;
import com.anthropic.models.messages.TextBlock;
import com.anthropic.models.messages.Tool;
import com.anthropic.models.messages.ToolResultBlockParam;
import com.anthropic.models.messages.ToolUnion;
import com.anthropic.models.messages.ToolUseBlock;
import com.anthropic.models.messages.ToolUseBlockParam;

import com.google.gson.Gson;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.URI;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;

/**
 * Java port of {@code german_weather.py} — ask Claude questions about
 * a CrateDB cluster that holds German weather data.
 *
 * <h2>Design</h2>
 *
 * <p>The Python version uses the official {@code claude-agent-sdk}
 * which bundles an MCP client; it talks to two MCP servers,
 * {@code cratedb-mcp} (subprocess) and an in-process {@code gw}
 * server built from the Grafana dashboard. The Java Anthropic SDK
 * does not currently expose an MCP client at parity, so we take a
 * simpler path:
 *
 * <ul>
 *   <li>A generic {@code query_sql} tool that POSTs SQL straight to
 *       CrateDB's HTTP {@code _sql} endpoint (replacing
 *       {@code cratedb-mcp}).</li>
 *   <li>One tool per Grafana panel, generated dynamically from
 *       {@code grafana/german_weather_data.json} — see
 *       {@link PanelTools}.</li>
 * </ul>
 *
 * <p>The agent loop is implemented manually below: call
 * {@code messages.create()}, walk the returned content blocks, and
 * for every {@link ToolUseBlock} append the assistant turn + a
 * follow-up user turn carrying the tool result. Loop until the
 * response contains no tool calls (or we hit the turn cap).
 */
public final class GermanWeather {

    private static final String MODEL = "claude-opus-4-7";
    private static final int MAX_TURNS = 30;
    private static final long MAX_TOKENS = 4096;

    /** Canned questions presented as menu options 1..N. */
    private static final List<String[]> CANNED_PROMPTS = List.of(
            new String[] {
                    "Latest measurement timestamp in the dataset",
                    "What is the latest measurement timestamp in demo.climate_data? "
                            + "Use time_from=2025-01-01 and time_to=2025-12-31."
            },
            new String[] {
                    "Min / Avg / Max temperature across all of Germany (Dec 2025)",
                    "Show the min, average, and max temperature across all of Germany "
                            + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."
            },
            new String[] {
                    "Min / Avg / Max air pressure across all of Germany (Dec 2025)",
                    "Show the min, average, and max air pressure across all of Germany "
                            + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."
            },
            new String[] {
                    "Min / Avg / Max wind speed across all of Germany (Dec 2025)",
                    "Show the min, average, and max wind speed across all of Germany "
                            + "for December 2025 (time_from=2025-12-01, time_to=2025-12-31)."
            },
            new String[] {
                    "Temperature snapshot across Germany at the most recent reading",
                    "Show the temperature snapshot across all of Germany at the most "
                            + "recent measurement time in December 2025 (time_from=2025-12-01, "
                            + "time_to=2025-12-31)."
            },
            new String[] {
                    "Pressure snapshot across Germany at the most recent reading",
                    "Show the pressure snapshot across all of Germany at the most recent "
                            + "measurement time in December 2025 (time_from=2025-12-01, "
                            + "time_to=2025-12-31)."
            },
            new String[] {
                    "Wind speed & direction snapshot across Germany",
                    "Show the wind speed and direction snapshot across all of Germany at "
                            + "the most recent measurement time in December 2025 "
                            + "(time_from=2025-12-01, time_to=2025-12-31)."
            },
            new String[] {
                    "Coldest place in Germany on 2025-12-31",
                    "Where was the coldest place in Germany at the most recent "
                            + "measurement on 2025-12-31? Report town/coordinates and temperature. "
                            + "Restrict candidate points to those that fall inside a German "
                            + "federal-state polygon by joining demo.climate_data to "
                            + "demo.german_regions with WITHIN(c.geo_location, r.geo_coords) so "
                            + "border points in Austria/Switzerland are excluded."
            });

    private static final String SYSTEM_PROMPT =
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

    private GermanWeather() {}

    public static void main(String[] argv) throws Exception {
        Config cfg = Config.parse(argv);
        CrateDbClient crate = new CrateDbClient(cfg.cratedbUrl);

        Optional<Path> dashboard = PanelTools.findDashboard();
        if (dashboard.isEmpty()) {
            System.err.println("Could not locate grafana/german_weather_data.json relative to cwd");
            System.exit(1);
        }
        List<PanelTools.PanelTool> panelTools = PanelTools.loadFromFile(dashboard.get(), crate);
        System.out.println("[panels] registered " + panelTools.size() + " panel tool(s):");
        for (PanelTools.PanelTool t : panelTools) {
            System.out.println("          - " + t.name);
        }

        // Add the generic query_sql tool that mirrors what cratedb-mcp would
        // expose for arbitrary SQL.
        PanelTools.PanelTool querySql = new PanelTools.PanelTool(
                "query_sql",
                "Execute an arbitrary SQL statement against the CrateDB cluster "
                        + "and return columns and rows. Use this when no panel tool fits.",
                Map.of("query", Map.of(
                        "type", "string",
                        "description", "SQL statement to execute against CrateDB.")),
                List.of("query"),
                args -> {
                    String sql = String.valueOf(args.get("query"));
                    try {
                        return CrateDbClient.formatResult(sql, crate.runSql(sql));
                    } catch (Exception exc) {
                        return "Error running SQL: " + exc.getMessage();
                    }
                });

        List<PanelTools.PanelTool> allTools = new ArrayList<>(panelTools);
        allTools.add(querySql);

        // Build a lookup table for tool dispatch.
        Map<String, PanelTools.PanelTool> byName = new LinkedHashMap<>();
        for (PanelTools.PanelTool t : allTools) {
            byName.put(t.name, t);
        }

        String prompt = choosePrompt();
        runAgent(cfg.anthropicApiKey, allTools, byName, prompt);
    }

    // -------------------------- agent loop --------------------------

    private static final Gson GSON = new Gson();

    private static void runAgent(String apiKey,
                                 List<PanelTools.PanelTool> tools,
                                 Map<String, PanelTools.PanelTool> byName,
                                 String userPrompt) {
        AnthropicClient client = AnthropicOkHttpClient.builder()
                .apiKey(apiKey)
                .build();

        // Translate our PanelTool -> Anthropic Tool definitions.
        // The Java SDK uses ToolUnion to allow either a user-defined Tool or
        // one of the built-in tool types (bash, computer-use, etc.) in the
        // same list. We always wrap as ofTool.
        List<ToolUnion> sdkTools = new ArrayList<>();
        for (PanelTools.PanelTool t : tools) {
            Tool.InputSchema schema = Tool.InputSchema.builder()
                    .properties(JsonValue.from(t.inputSchemaProperties))
                    .putAdditionalProperty("required", JsonValue.from(t.required))
                    .putAdditionalProperty("additionalProperties", JsonValue.from(false))
                    .build();
            sdkTools.add(ToolUnion.ofTool(Tool.builder()
                    .name(t.name)
                    .description(t.description)
                    .inputSchema(schema)
                    .build()));
        }

        MessageCreateParams.Builder paramsBuilder = MessageCreateParams.builder()
                .model(MODEL)
                .maxTokens(MAX_TOKENS)
                .system(SYSTEM_PROMPT)
                .tools(sdkTools)
                .addUserMessage(userPrompt);

        for (int turn = 0; turn < MAX_TURNS; turn++) {
            Message response = client.messages().create(paramsBuilder.build());

            List<ContentBlock> blocks = response.content();
            List<ContentBlockParam> assistantTurn = new ArrayList<>();
            List<ToolUseBlock> toolUses = new ArrayList<>();

            for (ContentBlock block : blocks) {
                Optional<TextBlock> text = block.text();
                Optional<ToolUseBlock> use = block.toolUse();
                if (text.isPresent()) {
                    System.out.println(text.get().text());
                    assistantTurn.add(ContentBlockParam.ofText(
                            com.anthropic.models.messages.TextBlockParam.builder()
                                    .text(text.get().text())
                                    .build()));
                } else if (use.isPresent()) {
                    ToolUseBlock u = use.get();
                    String inputJson;
                    try {
                        inputJson = new com.fasterxml.jackson.databind.ObjectMapper()
                                .writeValueAsString(u._input());
                    } catch (Exception ex) {
                        inputJson = u._input().toString();
                    }
                    if (inputJson.length() > 200) {
                        inputJson = inputJson.substring(0, 200) + "…";
                    }
                    System.out.println("[tool] " + u.name() + "(" + inputJson + ")");
                    toolUses.add(u);
                    assistantTurn.add(ContentBlockParam.ofToolUse(
                            ToolUseBlockParam.builder()
                                    .name(u.name())
                                    .id(u.id())
                                    .input(u._input())
                                    .build()));
                }
            }

            // If no tool calls, we're done.
            if (toolUses.isEmpty()) {
                System.out.println("\n=== done ===");
                return;
            }

            // Append assistant turn and a user turn carrying tool results.
            paramsBuilder.addAssistantMessageOfBlockParams(assistantTurn);

            List<ContentBlockParam> resultBlocks = new ArrayList<>();
            for (ToolUseBlock u : toolUses) {
                PanelTools.PanelTool handler = byName.get(u.name());
                String resultText;
                if (handler == null) {
                    resultText = "Unknown tool: " + u.name();
                } else {
                    Map<String, Object> args = extractInput(u);
                    resultText = handler.handler.apply(args);
                }
                resultBlocks.add(ContentBlockParam.ofToolResult(
                        ToolResultBlockParam.builder()
                                .toolUseId(u.id())
                                .content(resultText)
                                .build()));
            }
            paramsBuilder.addUserMessageOfBlockParams(resultBlocks);
        }

        System.out.println("\n=== max_turns reached, stopping ===");
    }

    /**
     * Read the tool input as a plain {@code Map<String, Object>}.
     *
     * <p>{@code ToolUseBlock._input()} returns a {@link JsonValue} that
     * wraps the parsed JSON. Calling {@code .toString()} on it produces
     * a Map-style {@code {k=v}} repr, NOT JSON, so we can't simply
     * stringify-then-Gson-parse. Instead we walk the JsonValue tree
     * and convert each node.
     */
    private static Map<String, Object> extractInput(ToolUseBlock use) {
        Object converted = convertJsonValue(use._input());
        if (converted instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> map = (Map<String, Object>) converted;
            return map;
        }
        return Map.of();
    }

    @SuppressWarnings("unchecked")
    private static Object convertJsonValue(JsonValue value) {
        // JsonValue exposes typed accessors; the cleanest cross-version
        // path is to serialize to a JSON string via Jackson (which the
        // SDK already uses internally) and parse with Gson into plain
        // Java types. Going through the SDK's serializer guarantees we
        // get real JSON, not the Map-toString format.
        String json;
        try {
            json = new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(value);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize tool input: " + e.getMessage(), e);
        }
        if (json == null || json.isBlank() || "null".equals(json)) {
            return Map.of();
        }
        Object parsed = GSON.fromJson(json, Object.class);
        return parsed != null ? parsed : Map.of();
    }

    // -------------------------- menu --------------------------

    private static String choosePrompt() throws Exception {
        System.out.println("\nSelect a question:");
        for (int i = 0; i < CANNED_PROMPTS.size(); i++) {
            System.out.println("  " + (i + 1) + ". " + CANNED_PROMPTS.get(i)[0]);
        }
        System.out.println("  " + (CANNED_PROMPTS.size() + 1) + ". Enter your own question");

        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
        System.out.print("\nChoice [1-" + (CANNED_PROMPTS.size() + 1) + "]: ");
        String raw = reader.readLine();
        if (raw == null) {
            throw new RuntimeException("no input");
        }
        int choice;
        try {
            choice = Integer.parseInt(raw.trim());
        } catch (NumberFormatException nfe) {
            throw new RuntimeException("Invalid choice: " + raw);
        }
        if (choice >= 1 && choice <= CANNED_PROMPTS.size()) {
            String prompt = CANNED_PROMPTS.get(choice - 1)[1];
            System.out.println("\n> " + prompt);
            return prompt;
        }
        if (choice == CANNED_PROMPTS.size() + 1) {
            System.out.print("Your question: ");
            String q = reader.readLine();
            if (q == null || q.isBlank()) {
                throw new RuntimeException("No prompt provided.");
            }
            return q.trim();
        }
        throw new RuntimeException("Choice out of range: " + choice);
    }

    // -------------------------- CLI parsing --------------------------

    private static final class Config {
        final String cratedbUrl;
        final String anthropicApiKey;

        Config(String cratedbUrl, String anthropicApiKey) {
            this.cratedbUrl = cratedbUrl;
            this.anthropicApiKey = anthropicApiKey;
        }

        static Config parse(String[] argv) {
            String cratedbUrl = null;
            String host = null;
            String port = "4200";
            String user = null;
            String password = null;
            String scheme = "http";
            String apiKey = null;

            for (int i = 0; i < argv.length; i++) {
                String a = argv[i];
                switch (a) {
                    case "--cratedb-url": cratedbUrl = argv[++i]; break;
                    case "--cratedb-host": host = argv[++i]; break;
                    case "--cratedb-port": port = argv[++i]; break;
                    case "--cratedb-user": user = argv[++i]; break;
                    case "--cratedb-password": password = argv[++i]; break;
                    case "--cratedb-scheme": scheme = argv[++i]; break;
                    case "--anthropic-api-key": apiKey = argv[++i]; break;
                    case "--help":
                    case "-h":
                        printUsageAndExit(0);
                        break;
                    default:
                        System.err.println("Unknown argument: " + a);
                        printUsageAndExit(1);
                }
            }

            // Env-var fallbacks.
            if (cratedbUrl == null) cratedbUrl = System.getenv("CRATEDB_CLUSTER_URL");
            if (host == null) host = System.getenv("CRATEDB_HOST");
            String envPort = System.getenv("CRATEDB_PORT"); if (envPort != null) port = envPort;
            if (user == null) user = System.getenv("CRATEDB_USER");
            if (password == null) password = System.getenv("CRATEDB_PASSWORD");
            String envScheme = System.getenv("CRATEDB_SCHEME"); if (envScheme != null) scheme = envScheme;
            if (apiKey == null) apiKey = System.getenv("ANTHROPIC_API_KEY");

            List<String> missing = new ArrayList<>();
            String resolvedUrl = null;
            if (cratedbUrl != null && !cratedbUrl.isEmpty()) {
                URI u = URI.create(cratedbUrl);
                String userInfo = u.getUserInfo();
                if (userInfo == null || !userInfo.contains(":")) {
                    missing.add("CrateDB URL must include user:password@host, e.g. http://user:pw@host:4200/");
                } else {
                    resolvedUrl = cratedbUrl;
                }
            } else {
                if (host == null || host.isEmpty()) {
                    missing.add("CrateDB host (--cratedb-host or CRATEDB_HOST), or --cratedb-url");
                }
                if (user == null || user.isEmpty()) {
                    missing.add("CrateDB user (--cratedb-user or CRATEDB_USER)");
                }
                if (password == null || password.isEmpty()) {
                    missing.add("CrateDB password (--cratedb-password or CRATEDB_PASSWORD)");
                }
                if (missing.isEmpty()) {
                    String encUser = java.net.URLEncoder.encode(user, java.nio.charset.StandardCharsets.UTF_8);
                    String encPw = java.net.URLEncoder.encode(password, java.nio.charset.StandardCharsets.UTF_8);
                    resolvedUrl = scheme + "://" + encUser + ":" + encPw + "@" + host + ":" + port + "/";
                }
            }
            if (apiKey == null || apiKey.isEmpty()) {
                missing.add("Anthropic API key (--anthropic-api-key or ANTHROPIC_API_KEY)");
            }
            if (!missing.isEmpty()) {
                System.err.println("Missing required configuration:");
                for (String m : missing) {
                    System.err.println("  - " + m);
                }
                System.exit(1);
            }
            return new Config(Objects.requireNonNull(resolvedUrl), apiKey);
        }
    }

    private static void printUsageAndExit(int code) {
        System.out.println("Usage: GermanWeather [options]\n"
                + "  --cratedb-url URL          Full CrateDB URL (must embed user:password@).\n"
                + "  --cratedb-host HOST        CrateDB host.\n"
                + "  --cratedb-port PORT        CrateDB port (default 4200).\n"
                + "  --cratedb-user USER        CrateDB username.\n"
                + "  --cratedb-password PWD     CrateDB password.\n"
                + "  --cratedb-scheme SCHEME    http or https (default http).\n"
                + "  --anthropic-api-key KEY    Anthropic API key.\n"
                + "\nAll flags fall back to matching CRATEDB_* / ANTHROPIC_API_KEY env vars.\n");
        System.exit(code);
    }
}
