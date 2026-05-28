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

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Loads {@code grafana/german_weather_data.json} and turns every panel
 * with a {@code rawSql} target into a callable tool.
 *
 * <p>The Java port mirrors {@code german_weather_panels.py}: a panel's
 * title becomes a slugified tool name; its rawSql becomes the
 * template that gets substituted at call-time; Grafana template
 * variables ($var, ${var}) and the $__timeFilter() macro become tool
 * parameters with explicit JSON schema entries.
 */
public final class PanelTools {

    /** $var or ${var}, captures the name. */
    private static final Pattern TEMPLATE_VAR = Pattern.compile("\\$\\{?(\\w+)}?");

    /** $__timeFilter("column"), captures the column expression. */
    private static final Pattern TIME_FILTER = Pattern.compile("\\$__timeFilter\\(\\s*([^)]+?)\\s*\\)");

    /** Grafana template variable -> (param name, JSON-schema type, description). */
    private static final Map<String, ParamSpec> VAR_SPEC = new HashMap<>();
    static {
        VAR_SPEC.put("region", new ParamSpec("region", "string",
                "Federal-state name (one of the 16 German Länder), or the literal "
                        + "string 'ALL' for the whole country. There is no row named 'Germany'."));
        VAR_SPEC.put("keyword", new ParamSpec("keyword", "string", "Full-text search term"));
        VAR_SPEC.put("search_categories", new ParamSpec("search_categories", "string",
                "A single fulltext-indexed column on demo.german_regions to MATCH against. "
                        + "Valid values: 'region_name', 'economics', 'transportation', "
                        + "'introduced_species'. Pass exactly one column name — "
                        + "comma-separated lists produce a SQL syntax error, and 'tourism' "
                        + "is not fulltext-indexed."));
        VAR_SPEC.put("latitude", new ParamSpec("latitude", "number", "Latitude of the location"));
        VAR_SPEC.put("longitude", new ParamSpec("longitude", "number", "Longitude of the location"));
    }

    private PanelTools() {}

    /** Immutable tool definition exposed to Claude. */
    public static final class PanelTool {
        public final String name;
        public final String description;
        /** JSON-schema object suitable for {@code Tool.InputSchema.properties(...)}. */
        public final Map<String, Object> inputSchemaProperties;
        public final List<String> required;
        public final Function<Map<String, Object>, String> handler;

        PanelTool(String name,
                  String description,
                  Map<String, Object> inputSchemaProperties,
                  List<String> required,
                  Function<Map<String, Object>, String> handler) {
            this.name = name;
            this.description = description;
            this.inputSchemaProperties = inputSchemaProperties;
            this.required = required;
            this.handler = handler;
        }
    }

    private record ParamSpec(String paramName, String jsonType, String description) {}

    /**
     * Load the dashboard JSON and build one tool per panel-with-rawSql.
     */
    public static List<PanelTool> loadFromFile(Path dashboardPath, CrateDbClient crate) throws IOException {
        String text = Files.readString(dashboardPath);
        JsonObject root = JsonParser.parseString(text).getAsJsonObject();
        JsonArray panels = root.has("panels") ? root.getAsJsonArray("panels") : new JsonArray();

        List<PanelTool> tools = new ArrayList<>();
        Set<String> usedNames = new HashSet<>();
        for (JsonElement el : panels) {
            JsonObject panel = el.getAsJsonObject();
            if (panel.has("type") && "row".equals(panel.get("type").getAsString())) {
                continue; // row panels are section headers, no queries
            }
            PanelTool tool = makeTool(panel, crate, usedNames);
            if (tool != null) {
                tools.add(tool);
            }
        }
        return tools;
    }

