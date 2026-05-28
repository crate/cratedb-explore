// Licensed to Crate.io GmbH ("Crate") under one or more contributor
// license agreements.  See the NOTICE file distributed with this work for
// additional information regarding copyright ownership.  Crate licenses
// this file to you under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.  You may
// obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0

using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GermanWeather;

/// <summary>
/// Loads <c>grafana/german_weather_data.json</c> and turns every panel
/// with a <c>rawSql</c> target into a callable tool. Mirrors the Java
/// PanelTools: panel title -> slugified tool name; rawSql -> template;
/// Grafana template variables ($var, ${var}) and the $__timeFilter()
/// macro -> tool parameters with JSON-schema entries.
/// </summary>
public static class PanelTools
{
    /// <summary>$var or ${var}; capture the bare name.</summary>
    private static readonly Regex TemplateVar = new(@"\$\{?(\w+)\}?", RegexOptions.Compiled);

    /// <summary>$__timeFilter("column"); capture the column expression.</summary>
    private static readonly Regex TimeFilter = new(@"\$__timeFilter\(\s*([^)]+?)\s*\)", RegexOptions.Compiled);

    private sealed record ParamSpec(string ParamName, string JsonType, string Description);

    private static readonly Dictionary<string, ParamSpec> VarSpec = new()
    {
        ["region"] = new ParamSpec("region", "string",
            "Federal-state name (one of the 16 German Länder), or the literal "
            + "string 'ALL' for the whole country. There is no row named 'Germany'."),
        ["keyword"] = new ParamSpec("keyword", "string", "Full-text search term"),
        ["search_categories"] = new ParamSpec("search_categories", "string",
            "A single fulltext-indexed column on demo.german_regions to MATCH against. "
            + "Valid values: 'region_name', 'economics', 'transportation', "
            + "'introduced_species'. Pass exactly one column name — "
            + "comma-separated lists produce a SQL syntax error, and 'tourism' "
            + "is not fulltext-indexed."),
        ["latitude"] = new ParamSpec("latitude", "number", "Latitude of the location"),
        ["longitude"] = new ParamSpec("longitude", "number", "Longitude of the location"),
    };

    /// <summary>Immutable tool definition exposed to Claude.</summary>
    public sealed class PanelTool
    {
        public string Name { get; }
        public string Description { get; }
        /// <summary>JSON-schema object suitable for the Anthropic tool input_schema.properties.</summary>
        public Dictionary<string, object> InputSchemaProperties { get; }
        public List<string> Required { get; }
        public Func<Dictionary<string, object?>, Task<string>> Handler { get; }

        public PanelTool(
            string name,
            string description,
            Dictionary<string, object> inputSchemaProperties,
            List<string> required,
            Func<Dictionary<string, object?>, Task<string>> handler)
        {
            Name = name;
            Description = description;
            InputSchemaProperties = inputSchemaProperties;
            Required = required;
            Handler = handler;
        }
    }

    public static async Task<List<PanelTool>> LoadFromFileAsync(string dashboardPath, CrateDbClient crate)
    {
        var text = await File.ReadAllTextAsync(dashboardPath).ConfigureAwait(false);
        var root = JsonNode.Parse(text) as JsonObject ?? throw new InvalidOperationException("dashboard root is not a JSON object");
        var panels = root["panels"] as JsonArray ?? new JsonArray();

        var tools = new List<PanelTool>();
        var usedNames = new HashSet<string>();
        foreach (var el in panels)
        {
            if (el is not JsonObject panel) continue;
            if (panel["type"]?.GetValue<string>() == "row") continue;
            var tool = MakeTool(panel, crate, usedNames);
            if (tool != null) tools.Add(tool);
        }
        return tools;
    }

