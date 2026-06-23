// Copyright 2026 Crate.io
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/// <summary>
/// Interactive search CLI for CrateDB's <c>german_regions</c> table.
///
/// Supports two modes:
///   KNN (default)  : semantic search via OpenAI embeddings + KNN_MATCH on a
///                    FLOAT_VECTOR column. Requires an OpenAI API key.
///   Fulltext       : BM25 match() against fulltext-indexed text columns.
///                    No OpenAI key needed. Enabled with --fulltext.
///
/// Usage:
///   # KNN (semantic) search
///   dotnet run -- --host myhost --user scott --password tiger
///
///   # Fulltext (BM25) search
///   dotnet run -- --host myhost --user scott --password tiger --fulltext
/// </summary>

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Npgsql;

const string Table = "german_regions";
const int DefaultPort = 5432;
const string DefaultUser = "crate";
const string DefaultPassword = "";
const string DefaultDatabase = "demo";
const int DefaultTopK = 5;
const string DefaultModel = "text-embedding-3-small";
const string DefaultFulltextColumns =
    "tourism_info,transportation,economics,introduced_species";
const string DefaultNameColumn = "region_name";

// --- Check for help ---

if (args.Any(a => a is "--help" or "-h"))
{
    PrintUsage();
    return;
}

// --- Parse arguments ---

var flags = new Dictionary<string, string>();
var boolFlags = new HashSet<string>();

for (var i = 0; i < args.Length; i++)
{
    if (args[i] == "--fulltext")
    {
        boolFlags.Add("fulltext");
    }
    else if (args[i].StartsWith("--") && i + 1 < args.Length)
    {
        flags[args[i][2..]] = args[i + 1];
        i++;
    }
}

var host = GetFlag("host", "CRATEDB_HOST", null);
var port = int.Parse(GetFlag("port", "CRATEDB_PORT", DefaultPort.ToString())!);
var user = GetFlag("user", "CRATEDB_USER", DefaultUser)!;
var password = GetFlag("password", "CRATEDB_PASSWORD", DefaultPassword)!;
var database = GetFlag("database", "CRATEDB_DB", DefaultDatabase)!;
var topK = int.Parse(GetFlag("top-k", null, DefaultTopK.ToString())!);
var nameColumn = GetFlag("name-column", null, DefaultNameColumn)!;
var fulltext = boolFlags.Contains("fulltext");
var fulltextColumnsStr = GetFlag("fulltext-columns", "CRATEDB_FULLTEXT_COLUMNS",
    DefaultFulltextColumns)!;
var openaiKey = GetFlag("openai-key", "OPENAI_API_KEY", null);
var model = GetFlag("model", "OPENAI_EMBED_MODEL", DefaultModel)!;

// --- Validate ---

var missing = new List<string>();
if (string.IsNullOrEmpty(host))
    missing.Add("--host / CRATEDB_HOST");
if (!fulltext && string.IsNullOrEmpty(openaiKey))
    missing.Add("--openai-key / OPENAI_API_KEY (or use --fulltext)");
if (missing.Count > 0)
{
    Console.Error.WriteLine(
        $"Error: missing required parameter(s): {string.Join(", ", missing)}");
    Environment.Exit(2);
    return;
}

if (!IsSafeIdent(nameColumn))
{
    Console.Error.WriteLine($"Error: invalid column name '{nameColumn}'");
    Environment.Exit(2);
    return;
}

var fulltextColumnList = fulltextColumnsStr
    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
    .ToList();

if (fulltext && fulltextColumnList.Count == 0)
{
    Console.Error.WriteLine("Error: --fulltext-columns is empty");
    Environment.Exit(2);
    return;
}
foreach (var col in fulltextColumnList)
{
    if (!IsSafeIdent(col))
    {
        Console.Error.WriteLine($"Error: invalid fulltext column name '{col}'");
        Environment.Exit(2);
        return;
    }
}

// --- Read query ---

var query = ReadQuery();
if (string.IsNullOrEmpty(query))
{
    Console.Error.WriteLine("[info] empty query, exiting");
    return;
}

// --- Connect and search ---

