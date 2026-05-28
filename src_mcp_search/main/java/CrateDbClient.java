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
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.Objects;

/**
 * Thin client for CrateDB's HTTP {@code _sql} endpoint.
 *
 * <p>CrateDB exposes a JSON-over-HTTP query API: POST a body of
 * {@code {"stmt": "..."}} to {@code /_sql} and get back JSON with
 * {@code cols} (column names) and {@code rows} (list of value lists).
 *
 * <p>HTTP requests are stateless — {@code SET search_path TO demo}
 * issued in one call does not carry over to the next. The persistent
 * equivalent is the {@code Default-Schema} request header, which this
 * client sets on every request to {@code "demo"} so unqualified table
 * names resolve under that schema.
 */
public final class CrateDbClient {

    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    /** Endpoint URL with credentials stripped — e.g. {@code http://host:4200/_sql}. */
    private final URI endpoint;

    /** Pre-built {@code Authorization: Basic ...} header value, or null when no auth. */
    private final String basicAuthHeader;

    public CrateDbClient(String cratedbUrl) {
        Objects.requireNonNull(cratedbUrl, "cratedbUrl");
        URI parsed = URI.create(cratedbUrl);

        String host = parsed.getHost();
        int port = parsed.getPort() == -1 ? 4200 : parsed.getPort();
        String scheme = parsed.getScheme() == null ? "http" : parsed.getScheme();
        this.endpoint = URI.create(scheme + "://" + host + ":" + port + "/_sql");

        String userInfo = parsed.getUserInfo();
        if (userInfo != null && !userInfo.isEmpty()) {
            this.basicAuthHeader = "Basic " + Base64.getEncoder()
                    .encodeToString(userInfo.getBytes(StandardCharsets.UTF_8));
        } else {
            this.basicAuthHeader = null;
        }
    }

    /**
     * Execute a single SQL statement and return the raw response body
     * parsed as JSON. The returned object has at least {@code cols} and
     * {@code rows} fields on success. On HTTP error, the body is still
     * returned so the caller can surface the error message to Claude
     * as tool output.
     */
    public JsonObject runSql(String sql) throws Exception {
        JsonObject reqBody = new JsonObject();
        reqBody.addProperty("stmt", sql);
        String body = reqBody.toString();

        HttpRequest.Builder reqBuilder = HttpRequest.newBuilder(endpoint)
                .header("Content-Type", "application/json")
                .header("Default-Schema", "demo")
                .timeout(Duration.ofSeconds(60))
                .POST(HttpRequest.BodyPublishers.ofString(body));
        if (basicAuthHeader != null) {
            reqBuilder.header("Authorization", basicAuthHeader);
        }

        HttpResponse<String> response = httpClient.send(
                reqBuilder.build(), HttpResponse.BodyHandlers.ofString());
        return JsonParser.parseString(response.body()).getAsJsonObject();
    }

    /**
     * Convenience formatter — render the JSON response from
     * {@link #runSql(String)} into a human-readable text block (the
     * SQL that ran, column names, rows, truncation note for large
     * results) suitable for use as a tool result.
     */
    public static String formatResult(String renderedSql, JsonObject result) {
        if (result.has("error")) {
            return "SQL: " + renderedSql + "\n\nERROR: " + result.get("error");
        }
        JsonArray cols = result.has("cols") ? result.getAsJsonArray("cols") : new JsonArray();
        JsonArray rows = result.has("rows") ? result.getAsJsonArray("rows") : new JsonArray();

        StringBuilder out = new StringBuilder();
        out.append("SQL:\n").append(renderedSql).append("\n\n");
        out.append("Columns: ").append(cols).append('\n');
        out.append("Row count: ").append(rows.size()).append('\n');
        int limit = Math.min(rows.size(), 50);
        for (int i = 0; i < limit; i++) {
            out.append("  ").append(rows.get(i)).append('\n');
        }
        if (rows.size() > limit) {
            out.append("  ... (").append(rows.size() - limit).append(" more rows omitted)\n");
        }
        return out.toString();
    }
}
