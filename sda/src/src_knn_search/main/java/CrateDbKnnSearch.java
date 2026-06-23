/*
 * Licensed to Crate.io GmbH ("Crate") under one or more contributor
 * license agreements.  See the NOTICE file distributed with this work for
 * additional information regarding copyright ownership.  Crate licenses
 * this file to you under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.  You may
 * obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 * However, if you have executed another commercial license agreement
 * with Crate these terms will supersede the license and you may use the
 * software solely pursuant to the terms of the relevant commercial agreement.
 */

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.*;
import java.util.*;

/**
 * Interactive search CLI for CrateDB's {@code german_regions} table.
 * <p>
 * Supports two modes:
 * <ul>
 *   <li><b>KNN (default)</b> — semantic search via OpenAI embeddings and
 *       CrateDB's {@code KNN_MATCH} on a {@code FLOAT_VECTOR} column.</li>
 *   <li><b>Fulltext</b> — BM25 relevance search via CrateDB's {@code MATCH}
 *       predicate. No OpenAI key needed. Enabled with {@code --fulltext}.</li>
 * </ul>
 *
 * <h2>Usage</h2>
 * <pre>
 *   # KNN (semantic) search
 *   mvn compile exec:java -Dexec.args="--host myhost --user scott --password tiger"
 *
 *   # Fulltext (BM25) search
 *   mvn compile exec:java -Dexec.args="--host myhost --user scott --password tiger --fulltext"
 * </pre>
 */
public class CrateDbKnnSearch {

    private static final String TABLE = "german_regions";
    private static final int DEFAULT_PORT = 5432;
    private static final String DEFAULT_USER = "crate";
    private static final String DEFAULT_PASSWORD = "";
    private static final String DEFAULT_DATABASE = "demo";
    private static final int DEFAULT_TOP_K = 5;
    private static final String DEFAULT_MODEL = "text-embedding-3-small";
    private static final String DEFAULT_FULLTEXT_COLUMNS =
            "tourism_info,transportation,economics,introduced_species";
    private static final String DEFAULT_NAME_COLUMN = "region_name";

    private record Config(
            String host, int port, String user, String password, String database,
            int topK, String nameColumn, boolean fulltext, List<String> fulltextColumns,
            String openaiKey, String model) {}

    public static void main(String[] args) {
        for (String arg : args) {
            if ("--help".equals(arg) || "-h".equals(arg)) {
                printUsage();
                return;
            }
        }

        Config config = parseArgs(args);

        String query = readQuery();
        if (query.isEmpty()) {
            System.err.println("[info] empty query, exiting");
            return;
        }

        try (Connection conn = connect(config)) {
            if (config.fulltext()) {
                preflightFulltext(conn, config.fulltextColumns());
                fulltextSearch(conn, config, query);
            } else {
                preflightKnn(conn);
                knnSearch(conn, config, query);
            }
        } catch (SQLException e) {
            System.err.println("Database error: " + e.getMessage());
            System.exit(1);
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            System.exit(1);
        }
    }