    private static PanelTool? MakeTool(JsonObject panel, CrateDbClient crate, HashSet<string> usedNames)
    {
        if (panel["targets"] is not JsonArray targets) return null;

        string? sqlTemplate = null;
        foreach (var t in targets)
        {
            if (t is JsonObject target && target["rawSql"] is JsonNode raw)
            {
                sqlTemplate = raw.GetValue<string>();
                break;
            }
        }
        if (sqlTemplate == null) return null;

        var title = panel["title"]?.GetValue<string>()
            ?? $"panel_{panel["id"]?.ToString() ?? "?"}";

        var name = Slugify(title);
        if (usedNames.Contains(name))
        {
            name = $"{name}_{panel["id"]?.ToString() ?? "x"}";
        }
        usedNames.Add(name);

        var (vars, usesTimeFilter) = ScanSql(sqlTemplate);

        var properties = new Dictionary<string, object>();
        var required = new List<string>();
        foreach (var v in vars)
        {
            if (!VarSpec.TryGetValue(v, out var spec)) continue;
            properties[spec.ParamName] = new Dictionary<string, object>
            {
                ["type"] = spec.JsonType,
                ["description"] = spec.Description,
            };
            required.Add(spec.ParamName);
        }
        if (usesTimeFilter)
        {
            properties["time_from"] = new Dictionary<string, object>
            {
                ["type"] = "string",
                ["description"] = "Start of the time window, ISO 8601 (e.g. 2025-12-01).",
            };
            properties["time_to"] = new Dictionary<string, object>
            {
                ["type"] = "string",
                ["description"] = "End of the time window, ISO 8601 (e.g. 2025-12-31).",
            };
            required.Add("time_from");
            required.Add("time_to");
        }

        var description = $"Grafana panel '{title}'. Runs the panel's SQL against CrateDB and returns the rows.";

        var capturedSql = sqlTemplate;
        var capturedTitle = title;
        async Task<string> Handler(Dictionary<string, object?> args)
        {
            try
            {
                var rendered = RenderSql(capturedSql, args);
                var result = await crate.RunSqlAsync(rendered).ConfigureAwait(false);
                return CrateDbClient.FormatResult(rendered, result);
            }
            catch (Exception exc)
            {
                return $"Error running panel '{capturedTitle}': {exc.Message}";
            }
        }

        return new PanelTool(name, description, properties, required, Handler);
    }

    /// <summary>Strip $/{} from template variable references, then snake_case + lowercase.</summary>
    internal static string Slugify(string title)
    {
        var s = TemplateVar.Replace(title, "$1");
        s = Regex.Replace(s, "[^A-Za-z0-9]+", "_");
        s = Regex.Replace(s, "^_+|_+$", "");
        s = s.ToLowerInvariant();
        return string.IsNullOrEmpty(s) ? "panel" : s;
    }

    internal static (List<string> Vars, bool UsesTimeFilter) ScanSql(string sql)
    {
        var seen = new HashSet<string>();
        var vars = new List<string>();
        foreach (Match m in TemplateVar.Matches(sql))
        {
            var name = m.Groups[1].Value;
            if (name.StartsWith("__")) continue;
            if (seen.Add(name)) vars.Add(name);
        }
        return (vars, sql.Contains("$__timeFilter"));
    }

    internal static string RenderSql(string sql, Dictionary<string, object?> args)
    {
        // Pass 1: $__timeFilter("col") -> (col >= 'time_from' AND col <= 'time_to')
        var rendered = TimeFilter.Replace(sql, m =>
        {
            var col = m.Groups[1].Value.Trim();
            if (!args.TryGetValue("time_from", out var tf) || tf == null ||
                !args.TryGetValue("time_to", out var tt) || tt == null)
            {
                throw new ArgumentException(
                    "time_from and time_to are required for panels that use $__timeFilter");
            }
            return $"({col} >= '{tf}' AND {col} <= '{tt}')";
        });

        // Pass 2: $var / ${var} -> arg value
        foreach (var (var, spec) in VarSpec)
        {
            if (!args.TryGetValue(spec.ParamName, out var value) || value == null) continue;
            var pat = new Regex(@"\$\{?" + Regex.Escape(var) + @"\}?");
            rendered = pat.Replace(rendered, value.ToString() ?? "");
        }
        return rendered;
    }

    /// <summary>
    /// Walk up from cwd looking for <c>grafana/german_weather_data.json</c>.
    /// </summary>
    public static string? FindDashboard()
    {
        var p = new DirectoryInfo(Directory.GetCurrentDirectory());
        for (var i = 0; i < 6 && p != null; i++)
        {
            var candidate = Path.Combine(p.FullName, "grafana", "german_weather_data.json");
            if (File.Exists(candidate)) return candidate;
            p = p.Parent;
        }
        return null;
    }
}