var connString = new NpgsqlConnectionStringBuilder
{
    Host = host,
    Port = port,
    Database = database,
    Username = user,
    Password = password,
}.ConnectionString;

try
{
    await using var conn = new NpgsqlConnection(connString);
    await conn.OpenAsync();

    if (fulltext)
    {
        PreflightFulltext(conn, fulltextColumnList);
        FulltextSearch(conn, nameColumn, fulltextColumnList, query, topK);
    }
    else
    {
        PreflightKnn(conn);
        await KnnSearch(conn, nameColumn, openaiKey!, model, query, topK);
    }
}
catch (NpgsqlException e)
{
    Console.Error.WriteLine($"Database error: {e.Message}");
    Environment.Exit(1);
}

// --- Helper methods ---

string? GetFlag(string flagName, string? envVar, string? defaultValue)
{
    if (flags.TryGetValue(flagName, out var value))
        return value;
    if (envVar != null)
    {
        var envValue = Environment.GetEnvironmentVariable(envVar);
        if (!string.IsNullOrEmpty(envValue))
            return envValue;
    }
    return defaultValue;
}

bool IsSafeIdent(string s)
{
    if (string.IsNullOrEmpty(s)) return false;
    return s.All(c => char.IsLetterOrDigit(c) || c == '_');
}

string ReadQuery()
{
    if (Console.IsInputRedirected)
    {
        var line = Console.ReadLine();
        return line?.Trim() ?? "";
    }
    Console.Error.Write("Search: ");
    var input = Console.ReadLine();
    return input?.Trim() ?? "";
}

void PreflightKnn(NpgsqlConnection conn)
{
    using (var cmd = new NpgsqlCommand(
               "SELECT COUNT(*) FROM information_schema.columns " +
               "WHERE table_name = 'german_regions' AND column_name = 'embedding'",
               conn))
    {
        var count = (long)(cmd.ExecuteScalar() ?? 0);
        if (count == 0)
        {
            Console.Error.WriteLine(
                "Error: table 'german_regions' has no 'embedding' column");
            Environment.Exit(1);
        }
    }

    using (var cmd = new NpgsqlCommand(
               "SELECT COUNT(*) FROM german_regions WHERE embedding IS NOT NULL",
               conn))
    {
        var n = (long)(cmd.ExecuteScalar() ?? 0);
        if (n == 0)
        {
            Console.Error.WriteLine(
                "Error: table 'german_regions' has no rows with a populated embedding");
            Environment.Exit(1);
        }
        Console.Error.WriteLine($"[info] german_regions: {n} row(s) with embeddings");
    }
}

void PreflightFulltext(NpgsqlConnection conn, List<string> columns)
{
    var present = new HashSet<string>();
    using (var cmd = new NpgsqlCommand(
               "SELECT column_name FROM information_schema.columns " +
               "WHERE table_name = 'german_regions'", conn))
    using (var reader = cmd.ExecuteReader())
    {
        while (reader.Read())
            present.Add(reader.GetString(0));
    }

    var missingCols = columns.Where(c => !present.Contains(c)).ToList();
    if (missingCols.Count > 0)
    {
        Console.Error.WriteLine(
            $"Error: table 'german_regions' is missing fulltext column(s): " +
            $"[{string.Join(", ", missingCols)}]");
        Environment.Exit(1);
    }
    Console.Error.WriteLine(
        $"[info] fulltext match across: {string.Join(", ", columns)}");
}

async Task KnnSearch(NpgsqlConnection conn, string nameCol, string apiKey,
    string embedModel, string searchQuery, int k)
{
    Console.Error.WriteLine($"[info] embedding query: \"{searchQuery}\"");
    var vec = await GetEmbedding(apiKey, searchQuery, embedModel);

    var sql = $"SELECT {nameCol}, _score FROM {Table} " +
              "WHERE KNN_MATCH(embedding, $1, $2) ORDER BY _score DESC LIMIT $3";

    await using var cmd = new NpgsqlCommand(sql, conn);
    cmd.Parameters.AddWithValue(vec);
    cmd.Parameters.AddWithValue(k);
    cmd.Parameters.AddWithValue(k);

    await using var reader = await cmd.ExecuteReaderAsync();
    PrintResults(reader);
}