    private static Config parseArgs(String[] args) {
        Map<String, String> flags = new HashMap<>();
        Set<String> boolFlags = new HashSet<>();

        for (int i = 0; i < args.length; i++) {
            if ("--fulltext".equals(args[i])) {
                boolFlags.add("fulltext");
            } else if (args[i].startsWith("--") && i + 1 < args.length) {
                flags.put(args[i].substring(2), args[i + 1]);
                i++;
            }
        }

        String host = flags.getOrDefault("host", envOrDefault("CRATEDB_HOST", null));
        int port = Integer.parseInt(
                flags.getOrDefault("port", envOrDefault("CRATEDB_PORT",
                        String.valueOf(DEFAULT_PORT))));
        String user = flags.getOrDefault("user", envOrDefault("CRATEDB_USER", DEFAULT_USER));
        String password = flags.getOrDefault("password",
                envOrDefault("CRATEDB_PASSWORD", DEFAULT_PASSWORD));
        String database = flags.getOrDefault("database",
                envOrDefault("CRATEDB_DB", DEFAULT_DATABASE));
        int topK = Integer.parseInt(
                flags.getOrDefault("top-k", String.valueOf(DEFAULT_TOP_K)));
        String nameColumn = flags.getOrDefault("name-column", DEFAULT_NAME_COLUMN);
        boolean fulltext = boolFlags.contains("fulltext");
        String fulltextColumnsStr = flags.getOrDefault("fulltext-columns",
                envOrDefault("CRATEDB_FULLTEXT_COLUMNS", DEFAULT_FULLTEXT_COLUMNS));
        String openaiKey = flags.getOrDefault("openai-key",
                envOrDefault("OPENAI_API_KEY", null));
        String model = flags.getOrDefault("model",
                envOrDefault("OPENAI_EMBED_MODEL", DEFAULT_MODEL));

        List<String> missing = new ArrayList<>();
        if (host == null || host.isEmpty()) {
            missing.add("--host / CRATEDB_HOST");
        }
        if (!fulltext && (openaiKey == null || openaiKey.isEmpty())) {
            missing.add("--openai-key / OPENAI_API_KEY (or use --fulltext)");
        }
        if (!missing.isEmpty()) {
            System.err.println("Error: missing required parameter(s): "
                    + String.join(", ", missing));
            System.exit(2);
        }

        if (!isSafeIdent(nameColumn)) {
            System.err.println("Error: invalid column name '" + nameColumn + "'");
            System.exit(2);
        }

        List<String> fulltextColumnList = Arrays.stream(fulltextColumnsStr.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .toList();

        if (fulltext && fulltextColumnList.isEmpty()) {
            System.err.println("Error: --fulltext-columns is empty");
            System.exit(2);
        }
        for (String col : fulltextColumnList) {
            if (!isSafeIdent(col)) {
                System.err.println("Error: invalid fulltext column name '" + col + "'");
                System.exit(2);
            }
        }

        return new Config(host, port, user, password, database, topK, nameColumn,
                fulltext, fulltextColumnList, openaiKey, model);
    }

    private static String envOrDefault(String envVar, String defaultValue) {
        String value = System.getenv(envVar);
        return (value != null && !value.isEmpty()) ? value : defaultValue;
    }

    private static boolean isSafeIdent(String s) {
        if (s == null || s.isEmpty()) return false;
        for (char c : s.toCharArray()) {
            if (!Character.isLetterOrDigit(c) && c != '_') return false;
        }
        return true;
    }

    private static Connection connect(Config config) throws SQLException {
        String url = String.format("jdbc:postgresql://%s:%d/%s",
                config.host(), config.port(), config.database());
        Properties props = new Properties();
        props.setProperty("user", config.user());
        props.setProperty("password", config.password());
        return DriverManager.getConnection(url, props);
    }

    private static float[] getEmbedding(String apiKey, String text, String model)
            throws Exception {
        text = text.replace("\n", " ").trim();

        JsonObject body = new JsonObject();
        body.addProperty("input", text);
        body.addProperty("model", model);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.openai.com/v1/embeddings"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + apiKey)
                .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
                .build();

        HttpClient client = HttpClient.newHttpClient();
        HttpResponse<String> response = client.send(request,
                HttpResponse.BodyHandlers.ofString());

        if (response.statusCode() != 200) {
            throw new RuntimeException("OpenAI API error (HTTP " + response.statusCode()
                    + "): " + response.body());
        }

        JsonObject json = JsonParser.parseString(response.body()).getAsJsonObject();
        JsonArray embeddingArray = json.getAsJsonArray("data")
                .get(0).getAsJsonObject()
                .getAsJsonArray("embedding");

        float[] embedding = new float[embeddingArray.size()];
        for (int i = 0; i < embeddingArray.size(); i++) {
            embedding[i] = embeddingArray.get(i).getAsFloat();
        }
        return embedding;
    }

    private static void preflightKnn(Connection conn) throws SQLException {
        try (Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery(
                     "SELECT COUNT(*) FROM information_schema.columns "
                     + "WHERE table_name = 'german_regions' AND column_name = 'embedding'")) {
            if (rs.next() && rs.getInt(1) == 0) {
                System.err.println(
                        "Error: table 'german_regions' has no 'embedding' column");
                System.exit(1);
            }
        }

        try (Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery(
                     "SELECT COUNT(*) FROM german_regions WHERE embedding IS NOT NULL")) {
            if (rs.next()) {
                int n = rs.getInt(1);
                if (n == 0) {
                    System.err.println("Error: table 'german_regions' has no rows "
                            + "with a populated embedding");
                    System.exit(1);
                }
                System.err.println("[info] german_regions: " + n
                        + " row(s) with embeddings");
            }
        }
    }

    private static void preflightFulltext(Connection conn, List<String> columns)
            throws SQLException {
        Set<String> present = new HashSet<>();
        try (Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery(
                     "SELECT column_name FROM information_schema.columns "
                     + "WHERE table_name = 'german_regions'")) {
            while (rs.next()) {
                present.add(rs.getString(1));
            }
        }
        List<String> missing = columns.stream()
                .filter(c -> !present.contains(c)).toList();
        if (!missing.isEmpty()) {
            System.err.println("Error: table 'german_regions' is missing "
                    + "fulltext column(s): " + missing);
            System.exit(1);
        }
        System.err.println("[info] fulltext match across: "
                + String.join(", ", columns));
    }

    private static void knnSearch(Connection conn, Config config, String query)
            throws Exception {
        System.err.println("[info] embedding query: \"" + query + "\"");
        float[] vec = getEmbedding(config.openaiKey(), query, config.model());

        String sql = "SELECT " + config.nameColumn() + ", _score FROM " + TABLE
                + " WHERE KNN_MATCH(embedding, ?, ?) ORDER BY _score DESC LIMIT ?";

        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            Float[] boxed = new Float[vec.length];
            for (int i = 0; i < vec.length; i++) boxed[i] = vec[i];
            ps.setArray(1, conn.createArrayOf("real", boxed));
            ps.setInt(2, config.topK());
            ps.setInt(3, config.topK());

            try (ResultSet rs = ps.executeQuery()) {
                printResults(rs);
            }
        }
    }