    private static PanelTool makeTool(JsonObject panel, CrateDbClient crate, Set<String> usedNames) {
        if (!panel.has("targets")) {
            return null;
        }
        String sqlTemplate = null;
        for (JsonElement t : panel.getAsJsonArray("targets")) {
            JsonObject target = t.getAsJsonObject();
            if (target.has("rawSql")) {
                sqlTemplate = target.get("rawSql").getAsString();
                break;
            }
        }
        if (sqlTemplate == null) {
            return null;
        }

        String title = panel.has("title") ? panel.get("title").getAsString()
                                          : "panel_" + (panel.has("id") ? panel.get("id").getAsString() : "?");

        String name = slugify(title);
        if (usedNames.contains(name)) {
            name = name + "_" + (panel.has("id") ? panel.get("id").getAsString() : "x");
        }
        usedNames.add(name);

        Scan scan = scanSql(sqlTemplate);

        // Build properties + required list in stable (insertion) order.
        Map<String, Object> properties = new LinkedHashMap<>();
        List<String> required = new ArrayList<>();
        for (String var : scan.vars) {
            ParamSpec spec = VAR_SPEC.get(var);
            if (spec == null) {
                continue;
            }
            properties.put(spec.paramName, Map.of(
                    "type", spec.jsonType,
                    "description", spec.description));
            required.add(spec.paramName);
        }
        if (scan.usesTimeFilter) {
            properties.put("time_from", Map.of(
                    "type", "string",
                    "description", "Start of the time window, ISO 8601 (e.g. 2025-12-01)."));
            properties.put("time_to", Map.of(
                    "type", "string",
                    "description", "End of the time window, ISO 8601 (e.g. 2025-12-31)."));
            required.add("time_from");
            required.add("time_to");
        }

        String description = "Grafana panel '" + title + "'. Runs the panel's SQL against CrateDB and returns the rows.";

        // Capture in final locals for the lambda.
        final String capturedSql = sqlTemplate;
        final String capturedTitle = title;
        Function<Map<String, Object>, String> handler = args -> {
            try {
                String rendered = renderSql(capturedSql, args);
                JsonObject result = crate.runSql(rendered);
                return CrateDbClient.formatResult(rendered, result);
            } catch (Exception exc) {
                return "Error running panel '" + capturedTitle + "': " + exc.getMessage();
            }
        };

        return new PanelTool(name, description, properties, required, handler);
    }

    // ---- Slug / scan / render helpers ----

    /** Strip $/{} from template variable references then snake_case + lowercase. */
    static String slugify(String title) {
        String s = TEMPLATE_VAR.matcher(title).replaceAll("$1");
        s = s.replaceAll("[^A-Za-z0-9]+", "_").replaceAll("^_+|_+$", "").toLowerCase();
        return s.isEmpty() ? "panel" : s;
    }

    private static final class Scan {
        final Set<String> vars;
        final boolean usesTimeFilter;

        Scan(Set<String> vars, boolean usesTimeFilter) {
            this.vars = vars;
            this.usesTimeFilter = usesTimeFilter;
        }
    }

    /** Inspect SQL and report which template variables it uses + whether $__timeFilter appears. */
    static Scan scanSql(String sql) {
        Set<String> vars = new java.util.LinkedHashSet<>();
        Matcher m = TEMPLATE_VAR.matcher(sql);
        while (m.find()) {
            String name = m.group(1);
            if (!name.startsWith("__")) {
                vars.add(name);
            }
        }
        return new Scan(vars, sql.contains("$__timeFilter"));
    }

    /** Substitute caller arguments into the panel SQL template. */
    static String renderSql(String sql, Map<String, Object> args) {
        // Pass 1: $__timeFilter("col") -> (col >= 'time_from' AND col <= 'time_to')
        StringBuilder out = new StringBuilder();
        Matcher m = TIME_FILTER.matcher(sql);
        int idx = 0;
        while (m.find()) {
            out.append(sql, idx, m.start());
            String col = m.group(1).trim();
            Object tf = args.get("time_from");
            Object tt = args.get("time_to");
            if (tf == null || tt == null) {
                throw new IllegalArgumentException(
                        "time_from and time_to are required for panels that use $__timeFilter");
            }
            out.append('(').append(col).append(" >= '").append(tf)
               .append("' AND ").append(col).append(" <= '").append(tt).append("')");
            idx = m.end();
        }
        out.append(sql, idx, sql.length());
        String rendered = out.toString();

        // Pass 2: $var / ${var} -> arg value
        for (Map.Entry<String, ParamSpec> e : VAR_SPEC.entrySet()) {
            String var = e.getKey();
            Object value = args.get(e.getValue().paramName);
            if (value == null) {
                continue;
            }
            Pattern pat = Pattern.compile("\\$\\{?" + Pattern.quote(var) + "}?");
            rendered = pat.matcher(rendered).replaceAll(Matcher.quoteReplacement(value.toString()));
        }
        return rendered;
    }

    /**
     * Optional helper: locate the dashboard JSON relative to the
     * Java module's working directory. The Maven pom puts the source
     * dir at {@code src_mcp_search/main/java/}, so the dashboard sits
     * three levels up.
     */
    public static Path defaultDashboardPath() {
        Path here = Path.of("").toAbsolutePath();
        return here.resolve(Path.of("..", "..", "..", "grafana", "german_weather_data.json")).normalize();
    }

    public static Optional<Path> findDashboard() {
        // Walk up looking for a 'grafana/german_weather_data.json' until we hit the repo root.
        Path p = Path.of("").toAbsolutePath();
        for (int i = 0; i < 6; i++) {
            Path candidate = p.resolve("grafana").resolve("german_weather_data.json");
            if (Files.exists(candidate)) {
                return Optional.of(candidate);
            }
            Path parent = p.getParent();
            if (parent == null) {
                break;
            }
            p = parent;
        }
        return Optional.empty();
    }
}
