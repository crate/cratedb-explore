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
/// Thin client for CrateDB's HTTP <c>_sql</c> endpoint. POST
/// <c>{"stmt": "..."}</c> and get back JSON with <c>cols</c> and
/// <c>rows</c>. HTTP requests are stateless, so the persistent
/// equivalent of <c>SET search_path TO demo</c> is the
/// <c>Default-Schema</c> request header, set on every request.
/// </summary>
public sealed class CrateDbClient
{
    private readonly HttpClient _http;
    private readonly Uri _endpoint;
    private readonly string? _basicAuthHeader;

    public CrateDbClient(string cratedbUrl)
    {
        ArgumentNullException.ThrowIfNull(cratedbUrl);
        var parsed = new Uri(cratedbUrl);

        var port = parsed.IsDefaultPort ? 4200 : parsed.Port;
        var scheme = string.IsNullOrEmpty(parsed.Scheme) ? "http" : parsed.Scheme;
        _endpoint = new Uri($"{scheme}://{parsed.Host}:{port}/_sql");

        var userInfo = parsed.UserInfo;
        if (!string.IsNullOrEmpty(userInfo))
        {
            // Uri percent-decodes user:password into UserInfo; re-encode bytes as Basic auth.
            var decoded = Uri.UnescapeDataString(userInfo);
            _basicAuthHeader = Convert.ToBase64String(Encoding.UTF8.GetBytes(decoded));
        }

        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
    }

    /// <summary>
    /// Execute a single SQL statement. Returns the parsed JSON body on
    /// success or error — callers surface either to Claude as tool output.
    /// </summary>
    public async Task<JsonObject> RunSqlAsync(string sql)
    {
        var body = new JsonObject { ["stmt"] = sql }.ToJsonString();
        using var req = new HttpRequestMessage(HttpMethod.Post, _endpoint)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
        req.Headers.TryAddWithoutValidation("Default-Schema", "demo");
        if (_basicAuthHeader != null)
        {
            req.Headers.Authorization = new AuthenticationHeaderValue("Basic", _basicAuthHeader);
        }

        using var resp = await _http.SendAsync(req).ConfigureAwait(false);
        var text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
        var parsed = JsonNode.Parse(text);
        return parsed as JsonObject ?? new JsonObject();
    }

    /// <summary>
    /// Render the JSON response into a human-readable text block (SQL,
    /// column names, rows, truncation note) for use as a tool result.
    /// </summary>
    public static string FormatResult(string renderedSql, JsonObject result)
    {
        if (result.ContainsKey("error"))
        {
            return $"SQL: {renderedSql}\n\nERROR: {result["error"]}";
        }

        var cols = result["cols"] as JsonArray ?? new JsonArray();
        var rows = result["rows"] as JsonArray ?? new JsonArray();

        var sb = new StringBuilder();
        sb.Append("SQL:\n").Append(renderedSql).Append("\n\n");
        sb.Append("Columns: ").Append(cols.ToJsonString()).Append('\n');
        sb.Append("Row count: ").Append(rows.Count).Append('\n');

        var limit = Math.Min(rows.Count, 50);
        for (var i = 0; i < limit; i++)
        {
            sb.Append("  ").Append(rows[i]?.ToJsonString() ?? "null").Append('\n');
        }
        if (rows.Count > limit)
        {
            sb.Append("  ... (").Append(rows.Count - limit).Append(" more rows omitted)\n");
        }
        return sb.ToString();
    }
}