    private static void fulltextSearch(Connection conn, Config config, String query)
            throws SQLException {
        System.err.println("[info] fulltext query: \"" + query + "\"");
        String columnsSql = String.join(", ", config.fulltextColumns());

        String sql = "SELECT " + config.nameColumn() + ", _score FROM " + TABLE
                + " WHERE match((" + columnsSql + "), ?) ORDER BY _score DESC LIMIT ?";

        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, query);
            ps.setInt(2, config.topK());

            try (ResultSet rs = ps.executeQuery()) {
                printResults(rs);
            }
        }
    }

    private static void printResults(ResultSet rs) throws SQLException {
        System.out.printf("%-30s  Score%n", "Region");
        System.out.printf("%-30s  -----%n", "-".repeat(30));
        int rank = 0;
        while (rs.next()) {
            rank++;
            System.out.printf("#%d  %-28s  %.4f%n",
                    rank, rs.getString(1), rs.getDouble(2));
        }
    }

    private static String readQuery() {
        try {
            if (System.console() != null) {
                String line = System.console().readLine("Search: ");
                return (line != null) ? line.trim() : "";
            }
            BufferedReader reader = new BufferedReader(
                    new InputStreamReader(System.in));
            String line = reader.readLine();
            return (line != null) ? line.trim() : "";
        } catch (Exception e) {
            return "";
        }
    }

    private static void printUsage() {
        System.err.println("Usage: CrateDbKnnSearch [options]");
        System.err.println();
        System.err.println("Options:");
        System.err.println("  --host <host>              CrateDB host (env: CRATEDB_HOST)");
        System.err.println("  --port <port>              PostgreSQL port (env: CRATEDB_PORT, default 5432)");
        System.err.println("  --user <user>              Database user (env: CRATEDB_USER, default crate)");
        System.err.println("  --password <pass>          Database password (env: CRATEDB_PASSWORD)");
        System.err.println("  --database <db>            Database name (env: CRATEDB_DB, default demo)");
        System.err.println("  --top-k <n>                Number of results (default 5)");
        System.err.println("  --name-column <col>        Column for row names (default region_name)");
        System.err.println("  --fulltext                 Use BM25 fulltext search instead of KNN");
        System.err.println("  --fulltext-columns <cols>  Comma-separated fulltext columns (env: CRATEDB_FULLTEXT_COLUMNS)");
        System.err.println("  --openai-key <key>         OpenAI API key (env: OPENAI_API_KEY, required for KNN)");
        System.err.println("  --model <model>            OpenAI embedding model (env: OPENAI_EMBED_MODEL, default text-embedding-3-small)");
        System.err.println("  -h, --help                 Show this help");
    }
}