void FulltextSearch(NpgsqlConnection conn, string nameCol,
    List<string> ftColumns, string searchQuery, int k)
{
    Console.Error.WriteLine($"[info] fulltext query: \"{searchQuery}\"");
    var columnsSql = string.Join(", ", ftColumns);

    var sql = $"SELECT {nameCol}, _score FROM {Table} " +
              $"WHERE match(({columnsSql}), $1) ORDER BY _score DESC LIMIT $2";

    using var cmd = new NpgsqlCommand(sql, conn);
    cmd.Parameters.AddWithValue(searchQuery);
    cmd.Parameters.AddWithValue(k);

    using var reader = cmd.ExecuteReader();
    PrintResults(reader);
}

async Task<float[]> GetEmbedding(string apiKey, string text, string embedModel)
{
    text = text.Replace("\n", " ").Trim();

    using var httpClient = new HttpClient();
    httpClient.DefaultRequestHeaders.Authorization =
        new AuthenticationHeaderValue("Bearer", apiKey);

    var requestBody = JsonSerializer.Serialize(new { input = text, model = embedModel });
    var content = new StringContent(requestBody, Encoding.UTF8, "application/json");

    var response = await httpClient.PostAsync(
        "https://api.openai.com/v1/embeddings", content);
    if (!response.IsSuccessStatusCode)
    {
        var errorBody = await response.Content.ReadAsStringAsync();
        throw new Exception(
            $"OpenAI API error (HTTP {(int)response.StatusCode}): {errorBody}");
    }

    var json = await response.Content.ReadAsStringAsync();
    var doc = JsonDocument.Parse(json);
    var embeddingElement = doc.RootElement
        .GetProperty("data")[0]
        .GetProperty("embedding");

    var embedding = new float[embeddingElement.GetArrayLength()];
    for (var i = 0; i < embedding.Length; i++)
        embedding[i] = embeddingElement[i].GetSingle();

    return embedding;
}

void PrintResults(NpgsqlDataReader reader)
{
    Console.WriteLine($"{"Region",-30}  Score");
    Console.WriteLine($"{new string('-', 30)}  -----");
    var rank = 0;
    while (reader.Read())
    {
        rank++;
        var name = reader.GetString(0);
        var score = Convert.ToDouble(reader.GetValue(1));
        Console.WriteLine($"#{rank}  {name,-28}  {score:F4}");
    }
}

void PrintUsage()
{
    Console.Error.WriteLine("Usage: CrateDbKnnSearch [options]");
    Console.Error.WriteLine();
    Console.Error.WriteLine("Options:");
    Console.Error.WriteLine("  --host <host>              CrateDB host (env: CRATEDB_HOST)");
    Console.Error.WriteLine("  --port <port>              PostgreSQL port (env: CRATEDB_PORT, default 5432)");
    Console.Error.WriteLine("  --user <user>              Database user (env: CRATEDB_USER, default crate)");
    Console.Error.WriteLine("  --password <pass>          Database password (env: CRATEDB_PASSWORD)");
    Console.Error.WriteLine("  --database <db>            Database name (env: CRATEDB_DB, default demo)");
    Console.Error.WriteLine("  --top-k <n>                Number of results (default 5)");
    Console.Error.WriteLine("  --name-column <col>        Column for row names (default region_name)");
    Console.Error.WriteLine("  --fulltext                 Use BM25 fulltext search instead of KNN");
    Console.Error.WriteLine("  --fulltext-columns <cols>  Comma-separated fulltext columns (env: CRATEDB_FULLTEXT_COLUMNS)");
    Console.Error.WriteLine("  --openai-key <key>         OpenAI API key (env: OPENAI_API_KEY, required for KNN)");
    Console.Error.WriteLine("  --model <model>            OpenAI embedding model (env: OPENAI_EMBED_MODEL, default text-embedding-3-small)");
    Console.Error.WriteLine("  -h, --help                 Show this help");
}
