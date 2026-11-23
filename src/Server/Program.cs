using Microsoft.AspNetCore.SignalR;
using HedgeFund.Server.Hubs;
using HedgeFund.Server.Connectors;
using HedgeFund.Server.Services;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;
using System.Text.Json;
using Microsoft.AspNetCore.Http.Extensions;
using System.Collections.Generic;
using System.Security.Cryptography;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;

// Load .env file
var envPath = Path.Combine(AppContext.BaseDirectory, ".env");
if (!File.Exists(envPath)) envPath = Path.Combine(Directory.GetCurrentDirectory(), ".env");
if (!File.Exists(envPath)) envPath = Path.Combine(Directory.GetCurrentDirectory(), "..", ".env");
if (!File.Exists(envPath)) envPath = Path.Combine(Directory.GetCurrentDirectory(), "..", "..", "..", ".env");
if (File.Exists(envPath))
    foreach (var line in File.ReadLines(envPath))
    {
        var eq = line.IndexOf('=');
        if (eq > 0 && !line.StartsWith('#'))
            Environment.SetEnvironmentVariable(line[..eq].Trim(), line[(eq+1)..].Trim());
    }

var builder = WebApplication.CreateBuilder(args);

// === SignalR ===
builder.Services.AddSignalR(options =>
{
    options.EnableDetailedErrors = true;
    options.MaximumReceiveMessageSize = 1024 * 1024; // 1 РњР‘
});

// === CORS вЂ” СЂР°Р·СЂРµС€РёС‚СЊ РїРѕРґРєР»СЋС‡РµРЅРёСЏ СЃ Р»СЋР±РѕРіРѕ РєР»РёРµРЅС‚Р° ===
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
    {
        policy.AllowAnyHeader()
              .AllowAnyMethod()
              .AllowCredentials()
              .WithOrigins("http://localhost:5050", "http://127.0.0.1:5050", "http://216.57.106.32:5050");
    });
});

// === РђСѓС‚РµРЅС‚РёС„РёРєР°С†РёСЏ (Cookie) ===
var loginUser = Environment.GetEnvironmentVariable("APP_LOGIN") ?? "admin";
var loginPassHash = Environment.GetEnvironmentVariable("APP_PASS_HASH") ?? Convert.ToBase64String(SHA256.HashData("admin"u8.ToArray()));

builder.Services.AddAuthentication(CookieAuthenticationDefaults.AuthenticationScheme)
    .AddCookie(options =>
    {
        options.LoginPath = "/login.html";
        options.LogoutPath = "/api/logout";
        options.Cookie.Name = "OpenMarkets.Auth";
        options.Cookie.HttpOnly = true;
        options.ExpireTimeSpan = TimeSpan.FromDays(7);
        options.SlidingExpiration = true;
        options.AccessDeniedPath = "/login.html";
    });
builder.Services.AddAuthorization();

// === РЎРµСЂРІРёСЃС‹ ===
builder.Services.AddSingleton<TradingService>();
builder.Services.AddSingleton<StrategyRunner>();

// === РџРѕСЂС‚ (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ 5050, РЅР°СЃС‚СЂР°РёРІР°РµРјС‹Р№ С‡РµСЂРµР· --urls РёР»Рё ASPNETCORE_URLS) ===
var port = builder.Configuration.GetValue<int>("Port", 5050);
builder.WebHost.UseUrls($"http://0.0.0.0:{port}");

var app = builder.Build();
var httpClient = new HttpClient();

// === RISK GATE (declared early, used by all launchers) ===
var riskGate = new HedgeFund.Core.Risk.RiskGate(
    goPerLot: 12000.0,
    safetyMarginFactor: 1.2,
    maxLots: 10,
    dailyLossLimit: 5000.0,
    maxDrawdownPct: 0.05);
httpClient.Timeout = TimeSpan.FromSeconds(3);

app.UseCors();
app.UseDefaultFiles();
app.UseStaticFiles(new StaticFileOptions
{
    OnPrepareResponse = ctx =>
    {
        // No-cache для HTML и JS — чтобы UI фиксы применялись сразу (F-024),
        // без ручных Ctrl+F5 и бампов версий
        var fname = ctx.File.Name ?? "";
        if (fname.EndsWith(".html", StringComparison.OrdinalIgnoreCase) ||
            fname.EndsWith(".js", StringComparison.OrdinalIgnoreCase))
        {
            ctx.Context.Response.Headers["Cache-Control"] = "no-cache, no-store, must-revalidate";
            ctx.Context.Response.Headers["Pragma"] = "no-cache";
            ctx.Context.Response.Headers["Expires"] = "0";
        }
    }
});
app.UseAuthentication();
app.UseAuthorization();

// === Rate Limiter for /api/login (5 attempts/min per IP) ===
var _loginRateLimit = new System.Collections.Concurrent.ConcurrentDictionary<string, (int count, long windowStart)>();
var _rateLimitCleanupLock = new object();
DateTime _lastRateLimitCleanup = DateTime.UtcNow;

app.Use(async (HttpContext ctx, Func<Task> next) =>
{
    if (ctx.Request.Path.Value == "/api/login" && ctx.Request.Method == "POST")
    {
        var ip = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var entry = _loginRateLimit.GetOrAdd(ip, _ => (0, now));
        
        if (now - entry.windowStart > 60)
        {
            _loginRateLimit.TryUpdate(ip, (1, now), entry);
        }
        else
        {
            var newCount = entry.count + 1;
            if (newCount > 5)
            {
                ctx.Response.StatusCode = 429;
                ctx.Response.ContentType = "application/json";
                await ctx.Response.WriteAsync("{\"error\":\"Too many login attempts. Try again in 1 minute.\"}");
                return;
            }
            _loginRateLimit.TryUpdate(ip, (newCount, entry.windowStart), entry);
        }
        
        // Periodic cleanup every 2 minutes
        if ((DateTime.UtcNow - _lastRateLimitCleanup).TotalMinutes >= 2)
        {
            lock (_rateLimitCleanupLock)
            {
                if ((DateTime.UtcNow - _lastRateLimitCleanup).TotalMinutes >= 2)
                {
                    var cutoff = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - 120;
                    foreach (var kv in _loginRateLimit)
                    {
                        if (kv.Value.windowStart < cutoff)
                            _loginRateLimit.TryRemove(kv.Key, out _);
                    }
                    _lastRateLimitCleanup = DateTime.UtcNow;
                }
            }
        }
    }
    await next();
});


// === Login API ===
app.MapPost("/api/login", async (HttpRequest req, HttpResponse res) =>
{
    using var reader = new StreamReader(req.Body);
    var body = await reader.ReadToEndAsync();
    try
    {
        var json = System.Text.Json.JsonDocument.Parse(body);
        var username = json.RootElement.GetProperty("username").GetString() ?? "";
        var password = json.RootElement.GetProperty("password").GetString() ?? "";
        var passHash = Convert.ToBase64String(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(password)));
        
        if (username == loginUser && passHash == loginPassHash)
        {
            var claims = new[] { new System.Security.Claims.Claim(System.Security.Claims.ClaimTypes.Name, username) };
            var identity = new System.Security.Claims.ClaimsIdentity(claims, CookieAuthenticationDefaults.AuthenticationScheme);
            var principal = new System.Security.Claims.ClaimsPrincipal(identity);
            await req.HttpContext.SignInAsync(CookieAuthenticationDefaults.AuthenticationScheme, principal);
            return Results.Ok(new { success = true });
        }
        return Results.Unauthorized();
    }
    catch { return Results.BadRequest(); }
});

app.MapPost("/api/logout", async (HttpContext ctx) =>
{
    await ctx.SignOutAsync(CookieAuthenticationDefaults.AuthenticationScheme);
    return Results.Ok(new { success = true });
});

app.MapGet("/api/me", (HttpContext ctx) =>
{
    if (ctx.User.Identity?.IsAuthenticated == true)
        return Results.Ok(new { user = ctx.User.Identity.Name });
    return Results.Unauthorized();
}).RequireAuthorization();


// === Auth Middleware (protect all except login/health/static) ===
app.Use(async (HttpContext ctx, Func<Task> next) =>
{
    var path = ctx.Request.Path.Value ?? "";
    
    // Allow: login, logout, health, test, heartbeat, static files, login page
    if (path.StartsWith("/api/login") || 
        path.StartsWith("/api/logout") ||
        path.StartsWith("/strategy/") ||
        path.StartsWith("/api/active") ||
        path.StartsWith("/connect-broker") || 
        path.StartsWith("/api/accounts") ||
        path.StartsWith("/api/connectors") ||
        path.StartsWith("/api/robot/service") ||
        path.StartsWith("/api/robot/status") ||
        path.StartsWith("/api/robot/config") ||
        path.StartsWith("/api/robot/update-instrument") ||
        path.StartsWith("/api/vp-backtest") ||
        path.StartsWith("/api/robot/ticker") ||
        path.StartsWith("/api/trades") ||
        path.StartsWith("/api/instance/") ||
        path == "/health" ||
        path == "/test" ||
        path == "/heartbeat" ||
        path == "/login.html" ||
        path == "/" ||
        path == "/index.html" ||
        path.StartsWith("/css/") || 
        path.StartsWith("/js/") || 
        path.StartsWith("/favicon") ||
        path.StartsWith("/trading"))
    {
        await next();
        return;
    }
    
    // Require auth for everything else (API + SignalR + pages)
    if (ctx.User.Identity?.IsAuthenticated != true)
    {
        ctx.Response.StatusCode = 401;
        if (ctx.Request.Headers["Accept"].ToString().Contains("text/html"))
        {
            ctx.Response.Redirect("/login.html");
            return;
        }
        await ctx.Response.WriteAsync("{\"error\":\"Unauthorized\"}");
        return;
    }
    
    await next();
});

// === Connector Manager ===
var connectorMgr = new HedgeFund.Core.Connectors.ConnectorManager();

// РўРµРєСѓС‰РёР№ РєРѕРЅРЅРµРєС‚РѕСЂ РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ: Finam
var _activeConnectorName = "Finam";

// === РњР°РїРїРёРЅРі С‚РёРєРµСЂРѕРІ РґР»СЏ Finam REST API ===
var _tickerToFinam = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
{
    ["SiU6"] = "SiU6@RTSX", ["SiZ6"] = "SiZ6@RTSX",
    ["MXU6"] = "MXU6@RTSX", ["MXZ6"] = "MXZ6@RTSX",
    ["GDU6"] = "GDU6@RTSX", ["RIZ6"] = "RIZ6@RTSX",
    ["BRU6"] = "BRU6@RTSX", ["BRV6"] = "BRV6@RTSX", ["NGQ6"] = "NGQ6@RTSX",
    ["BRM6"] = "BRM6@RTSX", ["BRK6"] = "BRK6@RTSX",
    ["GDM6"] = "GDM6@RTSX", ["GDH6"] = "GDH6@RTSX",
    ["MXM6"] = "MXM6@RTSX",
    ["RIU6"] = "RIU6@RTSX",
    ["LKOH"] = "LKOH@MISX", ["GMKN"] = "GMKN@MISX", ["NVTK"] = "NVTK@MISX",
    ["ROSN"] = "ROSN@MISX", ["SNGS"] = "SNGS@MISX", ["YNDX"] = "YNDX@MISX",
};
string ToFinamSymbol(string ticker) => _tickerToFinam.GetValueOrDefault(ticker, ticker.Contains('@') ? ticker : ticker + "@RTSX");

// === Finam REST JWT (auto-refresh, 15 min TTL) ===
string _finamApiKey = Environment.GetEnvironmentVariable("FINAM_API_KEY") ?? "";
string _finamAccountId = Environment.GetEnvironmentVariable("FINAM_ACCOUNT_ID") ?? "1225953";
string _finamJwt = "";
DateTime _finamJwtExpiry = DateTime.MinValue;
object _finamJwtLock = new object();

async Task<string> GetFinamJwt()
{
    lock (_finamJwtLock)
    {
        if (!string.IsNullOrEmpty(_finamJwt) && DateTime.UtcNow < _finamJwtExpiry.AddMinutes(-1))
            return _finamJwt;
    }
    // Use API key to get JWT for REST API (FINAM_TOKEN is gRPC-only)
    try
    {
        var apiKey = !string.IsNullOrEmpty(_finamApiKey) ? _finamApiKey : Environment.GetEnvironmentVariable("FINAM_API_KEY") ?? "";
        Console.WriteLine($"[DEBUG] GetFinamJwt: apiKey={(!string.IsNullOrEmpty(apiKey) ? "SET("+apiKey.Length+")" : "EMPTY")}");
        if (string.IsNullOrEmpty(apiKey)) return "";
        var resp = await new HttpClient().PostAsync("https://api.finam.ru/v1/sessions",
            new StringContent($"{{\"secret\": \"{apiKey}\"}}", System.Text.Encoding.UTF8, "application/json"));
        var json = await resp.Content.ReadAsStringAsync();
        Console.WriteLine($"[DEBUG] Sessions response: status={resp.StatusCode} body={json.Substring(0, Math.Min(json.Length, 100))}");
        var doc = JsonDocument.Parse(json);
        var token = doc.RootElement.GetProperty("token").GetString() ?? "";
        lock (_finamJwtLock)
        {
            _finamJwt = token;
            _finamJwtExpiry = DateTime.UtcNow.AddMinutes(14);
        }
        return token;
    }
    catch { return ""; }
}

var finamRest = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };

app.MapGet("/api/connectors", () => Results.Json(new
{
    active = _activeConnectorName,
    available = connectorMgr.AvailableConnectors,
    connected = connectorMgr.IsConnected
}));

app.MapPost("/api/connectors/switch", async (HttpRequest req) =>
{
    using var sr = new StreamReader(req.Body);
    var body = await sr.ReadToEndAsync();
    var doc = JsonDocument.Parse(body);
    var name = doc.RootElement.GetProperty("connector").GetString();
    var token = doc.RootElement.TryGetProperty("token", out var t) ? t.GetString() : "";
    
    if (name == null || !connectorMgr.AvailableConnectors.Contains(name))
        return Results.BadRequest(new { error = "Unknown connector" });
    
    IConnector connector = name switch
    {
        "Finam" => new HedgeFund.Server.Connectors.FinamConnectorAdapter(),
        "Transaq" => new HedgeFund.Server.Connectors.TransaqConnectorAdapter(),
        _ => throw new InvalidOperationException()
    };
    
    try
    {
        bool ok = await connector.ConnectAsync(token ?? "");
        if (!ok) { connector.Dispose(); return Results.Json(new { error = "Connection failed" }, statusCode: 400); }
        await connectorMgr.DisconnectAsync();
        connectorMgr.Active = connector;
        _activeConnectorName = name;
        return Results.Ok(new { status = "connected", connector = name });
    }
    catch (Exception ex) { connector.Dispose(); return Results.Json(new { error = ex.Message }, statusCode: 500); }
});

// === РњР°РїРїРёРЅРі SignalR Hub ===
app.MapHub<TradingHub>("/trading");


// === Candle Aggregator ===
var candleBuilderLock = new object();
GridMmRegimeLauncher? gridMm = null;
VolumeReversalLauncher? volRev = null;
// V7 removed 2026-06-23
var candleBuilderCurrent = (double[]?)null;
var candleBuilderHistory = new LinkedList<double[]>();
const int CANDLE_TF_MINUTES = 5;
const int MAX_CANDLES = 500;

void AggregateCandleTick(double price, double volume, long ts)
{
    lock (candleBuilderLock)
    {
        var candleStart = ts - (ts % (CANDLE_TF_MINUTES * 60));
        if (candleBuilderCurrent != null && (long)candleBuilderCurrent[0] == candleStart)
        {
            candleBuilderCurrent[2] = Math.Max(candleBuilderCurrent[2], price); // H
            candleBuilderCurrent[3] = Math.Min(candleBuilderCurrent[3], price); // L
            candleBuilderCurrent[4] = price; // C
            candleBuilderCurrent[5] += volume; // V
        }
        else
        {
            if (candleBuilderCurrent != null)
            {
                candleBuilderHistory.AddLast(candleBuilderCurrent);
                if (candleBuilderHistory.Count > MAX_CANDLES) candleBuilderHistory.RemoveFirst();
            }
            candleBuilderCurrent = new double[] { candleStart, price, price, price, price, volume };
        }
    }
}

// === Health-check endpoint ===
app.MapGet("/health", () => Results.Ok(new { status = "ok", time = DateTime.UtcNow })).AllowAnonymous();

// === Python Robot systemd proxy ===
app.MapPost("/api/robot/service/{action}", async (string action) =>
{
    try
    {
        // For stop: first tell robot to close positions, then stop service
        if (action == "stop")
        {
            try {
                using var http = new System.Net.Http.HttpClient();
                http.Timeout = System.TimeSpan.FromSeconds(5);
                await http.PostAsync("http://127.0.0.1:5070/stop", null);
            } catch {}
            await Task.Delay(2000); // wait for robot to close position
        }
        var psi = new System.Diagnostics.ProcessStartInfo
        {
            FileName = "systemctl",
            Arguments = action switch
            {
                "start" => "start trading-robot",
                "stop" => "stop trading-robot",
                _ => throw new ArgumentException($"Unknown action: {action}")
            },
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        var proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return Results.Json(new { error = "Failed to start process" });
        await proc.WaitForExitAsync();
        var output = await proc.StandardOutput.ReadToEndAsync();
        return Results.Json(new { action, exitCode = proc.ExitCode, output = output.Trim() });
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message });
    }
}).AllowAnonymous();

// === Robot status proxy (when robot API is down) ===
app.MapPost("/api/robot/stop", async () => {
    try {
        using var client = new System.Net.Http.HttpClient();
        client.Timeout = System.TimeSpan.FromSeconds(5);
        var resp = await client.PostAsync("http://localhost:5070/stop", null);
        var body = await resp.Content.ReadAsStringAsync();
        return Results.Text(body, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
});
app.MapPost("/api/robot/pause", async () => {
    try {
        using var client = new System.Net.Http.HttpClient();
        client.Timeout = System.TimeSpan.FromSeconds(3);
        var resp = await client.PostAsync("http://localhost:5070/pause", null);
        var body = await resp.Content.ReadAsStringAsync();
        return Results.Text(body, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
});
app.MapPost("/api/robot/resume", async () => {
    try {
        using var client = new System.Net.Http.HttpClient();
        client.Timeout = System.TimeSpan.FromSeconds(3);
        var resp = await client.PostAsync("http://localhost:5070/resume", null);
        var body = await resp.Content.ReadAsStringAsync();
        return Results.Text(body, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
});

app.MapGet("/api/robot/status", async () =>
{
    try {
        using var client = new System.Net.Http.HttpClient();
        client.Timeout = System.TimeSpan.FromSeconds(2);
        var resp = await client.GetAsync("http://localhost:5070/status");
        var body = await resp.Content.ReadAsStringAsync();
        return Results.Text(body, "application/json");
    } catch {
        return Results.Json(new { status = "offline", mode = "stopped" });
    }
}).AllowAnonymous();

app.MapGet("/api/robot/config", () =>
{
    try
    {
        var cfgPath = "/tmp/robot-config.json";
        if (System.IO.File.Exists(cfgPath))
        {
            var json = System.IO.File.ReadAllText(cfgPath);
            return Results.Content(json, "application/json");
        }
    } catch { }
    var defaults = System.Text.Json.JsonSerializer.Serialize(new { max_levels = 100, step_base = 31, spread_base = 31, max_hold_minutes = 99999999999999L, min_profit_per_lot = 35, vp_lookback = 33, vp_bin_size = 50, vp_va_percent = 0.70, rv_adaptation = false });
    return Results.Content(defaults, "application/json");
}).AllowAnonymous();
app.MapPost("/api/robot/config", async (HttpRequest req) =>
{
    try
    {
        using var reader = new System.IO.StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        // Save to file for robot to pick up
        System.IO.File.WriteAllText("/tmp/robot-config.json", body);
        // Also try to push to live robot
        try {
            using var client = new System.Net.Http.HttpClient();
            client.Timeout = System.TimeSpan.FromSeconds(2);
            var content = new System.Net.Http.StringContent(body, System.Text.Encoding.UTF8, "application/json");
            await client.PostAsync("http://localhost:5070/api/robot/config", content);
        } catch { }
        return Results.Content(body, "application/json");
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

// === F-012 endpoint ниже; сам класс в конце файла ===

app.MapPost("/api/of/process/{name}/{action}", async (string name, string action) =>
{
    try
    {
        if (action == "start")
        {
            var result = await RobotProcessManager.StartAsync(name);
            return Results.Json(result);
        }
        else if (action == "stop")
        {
            return Results.Json(RobotProcessManager.Stop(name));
        }
        else if (action == "status")
        {
            return Results.Json(new { alive = RobotProcessManager.IsAlive(name) });
        }
        return Results.Json(new { error = "unknown action" }, statusCode: 400);
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500);
    }
});

// === F-010: OF robot params proxy (works when robot offline) ===
app.MapGet("/api/of/config/{name}", (string name) =>
{
    var files = new Dictionary<string, string> { { "of", "of_config.json" }, { "of_mx", "of_config_mx.json" } };
    if (!files.TryGetValue(name, out var fname)) return Results.Json(new { error = "unknown robot" }, statusCode: 400);
    var dir = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
    for (int i = 0; i < 8 && dir != null; i++)
    {
        var candidate = System.IO.Path.Combine(dir.FullName, "robot", fname);
        if (System.IO.File.Exists(candidate))
        {
            try { return Results.Content(System.IO.File.ReadAllText(candidate), "application/json"); }
            catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
        }
        dir = dir.Parent;
    }
    return Results.Json(new Dictionary<string, object> { });
});
app.MapPost("/api/of/config/{name}", async (string name, HttpRequest req) =>
{
    var files = new Dictionary<string, string> { { "of", "of_config.json" }, { "of_mx", "of_config_mx.json" } };
    if (!files.TryGetValue(name, out var fname)) return Results.Json(new { error = "unknown robot" }, statusCode: 400);
    try
    {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        using var doc = System.Text.Json.JsonDocument.Parse(body);
        string? robotDirFound = null;
        var dir = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 8 && dir != null; i++)
        {
            var cdir = System.IO.Path.Combine(dir.FullName, "robot");
            if (System.IO.File.Exists(System.IO.Path.Combine(cdir, fname))) { robotDirFound = cdir; break; }
            dir = dir.Parent;
        }
        if (robotDirFound == null) return Results.Json(new { error = "robot config dir not found" }, statusCode: 500);
        var path = System.IO.Path.Combine(robotDirFound, fname);
        var cfg = new Dictionary<string, System.Text.Json.JsonElement>();
        if (System.IO.File.Exists(path))
        {
            try
            {
                using var old = System.Text.Json.JsonDocument.Parse(System.IO.File.ReadAllText(path));
                foreach (var prop in old.RootElement.EnumerateObject()) cfg[prop.Name] = prop.Value.Clone();
            }
            catch { }
        }
        using var inc = System.Text.Json.JsonDocument.Parse(body);
        foreach (var prop in inc.RootElement.EnumerateObject()) cfg[prop.Name] = prop.Value.Clone();
        System.IO.File.WriteAllText(path, System.Text.Json.JsonSerializer.Serialize(cfg));
        return Results.Json(new { ok = true });
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 400); }
});

// === Update robot ticker in config.py ===
app.MapPost("/api/robot/ticker", async (HttpRequest req) =>
{
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var ticker = json.RootElement.GetProperty("ticker").GetString() ?? "SiU6";
        // Map ticker to Finam symbol
        var symbolMap = new Dictionary<string,string> {
            {"SiM6", "SiM6@RTSX"}, {"SiU6", "SiU6@RTSX"}, {"SiH7", "SiH7@RTSX"},
            {"MXM6", "MXM6@RTSX"}, {"MXU6", "MXU6@RTSX"},
            {"GDM6", "GDM6@RTSX"}, {"BRK6", "BRK6@RTSX"},
            {"RIM6", "RIM6@RTSX"}, {"RIU6", "RIU6@RTSX"}
        };
        var symbol = symbolMap.GetValueOrDefault(ticker, ticker + "@RTSX");
        var configPath = "/root/.openclaw/workspace/HedgeFund/robot/config.py";
        var content = File.ReadAllText(configPath);
        // Replace SYMBOL and TICKER lines
        // Rewrite SYMBOL and TICKER lines
        var lines = content.Split('\n');
        for (int li = 0; li < lines.Length; li++) {
            if (lines[li].StartsWith("SYMBOL =")) lines[li] = $"SYMBOL = \"{symbol}\"";
            if (lines[li].StartsWith("TICKER =")) lines[li] = $"TICKER = \"{ticker}\"";
        }
        content = string.Join('\n', lines);
        File.WriteAllText(configPath, content);
        return Results.Json(new { ticker, symbol, updated = true });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

// === Test endpoint (no auth) ===
app.MapGet("/test", () => Results.Ok(new { message = "test endpoint works" })).AllowAnonymous();

// === REST API РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ ===
app.MapGet("/status", (TradingService svc) => Results.Ok(svc.GetStatus()));

app.MapPost("/connect-broker", async (TradingService svc, HttpRequest req) =>
{
    // РўРѕРєРµРЅ: РёР· С‚РµР»Р° Р·Р°РїСЂРѕСЃР° (JSON {token:"..."}) РёР»Рё РёР· РїРµСЂРµРјРµРЅРЅРѕР№ РѕРєСЂСѓР¶РµРЅРёСЏ
    string? token = null;
    try
    {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        if (!string.IsNullOrWhiteSpace(body))
        {
            var json = System.Text.Json.JsonDocument.Parse(body);
            if (json.RootElement.TryGetProperty("token", out var t))
                token = t.GetString();
        }
    } catch { }
    
    token ??= Environment.GetEnvironmentVariable("FINAM_API_KEY") ?? Environment.GetEnvironmentVariable("FINAM_TOKEN");
    if (string.IsNullOrEmpty(token))
        return Results.BadRequest(new { error = "FINAM_API_KEY РЅРµ Р·Р°РґР°РЅ" });

    // РЎРѕС…СЂР°РЅСЏРµРј РґР»СЏ Р°СЂР±РёС‚СЂР°Р¶Р° Рё РґСЂСѓРіРёС… СЃРµСЂРІРёСЃРѕРІ
    Environment.SetEnvironmentVariable("FINAM_TOKEN", token);

    // РџРѕРґРєР»СЋС‡Р°РµРј СЃ С‚Р°Р№РјР°СѓС‚РѕРј 30 СЃРµРє
    var hub = app.Services.GetRequiredService<IHubContext<TradingHub>>();
    await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "рџ”Њ РџРѕРґРєР»СЋС‡Р°СЋСЃСЊ Рє Р¤РёРЅР°Рј...");
    
    try
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        var connectTask = svc.ConnectBrokerAsync(token);
        var completed = await Task.WhenAny(connectTask, Task.Delay(-1, cts.Token));
        
        if (completed == connectTask)
        {
            var success = await connectTask;
            if (success)
            {
                await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "вњ… РџРѕРґРєР»СЋС‡РµРЅРѕ Рє Р¤РёРЅР°Рј!");
                return Results.Ok(new { status = "connected", broker = "Finam" });
            }
            else
            {
                await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊСЃСЏ");
                return Results.BadRequest(new { error = "РџРѕРґРєР»СЋС‡РµРЅРёРµ РЅРµ СѓРґР°Р»РѕСЃСЊ. РџСЂРѕРІРµСЂСЊС‚Рµ С‚РѕРєРµРЅ." });
            }
        }
        else
        {
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "вќЊ РўР°Р№РјР°СѓС‚ РїРѕРґРєР»СЋС‡РµРЅРёСЏ (30 СЃРµРє). РџСЂРѕРІРµСЂСЊС‚Рµ С‚РѕРєРµРЅ.");
            return Results.BadRequest(new { error = "РўР°Р№РјР°СѓС‚ РїРѕРґРєР»СЋС‡РµРЅРёСЏ 30СЃРµРє. РџСЂРѕРІРµСЂСЊС‚Рµ С‚РѕРєРµРЅ Рё РґРѕСЃС‚СѓРїРЅРѕСЃС‚СЊ Finam API." });
        }
    }
    catch (Exception ex)
    {
        await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", $"вќЊ РћС€РёР±РєР°: {ex.Message}");
        return Results.BadRequest(new { error = ex.Message });
    }
});

app.MapPost("/send-log", async (IHubContext<TradingHub> hub, HttpRequest req) =>
{
    using var reader = new StreamReader(req.Body);
    var message = await reader.ReadToEndAsync();
    await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", message);
    return Results.Ok(new { sent = true });
});

// === REST: СЃРІРµС‡Рё С‡РµСЂРµР· Finam API ===
app.MapGet("/api/candles", async (TradingService svc, string ticker, int tf, int days) =>
{
    try
    {
        
        // Finam REST
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Ok(Array.Empty<object>());
        var sym = ToFinamSymbol(ticker);
        var timeframe = tf switch { 1 => "TIME_FRAME_M1", 5 => "TIME_FRAME_M5", 15 => "TIME_FRAME_M15", 30 => "TIME_FRAME_M30", 60 => "TIME_FRAME_H1", 240 => "TIME_FRAME_H4", 1440 => "TIME_FRAME_D", _ => "TIME_FRAME_M5" };
        // Finam limits M1 history depth (~1-2 days). Cap days by TF so request never returns empty.
        var effDays = tf switch { 1 => Math.Min(Math.Max(days, 1), 2), 5 => Math.Min(Math.Max(days, 1), 10), _ => Math.Min(Math.Max(days, 1), 30) };
        var to = DateTime.UtcNow;
        var from = to.AddDays(-effDays);
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/instruments/{sym}/bars?timeframe={timeframe}&interval.start_time={from:yyyy-MM-ddTHH:mm:ssZ}&interval.end_time={to:yyyy-MM-ddTHH:mm:ssZ}");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var resp = await finamRest.SendAsync(req);
        if (resp.IsSuccessStatusCode)
        {
            var bDoc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (bDoc.RootElement.TryGetProperty("bars", out var bars))
            {
                var result = new List<object>();
                foreach (var b in bars.EnumerateArray())
                {
                    var ts = b.TryGetProperty("timestamp", out var tsEl) ? DateTimeOffset.Parse(tsEl.GetString() ?? "").ToUnixTimeSeconds() : 0;
                    var o = b.TryGetProperty("open", out var oEl) && oEl.TryGetProperty("value", out var ov) ? double.Parse(ov.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                    var h = b.TryGetProperty("high", out var hEl) && hEl.TryGetProperty("value", out var hv) ? double.Parse(hv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                    var l = b.TryGetProperty("low", out var lEl) && lEl.TryGetProperty("value", out var lv) ? double.Parse(lv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                    var c = b.TryGetProperty("close", out var cEl) && cEl.TryGetProperty("value", out var cv) ? double.Parse(cv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                    var v = b.TryGetProperty("volume", out var vEl) && vEl.TryGetProperty("value", out var vv) ? double.Parse(vv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                    if (ts > 0) result.Add(new { t = ts, o, h, l, c, v });
                }
                return Results.Ok(result);
            }
        }
        return Results.Ok(Array.Empty<object>());
    }
    catch (Exception ex) { return Results.BadRequest(new { error = ex.Message }); }
});




// === Backtest API (Python) ===
app.MapPost("/api/backtest", async (HttpRequest req) =>
{
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var scriptPath = "/root/.openclaw/workspace/HedgeFund/backtest/src/backtest_api.py";
        if (!File.Exists(scriptPath)) return Results.Json(new { error = "backtest_api.py not found" }, statusCode: 404);
        var psi = new System.Diagnostics.ProcessStartInfo {
            FileName = "python3",
            Arguments = $"\"{scriptPath}\"",
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        using var proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return Results.Json(new { error = "Failed to start python" }, statusCode: 500);
        await proc.StandardInput.WriteAsync(body);
        proc.StandardInput.Close();
        var output = await proc.StandardOutput.ReadToEndAsync();
        var err = await proc.StandardError.ReadToEndAsync();
        await proc.WaitForExitAsync();
        if (proc.ExitCode != 0) return Results.Json(new { error = err.Length > 500 ? err[..500] : err }, statusCode: 500);
        return Results.Text(output, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
});

// === VP Scalp Grid backtest ===
app.MapPost("/api/vp-backtest", async (HttpRequest req) =>
{
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var scriptPath = "/root/.openclaw/workspace/HedgeFund/backtest/src/vp_scalp_grid_api.py";
        if (!File.Exists(scriptPath)) return Results.Json(new { error = "vp_scalp_grid_api.py not found" }, statusCode: 404);
        var psi = new System.Diagnostics.ProcessStartInfo {
            FileName = "python3",
            Arguments = $"\"{scriptPath}\"",
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        using var proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return Results.Json(new { error = "Failed to start python" }, statusCode: 500);
        await proc.StandardInput.WriteAsync(body);
        proc.StandardInput.Close();
        var output = await proc.StandardOutput.ReadToEndAsync();
        var err = await proc.StandardError.ReadToEndAsync();
        await proc.WaitForExitAsync();
        if (proc.ExitCode != 0) return Results.Json(new { error = err.Length > 500 ? err[..500] : err }, statusCode: 500);
        return Results.Text(output, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }, statusCode: 500); }
}).AllowAnonymous();

// === Unified Data Provider API ===
app.MapGet("/transaq/health", (TradingService svc) =>
{
    var finamOk = svc.Connector?.IsConnected == true;
    return Results.Json(new
    {
        finam = new { status = finamOk ? "connected" : "not_connected", broker = "Finam Trade API (gRPC)" },
        transaq = new { status = "not_connected", broker = "Transaq (not configured)" },
        timestamp = DateTime.UtcNow.ToString("o")
    });
});

app.MapGet("/api/quotes", async () =>
{
    // Finam: batch all tickers
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new { source = "Finam", data = "[]" });
        var tickers = new[] { "SiU6", "SiZ6", "MXU6", "GDU6", "BRU6", "RIU6" };
        var result = new List<object>();
        foreach (var t in tickers)
        {
            try
            {
                var sym = ToFinamSymbol(t);
                using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/instruments/{sym}/quotes/latest");
                req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
                var resp = await finamRest.SendAsync(req);
                if (resp.IsSuccessStatusCode)
                {
                    var qDoc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
                    if (qDoc.RootElement.TryGetProperty("quote", out var q))
                    {
                        var bid = q.TryGetProperty("bid", out var bEl) && bEl.TryGetProperty("value", out var bv) ? double.Parse(bv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                        var ask = q.TryGetProperty("ask", out var aEl) && aEl.TryGetProperty("value", out var av) ? double.Parse(av.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                        var last = q.TryGetProperty("last", out var lEl) && lEl.TryGetProperty("value", out var lv) ? double.Parse(lv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                        var volume = q.TryGetProperty("volume", out var vEl) && vEl.TryGetProperty("value", out var vv) ? double.Parse(vv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                        result.Add(new { ticker = t, bid, ask, last, volume });
                    }
                }
            }
            catch { }
        }
        return Results.Json(new { source = "Finam", data = result });
    }
    catch { }
    return Results.Json(new { source = "Finam", data = "[]" });
});

app.MapGet("/api/balance", () =>
{
    return Results.Json(new { source = "Finam", note = "Finam REST via /api/accounts" });
});

app.MapGet("/api/orderbook", async (string ticker) =>
{
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new { rows = Array.Empty<object>() });
        var sym = ToFinamSymbol(ticker);
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/instruments/{sym}/orderbook");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var resp = await finamRest.SendAsync(req);
        if (!resp.IsSuccessStatusCode)
        {
            Console.WriteLine($"[OB] Finam {(int)resp.StatusCode} for {sym}");
            return Results.Json(new { rows = Array.Empty<object>() });
        }
        var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
        var rows = new List<object>();
        if (doc.RootElement.TryGetProperty("orderbook", out var ob) && ob.TryGetProperty("rows", out var rb))
        {
            foreach (var row in rb.EnumerateArray())
            {
                var price = row.TryGetProperty("price", out var pEl) && pEl.TryGetProperty("value", out var pv) ? double.Parse(pv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                var bid = row.TryGetProperty("buy_size", out var bEl) && bEl.TryGetProperty("value", out var bv) ? double.Parse(bv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                var ask = row.TryGetProperty("sell_size", out var sEl) && sEl.TryGetProperty("value", out var sv) ? double.Parse(sv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0;
                rows.Add(new { price, bid, ask });
            }
        }
        return Results.Json(new { rows });
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[OB] error: {ex.Message}");
        return Results.Json(new { rows = Array.Empty<object>() });
    }
});

app.MapGet("/api/orderbook/{ticker}", async (string ticker) =>
{
    // Redirect legacy route to query handler
    return Results.Json(new { rows = Array.Empty<object>(), note = "use /api/orderbook?ticker=" });
});

app.MapGet("/api/ohlcv", (string? ticker) =>
{
    lock (candleBuilderLock)
    {
        var candles = candleBuilderHistory.ToList();
        if (candleBuilderCurrent != null) candles.Add(candleBuilderCurrent);
        return Results.Json(new { tf = CANDLE_TF_MINUTES, count = candles.Count, candles = candles.Select(c => new { t = (long)c[0], o = c[1], h = c[2], l = c[3], c = c[4], v = c[5] }) });
    }
});



// === REST: РєРѕС‚РёСЂРѕРІРєРё С‡РµСЂРµР· Finam API ===
// === Accounts API (Finam) ===
app.MapGet("/api/accounts", async () =>
{
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new { error = "no JWT" });

        var accList = new List<object>();
        // Account IDs: 1225953 (FORTS/Main), 2049688 (UNION/EDP)
        var accountIds = new[] { _finamAccountId, "2049688" };
        foreach (var accId in accountIds)
        {
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/accounts/{accId}");
                req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
                var resp = await finamRest.SendAsync(req);
                if (!resp.IsSuccessStatusCode) continue;
                var aDoc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
                var equity = aDoc.RootElement.TryGetProperty("equity", out var eqEl) && eqEl.TryGetProperty("value", out var eqV) ? double.Parse(eqV.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var unrealizedPnl = aDoc.RootElement.TryGetProperty("unrealized_profit", out var upEl) && upEl.TryGetProperty("value", out var upV) ? double.Parse(upV.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var accType = aDoc.RootElement.TryGetProperty("type", out var tEl) ? tEl.GetString() : "";
                var name = accType == "UNION" ? "EDP" : "Main";
                accList.Add(new { id = accId, name, balance = equity, free = equity, margin = 0.0, go = 0.0, pnlToday = unrealizedPnl, pnlTotal = unrealizedPnl });
            }
            catch { }
        }
        return Results.Json(new { accounts = accList });
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
});

// === Positions API (unified) ===
app.MapGet("/api/positions", async (TradingService svc) =>
{
    
    // Finam REST
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new object[] {});
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/accounts/{_finamAccountId}");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var resp = await finamRest.SendAsync(req);
        if (resp.IsSuccessStatusCode)
        {
            var aDoc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (aDoc.RootElement.TryGetProperty("positions", out var positions))
            {
                var arr = new List<object>();
                foreach (var p in positions.EnumerateArray())
                {
                    // EDP/UNION отдаёт количество дробной строкой ("173.0") — парсим как double
                    double qtyD = 0;
                    var qtyOk = p.TryGetProperty("quantity", out var qEl) && qEl.TryGetProperty("value", out var qv)
                                && double.TryParse(qv.GetString(), System.Globalization.NumberStyles.Any,
                                                   System.Globalization.CultureInfo.InvariantCulture, out qtyD);
                    var qty = qtyOk ? (int)Math.Round(qtyD) : 0;
                    if (qty == 0) continue;
                    arr.Add(new {
                        ticker = p.TryGetProperty("symbol", out var sym) ? sym.GetString()?.Split('@')[0] : "",
                        dir = qty > 0 ? "Buy" : "Sell",
                        qty = Math.Abs(qty),
                        avgPrice = p.TryGetProperty("current_price", out var cpEl) && cpEl.TryGetProperty("value", out var cpv) ? double.Parse(cpv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0,
                        pnlToday = p.TryGetProperty("unrealized_profit", out var upEl) && upEl.TryGetProperty("value", out var upv) ? double.Parse(upv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0
                    });
                }
                return Results.Json(arr);
            }
        }
    }
    catch { }
    return Results.Json(new object[] {});
});

// === Orders API (unified) ===
app.MapGet("/api/orders", async (TradingService svc) =>
{
    
    // Finam REST вЂ” orders via account
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new object[] {});
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/accounts/{_finamAccountId}/orders");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var resp = await finamRest.SendAsync(req);
        if (resp.IsSuccessStatusCode)
        {
            var body = await resp.Content.ReadAsStringAsync();
            return Results.Text(body, "application/json");
        }
    }
    catch { }
    return Results.Json(new object[] {});
});

// === Trades API ===
app.MapGet("/api/trades", async (string? date, string? dateFrom, string? dateTo) =>
{
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Json(new object[] {});
        string startTime, endTime;
        if (!string.IsNullOrEmpty(dateFrom) || !string.IsNullOrEmpty(dateTo))
        {
            var start = !string.IsNullOrEmpty(dateFrom) ? dateFrom : DateTime.UtcNow.ToString("yyyy-MM-dd");
            var endDt = !string.IsNullOrEmpty(dateTo) ? DateTime.TryParse(dateTo, out var p) ? p.AddDays(1).ToString("yyyy-MM-dd") : dateTo : DateTime.UtcNow.AddDays(1).ToString("yyyy-MM-dd");
            startTime = $"{start}T00:00:00Z";
            endTime = $"{endDt}T00:00:00Z";
        }
        else
        {
            var targetDate = !string.IsNullOrEmpty(date) ? date : DateTime.UtcNow.ToString("yyyy-MM-dd");
            var endDate = DateTime.TryParse(targetDate, out var parsed) ? parsed.AddDays(1).ToString("yyyy-MM-dd") : targetDate;
            startTime = $"{targetDate}T00:00:00Z";
            endTime = $"{endDate}T00:00:00Z";
        }
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/accounts/{_finamAccountId}/trades?interval.start_time={startTime}&interval.end_time={endTime}");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var tradesResp = await finamRest.SendAsync(req);
        Console.WriteLine($"[DEBUG] Trades response: status={tradesResp.StatusCode} url={req.RequestUri}");
        if (tradesResp.IsSuccessStatusCode)
        {
            var body = await tradesResp.Content.ReadAsStringAsync();
            return Results.Text(body, "application/json");
        }
    }
    catch { }
    return Results.Json(new object[] {});
}).AllowAnonymous();

app.MapGet("/api/quote", async (string ticker) =>
{
    // === Connector isolation ===
    
    // Finam REST
    try
    {
        var jwt = await GetFinamJwt();
        if (string.IsNullOrEmpty(jwt)) return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, source = "Finam (no JWT)" });
        var sym = ToFinamSymbol(ticker);
        var resp = await finamRest.GetAsync($"https://api.finam.ru/v1/instruments/{sym}/quotes/latest");
        resp.Headers.Add("Authorization", jwt);
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.finam.ru/v1/instruments/{sym}/quotes/latest");
        req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", jwt);
        var qResp = await finamRest.SendAsync(req);
        if (qResp.IsSuccessStatusCode)
        {
            var qDoc = JsonDocument.Parse(await qResp.Content.ReadAsStringAsync());
            if (qDoc.RootElement.TryGetProperty("quote", out var q))
            {
                var bid = q.TryGetProperty("bid", out var bEl) && bEl.TryGetProperty("value", out var bv) ? double.Parse(bv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var ask = q.TryGetProperty("ask", out var aEl) && aEl.TryGetProperty("value", out var av) ? double.Parse(av.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var last = q.TryGetProperty("last", out var lEl) && lEl.TryGetProperty("value", out var lv) ? double.Parse(lv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var volume = q.TryGetProperty("volume", out var vEl) && vEl.TryGetProperty("value", out var vv) ? double.Parse(vv.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                var change = q.TryGetProperty("change", out var cEl) && cEl.TryGetProperty("value", out var cv2) ? double.Parse(cv2.GetString() ?? "0", System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                return Results.Ok(new { bid, ask, last, volume, change, spread = ask > 0 && bid > 0 ? ask - bid : 0.0, source = "Finam" });
            }
        }
    }
    catch { }
    return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, source = "Finam (error)" });
});

app.MapPost("/strategy/grid-mm/start", async (TradingService tradingSvc, HttpRequest req) =>
{
    try
    {
        var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (string.IsNullOrEmpty(token))
            return Results.Json(new { error = "FINAM_TOKEN not set" }, statusCode: 400);
        if (gridMm != null)
            return Results.Json(new { status = "already_running", detail = gridMm.GetStatus() });
        
        // Read forceEntryOnStart from body
        bool forceEntry = false;
        try {
            using var reader = new StreamReader(req.Body);
            var body = await reader.ReadToEndAsync();
            if (!string.IsNullOrEmpty(body)) {
                var doc = JsonDocument.Parse(body);
                if (doc.RootElement.TryGetProperty("forceEntryOnStart", out var fe))
                    forceEntry = fe.GetBoolean();
            }
        } catch {}
        
        gridMm = new GridMmRegimeLauncher(tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected. POST /connect-broker first"), "SiU6", accountId: _finamAccountId, forceEntryOnStart: forceEntry);
        return Results.Json(new { status = "initialized", detail = gridMm.GetStatus() });
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message }, statusCode: 500);
    }
});

app.MapPost("/strategy/grid-mm/run", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized. POST /strategy/grid-mm/start first" }, statusCode: 400);
    gridMm.Start();
    return Results.Json(new { status = "running", detail = gridMm.GetStatus() });
});

app.MapPost("/strategy/grid-mm/stop", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    var status = gridMm.GetStatus();
    gridMm.StopTrading();
    gridMm.Dispose();
    gridMm = null;
    return Results.Json(new { status = "stopped", detail = status });
});

app.MapPost("/strategy/grid-mm/pause", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    gridMm.Pause();
    return Results.Json(new { status = "paused", detail = gridMm.GetStatus() });
});

app.MapGet("/strategy/grid-mm/status", () =>
{
    if (gridMm == null) return Results.Json(new { status = "not_initialized" });
    return Results.Json(new { status = "ok", detail = gridMm.GetStatus(), connected = gridMm.IsConnected });
});

app.MapPost("/strategy/grid-mm/config", async (HttpRequest req) =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    try
    {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var doc = JsonDocument.Parse(body);
        var root = doc.RootElement;
        
        if (root.TryGetProperty("sarStart", out var sarStart)) gridMm.Strategy.Params.SarStart = sarStart.GetDouble();
        if (root.TryGetProperty("sarStep", out var sarStep)) gridMm.Strategy.Params.SarStep = sarStep.GetDouble();
        if (root.TryGetProperty("sarMax", out var sarMax)) gridMm.Strategy.Params.SarMax = sarMax.GetDouble();
        if (root.TryGetProperty("emaPeriod", out var emaPeriod)) gridMm.Strategy.Params.EmaPeriod = emaPeriod.GetInt32();
        if (root.TryGetProperty("gridStep", out var gridStep)) gridMm.Strategy.Params.GridStep = gridStep.GetDouble();
        if (root.TryGetProperty("gridSpread", out var gridSpread)) gridMm.Strategy.Params.GridSpread = gridSpread.GetDouble();
        if (root.TryGetProperty("maxGridLevels", out var maxGrid)) { gridMm.Strategy.Params.MaxGridLevels = maxGrid.GetInt32(); gridMm.Strategy.ResizeGrid(); }
        if (root.TryGetProperty("commission", out var comm)) gridMm.Strategy.Params.Commission = comm.GetDouble();
        if (root.TryGetProperty("maxLots", out var maxLots)) gridMm.Strategy.Params.MaxLots = maxLots.GetInt32();
        if (root.TryGetProperty("forceEntryOnStart", out var forceEntry)) gridMm.Strategy.Params.ForceEntryOnStart = forceEntry.GetBoolean();
        
        Console.WriteLine($"[CONFIG] Updated: ForceEntry={gridMm.Strategy.Params.ForceEntryOnStart}");
        return Results.Ok(new { status = "ok" });
    }
    catch (Exception ex)
    {
        return Results.BadRequest(new { error = ex.Message });
    }
});

app.MapGet("/strategy/grid-mm/indicators", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    var s = gridMm.Strategy;
    return Results.Json(new {
        sar = s.CurrentSar,
        ema = s.CurrentEma,
        posDir = s.PositionDirection,
        entryPrice = s.EntryPrice,
        lots = s.CurrentLotLevel,
        totalLots = s.TotalEntryLots,
        trades = s.Trades.Select(t => new { time = t.Time.ToString("o"), t.Ticker, t.Direction, t.Price, t.Lots, t.Comment })
    });
});

app.MapGet("/strategy/grid-mm/trades", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    return Results.Json(gridMm.Strategy.Trades.Select(t => new {
        time = t.Time.ToString("o"),
        t.Ticker,
        dir = t.Direction == 1 ? "BUY" : "SELL",
        t.Price,
        t.Lots,
        t.Comment
    }));
});

app.MapGet("/strategy/grid-mm/chart-data", () =>
{
    if (gridMm == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    var s = gridMm.Strategy;
    var hist = s.IndicatorHistory;
    var trades = s.Trades;
    return Results.Json(new
    {
        indicators = hist.Select(p => new { time = p.Time.ToString("o"), sar = p.Sar, ema = p.Ema }),
        trades = trades.Select(t => new { time = t.Time.ToString("o"), dir = t.Direction, price = t.Price, lots = t.Lots, comment = t.Comment }),
        current = new
        {
            sar = s.CurrentSar,
            ema = s.CurrentEma,
            posDir = s.PositionDirection,
            entryPrice = s.EntryPrice,
            lots = s.CurrentLotLevel,
            totalPnl = s.TotalPnL,
            totalTrades = s.TotalTrades
        }
    });
});

// NOTE: NoSignal Рё PSAR Grid вЂ” СѓРґР°Р»РµРЅС‹. РСЃРїРѕР»СЊР·СѓРµРј Grid MM Regime.


// === Grid MM V8 (Trend-following) ===
GridMmV7Launcher? gridMmV8 = null;

app.MapPost("/strategy/grid-mm-v8/start", async (TradingService tradingSvc, HttpRequest req) =>
{
    if (gridMmV8 != null)
        return Results.Json(new { status = "already_running", detail = gridMmV8.GetStatus() });
    
    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    
    // Determine instrument from request body
    string v8Instrument = "SiU6";
    string? bodyRaw = null;
    try { bodyRaw = await new StreamReader(req.Body).ReadToEndAsync(); } catch {}
    if (!string.IsNullOrEmpty(bodyRaw))
    {
        try {
            var docPre = System.Text.Json.JsonDocument.Parse(bodyRaw);
            if (docPre.RootElement.TryGetProperty("instrument", out var instrEl))
                v8Instrument = instrEl.GetString() ?? "SiU6";
        } catch {}
    }
    
    var v8Config = new GridMmV7Strategy.Config { InvertedLogic = true };
    
    try
    {
        if (!string.IsNullOrEmpty(bodyRaw))
        {
            var doc = System.Text.Json.JsonDocument.Parse(bodyRaw);
            var root = doc.RootElement;
            if (root.TryGetProperty("sarStart", out var v)) v8Config.SarStart = v.GetDouble();
            if (root.TryGetProperty("sarStep", out v)) v8Config.SarStep = v.GetDouble();
            if (root.TryGetProperty("sarMax", out v)) v8Config.SarMax = v.GetDouble();
            if (root.TryGetProperty("emaPeriod", out v)) v8Config.EmaPeriod = v.GetInt32();
            if (root.TryGetProperty("gridStep", out v)) v8Config.GridStep = v.GetDouble();
            if (root.TryGetProperty("gridSpread", out v)) v8Config.GridSpread = v.GetDouble();
            if (root.TryGetProperty("maxGridLevels", out v)) v8Config.MaxGridLevels = v.GetInt32();
            if (root.TryGetProperty("maxLots", out v)) v8Config.MaxLots = v.GetInt32();
            if (root.TryGetProperty("stdPeriod", out v)) v8Config.StdPeriod = v.GetInt32();
            if (root.TryGetProperty("stdMult", out v)) v8Config.StdMult = v.GetDouble();
            if (root.TryGetProperty("gridHold", out v)) v8Config.GridHold = v.GetBoolean();
        }
    }
    catch { }
    
    gridMmV8 = new GridMmV8Launcher(broker, v8Config);
    gridMmV8.SetRiskGate(riskGate);
    if (!string.IsNullOrEmpty(v8Instrument) && v8Instrument != "SiM6")
        gridMmV8.SetInstrument(v8Instrument);
    
    try {
        var startTask = gridMmV8.StartAsync();
        if (await Task.WhenAny(startTask, Task.Delay(5000)) != startTask)
            return Results.Json(new { status = "error", error = "Start timeout" });
    } catch (Exception ex) { return Results.Json(new { status = "error", error = ex.Message }); }
    return Results.Json(new { status = "running", detail = gridMmV8.GetStatus() });
});

app.MapPost("/strategy/grid-mm-v8/stop", async () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await gridMmV8.StopAsync();
    var status = gridMmV8.GetStatus();
    gridMmV8.Dispose(); gridMmV8 = null;
    return Results.Json(new { status = "stopped", detail = status });
});

app.MapPost("/strategy/grid-mm-v8/pause", async () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await gridMmV8.PauseAsync();
    return Results.Json(new { status = "paused", detail = gridMmV8.GetStatus() });
});

app.MapPost("/strategy/grid-mm-v8/resume", async () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await gridMmV8.ResumeAsync();
    return Results.Json(new { status = "running", detail = gridMmV8.GetStatus() });
});

app.MapGet("/strategy/grid-mm-v8/status", () =>
{
    if (gridMmV8 == null) return Results.Json(new { status = "not_initialized" });
    var s = gridMmV8.Strategy;
    return Results.Json(new {
        status = s.CurrentMode.ToString(),
        posDir = s.PositionDirection, entryPrice = s.EntryPrice,
        lots = s.TotalLots, filledLevels = s.FilledLevels,
        roundTrips = s.RoundTrips, pnl = s.TotalPnL,
        sar = s.CurrentSar, ema = s.CurrentEma, std = s.CurrentStd,
        level = s.CurrentLevel, connected = true,
        detail = gridMmV8.GetStatus()
    });
});

app.MapPost("/strategy/grid-mm-v8/force-entry", () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    gridMmV8.Strategy.ForceEntry(0); // С†РµРЅР° РѕР±РЅРѕРІРёС‚СЃСЏ РїСЂРё РёСЃРїРѕР»РЅРµРЅРёРё
    return Results.Json(new { status = "ok", detail = gridMmV8.GetStatus() });
});

app.MapPost("/strategy/grid-mm-v8/buy", async () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await gridMmV8.MarketBuyAsync();
    return Results.Json(new { status = "ok" });
});

app.MapPost("/strategy/grid-mm-v8/sell", async () =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await gridMmV8.MarketSellAsync();
    return Results.Json(new { status = "ok" });
});

app.MapPost("/strategy/grid-mm-v8/config", async (HttpRequest req) =>
{
    if (gridMmV8 == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    var body = await new StreamReader(req.Body).ReadToEndAsync();
    var doc = System.Text.Json.JsonDocument.Parse(body);
    var root = doc.RootElement;
    var p = gridMmV8.Strategy.Params;
    if (root.TryGetProperty("sarStart", out var v)) p.SarStart = v.GetDouble();
    if (root.TryGetProperty("sarStep", out v)) p.SarStep = v.GetDouble();
    if (root.TryGetProperty("sarMax", out v)) p.SarMax = v.GetDouble();
    if (root.TryGetProperty("emaPeriod", out v)) p.EmaPeriod = v.GetInt32();
    if (root.TryGetProperty("gridStep", out v)) p.GridStep = v.GetDouble();
    if (root.TryGetProperty("gridSpread", out v)) p.GridSpread = v.GetDouble();
    if (root.TryGetProperty("maxGridLevels", out v)) p.MaxGridLevels = v.GetInt32();
    if (root.TryGetProperty("maxLots", out v)) p.MaxLots = v.GetInt32();
    if (root.TryGetProperty("stdPeriod", out v)) p.StdPeriod = v.GetInt32();
    if (root.TryGetProperty("stdMult", out v)) p.StdMult = v.GetDouble();
    if (root.TryGetProperty("gridHold", out v)) p.GridHold = v.GetBoolean();
    return Results.Json(new { status = "ok", config = new { p.SarStart, p.SarStep, p.SarMax, p.EmaPeriod, p.GridStep, p.GridSpread, p.MaxGridLevels, p.StdPeriod, p.StdMult, p.GridHold }});
});

// === VP Scalp Grid ===
VpScalpGridLauncher? vpScalpGrid = null;

// === VP Scalp Grid Copy ===
VpScalpGridCopyLauncher? vpCopyLauncher = null;

// === Fade Impulse ===
FadeImpulseLauncher? fadeImpulseLauncher = null;

// === V8 Trail ===
V8TrailLauncher? v8TrailLauncher = null;

// === VP Scalp Simple ===
VpScalpSimpleLauncher? vpScalpSimpleLauncher = null;

app.MapPost("/strategy/vp-scalp-grid/start", async (TradingService tradingSvc, HttpRequest req) =>
{
    if (vpScalpGrid != null)
        return Results.Json(new { status = "already_running", detail = vpScalpGrid.GetStatus() });

    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    var config = new VpScalpGridStrategy.Config();

    string? bodyRaw = null;
    try { bodyRaw = await new StreamReader(req.Body).ReadToEndAsync(); } catch {}
    if (!string.IsNullOrEmpty(bodyRaw))
    {
        try {
            var doc = System.Text.Json.JsonDocument.Parse(bodyRaw);
            var root = doc.RootElement;
            if (root.TryGetProperty("maxLevels", out var v)) config.MaxLevels = v.GetInt32();
            if (root.TryGetProperty("stepBase", out v)) config.StepBase = v.GetInt32();
            if (root.TryGetProperty("spreadBase", out v)) config.SpreadBase = v.GetInt32();
            if (root.TryGetProperty("maxHoldMinutes", out v)) config.MaxHoldMinutes = v.GetInt32();
            if (root.TryGetProperty("vpLookback", out v)) config.VpLookback = v.GetInt32();
            if (root.TryGetProperty("vpBinSize", out v)) config.VpBinSize = v.GetInt32();
            if (root.TryGetProperty("vaPercent", out v)) config.VaPercent = v.GetDouble();
            if (root.TryGetProperty("rvAdaptation", out v)) config.RvAdaptation = v.GetBoolean();
            if (root.TryGetProperty("minProfitPerLot", out v)) config.MinProfitPerLot = v.GetInt32();
        } catch {}
    }

    var vpStrategy = new VpScalpGridStrategy(config);
    vpScalpGrid = new VpScalpGridLauncher(broker, vpStrategy);
    vpScalpGrid.SetRiskGate(riskGate);

    try {
        var startTask = vpScalpGrid.StartAsync();
        if (await Task.WhenAny(startTask, Task.Delay(5000)) != startTask)
            return Results.Json(new { status = "error", error = "Start timeout" });
    } catch (Exception ex) { return Results.Json(new { status = "error", error = ex.Message }); }
    return Results.Json(new { status = "running", detail = vpScalpGrid.GetStatus() });
});

app.MapPost("/strategy/vp-scalp-grid/stop", async () =>
{
    if (vpScalpGrid == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpScalpGrid.StopAsync();
    var status = vpScalpGrid.GetStatus();
    vpScalpGrid = null;
    return Results.Json(new { status = "stopped", detail = status });
});

app.MapPost("/strategy/vp-scalp-grid/pause", async () =>
{
    if (vpScalpGrid == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpScalpGrid.PauseAsync();
    return Results.Json(new { status = "paused", detail = vpScalpGrid.GetStatus() });
});

app.MapPost("/strategy/vp-scalp-grid/resume", async () =>
{
    if (vpScalpGrid == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpScalpGrid.ResumeAsync();
    return Results.Json(new { status = "running", detail = vpScalpGrid.GetStatus() });
});

app.MapGet("/strategy/vp-scalp-grid/status", () =>
{
    if (vpScalpGrid == null) return Results.Json(new { status = "stopped" });
    return Results.Json(vpScalpGrid.GetStatus());
});

app.MapPost("/strategy/vp-scalp-grid/config", async (HttpRequest req) =>
{
    if (vpScalpGrid == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    var body = await new StreamReader(req.Body).ReadToEndAsync();
    var doc = System.Text.Json.JsonDocument.Parse(body);
    var root = doc.RootElement;
    var p = vpScalpGrid.Strategy.Params;
    if (root.TryGetProperty("maxLevels", out var v)) p.MaxLevels = v.GetInt32();
    if (root.TryGetProperty("stepBase", out v)) p.StepBase = v.GetInt32();
    if (root.TryGetProperty("spreadBase", out v)) p.SpreadBase = v.GetInt32();
    if (root.TryGetProperty("maxHoldMinutes", out v)) p.MaxHoldMinutes = v.GetInt32();
    if (root.TryGetProperty("vpLookback", out v)) p.VpLookback = v.GetInt32();
    if (root.TryGetProperty("vpBinSize", out v)) p.VpBinSize = v.GetInt32();
    if (root.TryGetProperty("vaPercent", out v)) p.VaPercent = v.GetDouble();
    if (root.TryGetProperty("rvAdaptation", out v)) p.RvAdaptation = v.GetBoolean();
    if (root.TryGetProperty("minProfitPerLot", out v)) p.MinProfitPerLot = v.GetInt32();
    return Results.Json(new { status = "ok" });
});

// === VP Scalp Grid Copy ===
app.MapPost("/strategy/vp-copy/start", async (TradingService tradingSvc, HttpRequest req) =>
{
    if (vpCopyLauncher != null)
        return Results.Json(new { status = "already_running", detail = vpCopyLauncher.GetStatus() });

    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    var config = new VpScalpGridCopyStrategy.Config();

    string? bodyRaw = null;
    try { bodyRaw = await new StreamReader(req.Body).ReadToEndAsync(); } catch {}
    if (!string.IsNullOrEmpty(bodyRaw))
    {
        try {
            var doc = System.Text.Json.JsonDocument.Parse(bodyRaw);
            var root = doc.RootElement;
            if (root.TryGetProperty("maxLevels", out var v)) config.MaxLevels = v.GetInt32();
            if (root.TryGetProperty("stepBase", out v)) config.StepBase = v.GetInt32();
            if (root.TryGetProperty("spreadBase", out v)) config.SpreadBase = v.GetInt32();
            if (root.TryGetProperty("maxHoldMinutes", out v)) config.MaxHoldMinutes = v.GetInt32();
            if (root.TryGetProperty("vpLookback", out v)) config.VpLookback = v.GetInt32();
            if (root.TryGetProperty("vpBinSize", out v)) config.VpBinSize = v.GetInt32();
            if (root.TryGetProperty("vaPercent", out v)) config.VaPercent = v.GetDouble();
            if (root.TryGetProperty("rvAdaptation", out v)) config.RvAdaptation = v.GetBoolean();
        } catch {}
    }

    var vpCopyStrategy = new VpScalpGridCopyStrategy(config);
    vpCopyLauncher = new VpScalpGridCopyLauncher(broker, vpCopyStrategy);
    vpCopyLauncher.SetRiskGate(riskGate);

    try {
        var startTask = vpCopyLauncher.StartAsync();
        if (await Task.WhenAny(startTask, Task.Delay(5000)) != startTask)
            return Results.Json(new { status = "error", error = "Start timeout" });
    } catch (Exception ex) { return Results.Json(new { status = "error", error = ex.Message }); }
    return Results.Json(new { status = "running", detail = vpCopyLauncher.GetStatus() });
});

app.MapPost("/strategy/vp-copy/stop", async () =>
{
    if (vpCopyLauncher == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpCopyLauncher.StopAsync();
    var status = vpCopyLauncher.GetStatus();
    vpCopyLauncher.Dispose();
    vpCopyLauncher = null;
    return Results.Json(new { status = "stopped", detail = status });
});

app.MapPost("/strategy/vp-copy/pause", async () =>
{
    if (vpCopyLauncher == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpCopyLauncher.PauseAsync();
    return Results.Json(new { status = "paused", detail = vpCopyLauncher.GetStatus() });
});

app.MapPost("/strategy/vp-copy/resume", async () =>
{
    if (vpCopyLauncher == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    await vpCopyLauncher.ResumeAsync();
    return Results.Json(new { status = "running", detail = vpCopyLauncher.GetStatus() });
});

app.MapGet("/strategy/vp-copy/status", () =>
{
    if (vpCopyLauncher == null) return Results.Json(new { status = "stopped" });
    return Results.Json(vpCopyLauncher.GetStatus());
});

app.MapPost("/strategy/vp-copy/config", async (HttpRequest req) =>
{
    if (vpCopyLauncher == null) return Results.Json(new { error = "Not running" }, statusCode: 400);
    var body = await new StreamReader(req.Body).ReadToEndAsync();
    var doc = System.Text.Json.JsonDocument.Parse(body);
    var root = doc.RootElement;
    var p = vpCopyLauncher.Strategy.Params;
    if (root.TryGetProperty("maxLevels", out var v)) p.MaxLevels = v.GetInt32();
    if (root.TryGetProperty("stepBase", out v)) p.StepBase = v.GetInt32();
    if (root.TryGetProperty("spreadBase", out v)) p.SpreadBase = v.GetInt32();
    if (root.TryGetProperty("maxHoldMinutes", out v)) p.MaxHoldMinutes = v.GetInt32();
    if (root.TryGetProperty("vpLookback", out v)) p.VpLookback = v.GetInt32();
    if (root.TryGetProperty("vpBinSize", out v)) p.VpBinSize = v.GetInt32();
    if (root.TryGetProperty("vaPercent", out v)) p.VaPercent = v.GetDouble();
    if (root.TryGetProperty("rvAdaptation", out v)) p.RvAdaptation = v.GetBoolean();
    return Results.Json(new { status = "ok" });
});

app.MapGet("/api/active-strategies", () =>
{
    var strategies = new List<object>();
    if (gridMm != null)
    {
        var s = gridMm.Strategy;
        strategies.Add(new {
            id = "grid-mm", name = "Grid MM v6", instrument = "SiM6", tf = "5 РјРёРЅ",
            mode = s.Mode.ToString(), posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.CurrentLotLevel, openLots = s.OpenLots, filledGrid = s.FilledGridLevels,
            totalTrades = s.TotalTrades, totalPnL = s.TotalPnL,
            sar = s.CurrentSar, ema = s.CurrentEma, connected = gridMm.IsConnected,
            detail = gridMm.GetStatus()
        });
    }
    {
        var s = volRev.Strategy;
        strategies.Add(new {
            id = "vol-rev", name = "Volume Reversal", instrument = "SiM6", tf = "5 РјРёРЅ",
            mode = s.PositionDirection != 0 ? "Running" : "Waiting",
            posDir = s.PositionDirection, entryPrice = 0.0, lots = 0, openLots = 0, filledGrid = 0,
            totalTrades = s.TotalTrades, totalPnL = s.TotalPnL,
            sar = 0.0, ema = 0.0, connected = false, detail = volRev.GetStatus()
        });
    }
    if (gridMmV8 != null)
    {
        var s = gridMmV8.Strategy;
        strategies.Add(new {
            id = "grid-mm-v8", name = "Grid MM v8 (Trend)", instrument = gridMmV8.Ticker, tf = "5 РјРёРЅ",
            mode = s.CurrentMode.ToString(), posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.TotalLots, openLots = s.TotalLots, filledGrid = s.FilledLevels,
            totalTrades = s.RoundTrips, totalPnL = s.TotalPnL,
            sar = s.CurrentSar, ema = s.CurrentEma, connected = true,
            detail = gridMmV8.GetStatus()
        });
    }
    if (vpScalpGrid != null)
    {
        var s = vpScalpGrid.Strategy;
        strategies.Add(new {
            id = "vp-scalp-grid", name = "VP Scalp Grid", instrument = "SiM6", tf = "1 РјРёРЅ",
            mode = s.CurrentMode.ToString(), posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.TotalLots, openLots = s.TotalLots, filledGrid = s.FilledLevels,
            totalTrades = 0, totalPnL = s.RealizedPnL,
            sar = 0.0, ema = 0.0, connected = true,
            detail = vpScalpGrid.GetStatus()
        });
    }
    if (vpCopyLauncher != null)
    {
        var s = vpCopyLauncher.Strategy;
        strategies.Add(new {
            id = "vp-copy", name = "VP Scalp Grid Copy", instrument = "SiM6", tf = "1 РјРёРЅ",
            mode = s.CurrentMode.ToString(), posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.TotalLots, openLots = s.TotalLots, filledGrid = s.FilledLevels,
            totalTrades = s.RoundTrips, totalPnL = s.RealizedPnL,
            sar = 0.0, ema = 0.0, connected = true,
            detail = vpCopyLauncher.GetStatus()
        });
    }
    if (vpScalpSimpleLauncher != null)
    {
        var s = vpScalpSimpleLauncher.Strategy;
        strategies.Add(new {
            id = "vp-simple", name = "VP Scalp Simple", instrument = "MXM6", tf = "5 РјРёРЅ",
            mode = s.CurrentMode.ToString(), posDir = s.PositionDir, entryPrice = s.EntryPrice,
            lots = s.PositionDir != 0 ? 1 : 0, openLots = s.PositionDir != 0 ? 1 : 0, filledGrid = 0,
            totalTrades = s.TotalTrades, totalPnL = s.RealizedPnL,
            sar = 0.0, ema = 0.0, connected = true,
            detail = vpScalpSimpleLauncher.GetStatus()
        });
    }
    if (v8TrailLauncher != null)
    {
        var s = v8TrailLauncher.Strategy;
        strategies.Add(new {
            id = "v8-trail", name = "V8 Trail", instrument = v8TrailLauncher.Ticker, tf = "5 РјРёРЅ",
            mode = s.PositionDirection != 0 ? "Running" : "Waiting",
            posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.PositionDirection != 0 ? 1 : 0, openLots = s.PositionDirection != 0 ? 1 : 0, filledGrid = 0,
            totalTrades = s.TotalTrades, totalPnL = s.RealizedPnL,
            sar = s.CurrentSar, ema = s.CurrentEma, connected = true,
            detail = v8TrailLauncher.GetStatus()
        });
    }
    return Results.Json(new { strategies, count = strategies.Count });
});

app.MapPost("/strategy/vol-rev/start", (TradingService svc) =>
{
    var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
    if (string.IsNullOrEmpty(token)) return Results.Json(new { error = "FINAM_TOKEN not set" }, statusCode: 400);
    if (volRev != null) return Results.Json(new { status = "already_running", detail = volRev.GetStatus() });
    if (svc.Connector == null) return Results.Json(new { error = "Broker not connected" }, statusCode: 400);
    volRev = new VolumeReversalLauncher(svc.Connector, "RIM6");
    volRev.SetRiskGate(riskGate);
    return Results.Json(new { status = "initialized", detail = volRev.GetStatus() });
});

app.MapPost("/strategy/vol-rev/run", () =>
{
    if (volRev == null) return Results.Json(new { error = "Not initialized. POST /strategy/vol-rev/start first" }, statusCode: 400);
    volRev.Start();
    return Results.Json(new { status = "running", detail = volRev.GetStatus() });
});

app.MapPost("/strategy/vol-rev/stop", () =>
{
    if (volRev == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    volRev.StopTrading();
    volRev.Strategy.ForceClose();
    return Results.Json(new { status = "stopped", detail = volRev.GetStatus() });
});

app.MapPost("/strategy/vol-rev/pause", () =>
{
    if (volRev == null) return Results.Json(new { error = "Not initialized" }, statusCode: 400);
    volRev.Pause();
    return Results.Json(new { status = "paused", detail = volRev.GetStatus() });
});

app.MapGet("/strategy/vol-rev/status", () =>
{
    if (volRev == null) return Results.Json(new { status = "not_initialized" });
    return Results.Json(new { status = "ok", detail = volRev.GetStatus(),
        pos = volRev.Strategy.PositionDirection,
        trades = volRev.Strategy.TotalTrades,
        pnl = volRev.Strategy.TotalPnL,
        avgVol = volRev.Strategy.CurrentAvgVolume
    });
});

// === Heartbeat Monitoring (no auth) ===
app.MapGet("/heartbeat", () =>
{
    object gridMmResult, volRevResult;
    
    if (gridMm != null)
    {
        var status = gridMm.GetStatus();
        var connected = gridMm.IsConnected;
        
        // Parse status string to extract PnL
        double pnl = 0;
        int position = 0;
        string mode = "Stopped";
        
        var parts = status.Split('|');
        foreach (var part in parts)
        {
            var trimmed = part.Trim();
            if (trimmed.StartsWith("PnL="))
            {
                var pnlStr = trimmed.Substring(4).Trim();
                double.TryParse(pnlStr, out pnl);
            }
            else if (trimmed.StartsWith("Pos="))
            {
                var posStr = trimmed.Substring(4).Split(' ')[0].Trim();
                int.TryParse(posStr, out position);
            }
            else if (trimmed.StartsWith("Mode="))
            {
                mode = trimmed.Substring(5).Split(' ')[0].Trim();
            }
        }
        
        gridMmResult = new
        {
            initialized = true,
            running = mode == "Running",
            connected = connected,
            pnl = pnl,
            position = position,
            mode = mode,
            status = status
        };
    }
    else
    {
        gridMmResult = new { initialized = false };
    }
    
    if (volRev != null)
    {
        var status = volRev.GetStatus();
        var pnl = volRev.Strategy.TotalPnL;
        var pos = volRev.Strategy.PositionDirection;
        var mode = volRev.Strategy.Mode.ToString();
        
        volRevResult = new
        {
            initialized = true,
            running = mode == "Running",
            pnl = pnl,
            position = pos,
            mode = mode,
            status = status
        };
    }
    else
    {
        volRevResult = new { initialized = false };
    }
    
    var result = new
    {
        timestamp = DateTime.UtcNow,
        grid_mm = gridMmResult,
        vol_rev = volRevResult
    };
    
    return Results.Ok(result);
}).AllowAnonymous();

// === ARB REMOVED ===


// РћС‚РјРµРЅР° РІСЃРµС… Р°РєС‚РёРІРЅС‹С… РѕСЂРґРµСЂРѕРІ
app.MapPost("/api/orders/cancel-all", async () =>
{
    try
    {
        var connector = connectorMgr?.Active;
        if (connector == null)
            return Results.Json(new { error = "No active connector" }, statusCode: 400);
        
        // РџРѕР»СѓС‡Р°РµРј РѕСЂРґРµСЂР° С‡РµСЂРµР· Р»РѕРєР°Р»СЊРЅС‹Р№ API
        using var httpClient = new HttpClient();
        httpClient.BaseAddress = new Uri("http://localhost:5050");
        var ordersResp = await httpClient.GetAsync("/api/orders");
        var ordersJson = await ordersResp.Content.ReadAsStringAsync();
        var ordersDoc = JsonDocument.Parse(ordersJson);
        
        int cancelled = 0;
        int total = 0;
        
        if (ordersDoc.RootElement.TryGetProperty("orders", out var ordersArray))
        {
            foreach (var order in ordersArray.EnumerateArray())
            {
                string status = order.GetProperty("status").GetString() ?? "";
                if (status == "ORDER_STATUS_NEW")
                {
                    total++;
                    string orderId = order.GetProperty("order_id").GetString() ?? "";
                    try
                    {
                        await connector.CancelOrderAsync(orderId);
                        cancelled++;
                        Console.WriteLine($"[CANCEL] Cancelled {orderId}");
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"[CANCEL] Failed to cancel {orderId}: {ex.Message}");
                    }
                }
            }
        }
        
        return Results.Json(new { status = "ok", cancelled, total });
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message }, statusCode: 500);
    }
});

Console.WriteLine($"в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ");
Console.WriteLine($"  OpenMarketflow Trading Server");
Console.WriteLine($"  SignalR Hub: http://0.0.0.0:{port}/trading");
Console.WriteLine($"  Health:     http://0.0.0.0:{port}/health");
Console.WriteLine($"  Status:     http://0.0.0.0:{port}/status");
Console.WriteLine($"  Grid MM:   POST /strategy/grid-mm/start в†’ /run");
Console.WriteLine($"в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ");

// РђРІС‚РѕРїРѕРґРєР»СЋС‡РµРЅРёРµ Рє Р¤РёРЅР°Рј РµСЃР»Рё С‚РѕРєРµРЅ Р·Р°РґР°РЅ
var finamToken = Environment.GetEnvironmentVariable("FINAM_TOKEN");
if (!string.IsNullOrEmpty(finamToken))
{
    _ = Task.Run(async () =>
    {
        await Task.Delay(2000); // РґРѕР¶РґР°С‚СЊСЃСЏ РїРѕР»РЅРѕРіРѕ СЃС‚Р°СЂС‚Р°
        var tradingService = app.Services.GetRequiredService<TradingService>();
        var hub = app.Services.GetRequiredService<IHubContext<TradingHub>>();
        
        Console.WriteLine("рџ”Њ РђРІС‚РѕРїРѕРґРєР»СЋС‡РµРЅРёРµ Рє Р¤РёРЅР°Рј...");
        await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "рџ”Њ РџРѕРґРєР»СЋС‡Р°СЋСЃСЊ Рє Р¤РёРЅР°Рј...");
        
        var finamAccountId = Environment.GetEnvironmentVariable("FINAM_ACCOUNT_ID") ?? "";
        var success = await tradingService.ConnectBrokerAsync(finamToken, finamAccountId);
        if (success)
        {
            Console.WriteLine("вњ… РџРѕРґРєР»СЋС‡РµРЅРѕ Рє Р¤РёРЅР°Рј!");
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "вњ… РџРѕРґРєР»СЋС‡РµРЅРѕ Рє Р¤РёРЅР°Рј!");
            await hub.Clients.All.SendAsync("OnStatusUpdate", tradingService.GetStatus());
        }
        else
        {
            Console.WriteLine("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊСЃСЏ Рє Р¤РёРЅР°Рј");
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊСЃСЏ Рє Р¤РёРЅР°Рј");
        }
    });
}

// === Fade Impulse API ===
app.MapPost("/strategy/fade-impulse/start", (TradingService tradingSvc) =>
{
    if (fadeImpulseLauncher != null)
        return Results.Json(new { status = "already_running" });

    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    var config = new FadeImpulseStrategy.Config();
    fadeImpulseLauncher = new FadeImpulseLauncher(
        broker, "1225953", "MXM6", "MXM6@RTSX", config);
    fadeImpulseLauncher.SetRiskGate(riskGate);
    fadeImpulseLauncher.Start();
    return Results.Json(new { status = "started" });
});

app.MapPost("/strategy/fade-impulse/stop", async () =>
{
    if (fadeImpulseLauncher == null)
        return Results.Json(new { status = "not_running" });
    await fadeImpulseLauncher.StopAsync();
    fadeImpulseLauncher = null;
    return Results.Json(new { status = "stopped" });
});

app.MapPost("/strategy/fade-impulse/pause", () =>
{
    if (fadeImpulseLauncher == null)
        return Results.Json(new { status = "not_running" });
    fadeImpulseLauncher.Pause();
    return Results.Json(new { status = "paused" });
});

app.MapPost("/strategy/fade-impulse/resume", () =>
{
    if (fadeImpulseLauncher == null)
        return Results.Json(new { status = "not_running" });
    fadeImpulseLauncher.Resume();
    return Results.Json(new { status = "resumed" });
});

app.MapGet("/strategy/fade-impulse/status", () =>
{
    if (fadeImpulseLauncher == null)
        return Results.Json(new { status = "not_running" });
    return Results.Json(fadeImpulseLauncher.GetStatus());
});

app.MapPost("/strategy/fade-impulse/config", async (HttpRequest req) =>
{
    if (fadeImpulseLauncher == null)
        return Results.Json(new { status = "not_running" });
    var body = await new StreamReader(req.Body).ReadToEndAsync();
    var doc = System.Text.Json.JsonDocument.Parse(body);
    var root = doc.RootElement;
    var config = fadeImpulseLauncher.Strategy.Params;
    // Update config fields if provided
    if (root.TryGetProperty("volMult", out var vm)) config.VolMult = vm.GetDouble();
    if (root.TryGetProperty("bodyMult", out var bm)) config.BodyMult = bm.GetDouble();
    if (root.TryGetProperty("slPts", out var sl)) config.SlPts = sl.GetDouble();
    if (root.TryGetProperty("tpPts", out var tp)) config.TpPts = tp.GetDouble();
    if (root.TryGetProperty("pullbackBars", out var pb)) config.PullbackBars = pb.GetInt32();
    if (root.TryGetProperty("maxHoldMinutes", out var mh)) config.MaxHoldMinutes = mh.GetInt32();
    fadeImpulseLauncher.UpdateConfig(config);
    return Results.Json(new { status = "updated", config = new {
        config.VolMult, config.BodyMult, config.SlPts, config.TpPts,
        config.PullbackBars, config.MaxHoldMinutes
    }});
});

// === V8 Trail API ===
app.MapPost("/strategy/v8-trail/start", async (HttpRequest req, TradingService tradingSvc) =>
{
    if (v8TrailLauncher != null) return Results.Json(new { status = "already_running", detail = v8TrailLauncher.GetStatus() });
    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    var body = await req.ReadFromJsonAsync<Dictionary<string, JsonElement>>();
    string ticker = body?.ContainsKey("ticker") == true ? body["ticker"].GetString()! : "RTSM6";
    string finamSym = body?.ContainsKey("finamSymbol") == true ? body["finamSymbol"].GetString()! : "RTSM6@RTSX";
    double stepPrice = body?.ContainsKey("stepPrice") == true ? body["stepPrice"].GetDouble() : 14.86;
    var config = new V8TrailStrategy.V8Params();
    if (body?.ContainsKey("sarStart") == true) config.SarStart = body["sarStart"].GetDouble();
    if (body?.ContainsKey("sarStep") == true) config.SarStep = body["sarStep"].GetDouble();
    if (body?.ContainsKey("sarMax") == true) config.SarMax = body["sarMax"].GetDouble();
    if (body?.ContainsKey("emaPeriod") == true) config.EmaPeriod = body["emaPeriod"].GetInt32();
    if (body?.ContainsKey("slPct") == true) config.SlPct = body["slPct"].GetDouble();
    if (body?.ContainsKey("maxHoldMinutes") == true) config.MaxHoldMinutes = body["maxHoldMinutes"].GetInt32();
    v8TrailLauncher = new V8TrailLauncher(broker, "1225953", ticker, finamSym, stepPrice, config);
    v8TrailLauncher.SetRiskGate(riskGate);
    v8TrailLauncher.Start();
    return Results.Json(new { status = "started", detail = v8TrailLauncher.GetStatus() });
});

app.MapPost("/strategy/v8-trail/stop", async () =>
{
    if (v8TrailLauncher == null) return Results.Json(new { status = "not_running" });
    await v8TrailLauncher.StopAsync();
    v8TrailLauncher = null;
    return Results.Json(new { status = "stopped" });
});

app.MapGet("/strategy/v8-trail/status", () =>
{
    if (v8TrailLauncher == null) return Results.Json(new { status = "not_running" });
    return Results.Json(new { status = "running", detail = v8TrailLauncher.Strategy.GetStatus() });
});

app.MapPost("/strategy/v8-trail/config", async (HttpRequest req) =>
{
    if (v8TrailLauncher == null) return Results.Json(new { status = "not_running" });
    var body = await req.ReadFromJsonAsync<Dictionary<string, JsonElement>>();
    if (body == null) return Results.Json(new { status = "error", error = "no body" });
    var p = v8TrailLauncher.Strategy.Params;
    if (body.ContainsKey("sarStart")) p.SarStart = body["sarStart"].GetDouble();
    if (body.ContainsKey("sarStep")) p.SarStep = body["sarStep"].GetDouble();
    if (body.ContainsKey("sarMax")) p.SarMax = body["sarMax"].GetDouble();
    if (body.ContainsKey("emaPeriod")) p.EmaPeriod = body["emaPeriod"].GetInt32();
    if (body.ContainsKey("slPct")) p.SlPct = body["slPct"].GetDouble();
    if (body.ContainsKey("maxHoldMinutes")) p.MaxHoldMinutes = body["maxHoldMinutes"].GetInt32();
    return Results.Json(new { status = "ok", detail = v8TrailLauncher.GetStatus() });
});

// === VP Scalp Simple API ===
app.MapPost("/strategy/vp-simple/start", async (HttpRequest req, TradingService tradingSvc) =>
{
    if (vpScalpSimpleLauncher != null)
        return Results.Json(new { status = "already_running" });
    var broker = tradingSvc.FinamBroker ?? throw new InvalidOperationException("Broker not connected");
    var config = new VpScalpSimpleStrategy.Config();
    string ticker = "MXM6", finamSym = "MXM6@RTSX";
    try {
        var body = await new StreamReader(req.Body).ReadToEndAsync();
        if (!string.IsNullOrEmpty(body)) {
            var doc = System.Text.Json.JsonDocument.Parse(body);
            if (doc.RootElement.TryGetProperty("ticker", out var t)) { ticker = t.GetString() ?? "MXM6"; finamSym = ticker + "@RTSX"; }
            if (doc.RootElement.TryGetProperty("slPct", out var sp)) config.SlPct = sp.GetDouble();
            if (doc.RootElement.TryGetProperty("maxHoldMinutes", out var mh)) config.MaxHoldMinutes = mh.GetInt32();
            if (doc.RootElement.TryGetProperty("vpLookback", out var vl)) config.VpLookback = vl.GetInt32();
            if (doc.RootElement.TryGetProperty("vpBins", out var vb)) config.VpBins = vb.GetInt32();
        }
    } catch {}
    vpScalpSimpleLauncher = new VpScalpSimpleLauncher(broker, "1225953", ticker, finamSym, config);
    vpScalpSimpleLauncher.SetRiskGate(riskGate);
    vpScalpSimpleLauncher.Start();
    return Results.Json(new { status = "started" });
});

app.MapPost("/strategy/vp-simple/stop", async () =>
{
    if (vpScalpSimpleLauncher == null) return Results.Json(new { status = "not_running" });
    await vpScalpSimpleLauncher.StopAsync();
    vpScalpSimpleLauncher = null;
    return Results.Json(new { status = "stopped" });
});

app.MapPost("/strategy/vp-simple/pause", () =>
{
    if (vpScalpSimpleLauncher == null) return Results.Json(new { status = "not_running" });
    vpScalpSimpleLauncher.Pause();
    return Results.Json(new { status = "paused" });
});

app.MapPost("/strategy/vp-simple/resume", () =>
{
    if (vpScalpSimpleLauncher == null) return Results.Json(new { status = "not_running" });
    vpScalpSimpleLauncher.Resume();
    return Results.Json(new { status = "resumed" });
});

app.MapGet("/strategy/vp-simple/status", () =>
{
    if (vpScalpSimpleLauncher == null) return Results.Json(new { status = "not_running" });
    return Results.Json(vpScalpSimpleLauncher.GetStatus());
});

app.MapPost("/strategy/vp-simple/config", async (HttpRequest req) =>
{
    if (vpScalpSimpleLauncher == null) return Results.Json(new { status = "not_running" });
    var body = await new StreamReader(req.Body).ReadToEndAsync();
    var doc = System.Text.Json.JsonDocument.Parse(body);
    var root = doc.RootElement;
    var config = vpScalpSimpleLauncher.Strategy.Params;
    if (root.TryGetProperty("slPct", out var sp)) config.SlPct = sp.GetDouble();
    if (root.TryGetProperty("maxHoldMinutes", out var mh)) config.MaxHoldMinutes = mh.GetInt32();
    if (root.TryGetProperty("vpLookback", out var vl)) config.VpLookback = vl.GetInt32();
    if (root.TryGetProperty("vpBins", out var vb)) config.VpBins = vb.GetInt32();
    if (root.TryGetProperty("vaPercent", out var va)) config.VaPercent = va.GetDouble();
    return Results.Json(new { status = "updated", config = new { config.SlPct, config.MaxHoldMinutes, config.VpLookback, config.VpBins, config.VaPercent, config.Commission } });
});

// === Robot Instance Management (independent robots) ===
var launcherPath = "/root/.openclaw/workspace/HedgeFund/robot_instance/launcher.py";
var instancesBase = "/tmp/robot-instances";

app.MapPost("/api/instance/create", async (HttpRequest req) => {
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var ticker = json.RootElement.GetProperty("ticker").GetString() ?? "SiU6";
        var port = json.RootElement.TryGetProperty("port", out var p) ? p.GetInt32() : 5071;
        var id = $"{ticker}_{port}";
        var dir = Path.Combine(instancesBase, id);
        Directory.CreateDirectory(dir);
        // Write params to file (avoids command-line JSON quoting issues)
        File.WriteAllText(Path.Combine(dir, "instance_params.json"), body);
        // Call launcher create вЂ” reads params from instance_params.json
        var psi = new System.Diagnostics.ProcessStartInfo {
            FileName = "python3",
            Arguments = $"\"{launcherPath}\" create \"{dir}\" {port} {ticker}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        var proc = System.Diagnostics.Process.Start(psi);
        var output = await proc!.StandardOutput.ReadToEndAsync();
        await proc.WaitForExitAsync();
        return Results.Json(new { id, dir, port, ticker, created = true, output = output.Trim() });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

app.MapPost("/api/instance/start", async (HttpRequest req) => {
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var id = json.RootElement.GetProperty("id").GetString();
        var dir = Path.Combine(instancesBase, id);
        var port = json.RootElement.GetProperty("port").GetInt32();
        if (!Directory.Exists(dir)) return Results.Json(new { error = "Instance not found" });
        var psi = new System.Diagnostics.ProcessStartInfo {
            FileName = "python3",
            Arguments = $"\"{launcherPath}\" start \"{dir}\" {port}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        var proc = System.Diagnostics.Process.Start(psi);
        var output = await proc!.StandardOutput.ReadToEndAsync();
        await proc.WaitForExitAsync();
        return Results.Json(new { id, port, output = output.Trim(), started = true });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

app.MapPost("/api/instance/stop", async (HttpRequest req) => {
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var id = json.RootElement.GetProperty("id").GetString();
        var dir = Path.Combine(instancesBase, id);
        var port = json.RootElement.TryGetProperty("port", out var po) ? po.GetInt32() : 0;
        // First: call robot /stop to close position gracefully
        if (port > 0) {
            try {
                using var http = new HttpClient();
                http.Timeout = TimeSpan.FromSeconds(5);
                await http.PostAsync($"http://127.0.0.1:{port}/stop", null);
            } catch {}
        }
        // Kill by port using fuser
        if (port > 0) {
            try {
                var killPsi = new System.Diagnostics.ProcessStartInfo {
                    FileName = "/bin/bash",
                    Arguments = $"-c \"fuser -k {port}/tcp 2>/dev/null\"",
                    UseShellExecute = false
                };
                System.Diagnostics.Process.Start(killPsi)?.WaitForExit(3000);
            } catch {}
        }
        // Also try pid file
        var pidPath = Path.Combine(dir, "pid");
        if (File.Exists(pidPath)) {
            try {
                var pid = int.Parse(File.ReadAllText(pidPath).Trim());
                var killPsi2 = new System.Diagnostics.ProcessStartInfo {
                    FileName = "/bin/bash",
                    Arguments = $"-c \"kill -TERM {pid} 2>/dev/null; kill -TERM -{pid} 2>/dev/null\"",
                    UseShellExecute = false
                };
                System.Diagnostics.Process.Start(killPsi2)?.WaitForExit(2000);
            } catch {}
            try { File.Delete(pidPath); } catch {}
        }
        return Results.Json(new { id, stopped = true });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

app.MapGet("/api/instance/list", () => {
    try {
        if (!Directory.Exists(instancesBase)) return Results.Json(new { instances = Array.Empty<object>() });
        var result = new List<object>();
        foreach (var dir in Directory.GetDirectories(instancesBase)) {
            var id = Path.GetFileName(dir);
            var cfgPath = Path.Combine(dir, "config.json");
            if (!File.Exists(cfgPath)) continue;
            var cfg = System.Text.Json.JsonDocument.Parse(File.ReadAllText(cfgPath));
            var ticker = cfg.RootElement.TryGetProperty("ticker", out var t) ? t.GetString() : "?";
            var port = cfg.RootElement.TryGetProperty("port", out var p) ? p.GetInt32() : 5071;
            // Check if running
            var pidPath = Path.Combine(dir, "pid");
            bool running = false;
            if (File.Exists(pidPath)) {
                try {
                    var pid = int.Parse(File.ReadAllText(pidPath).Trim());
                    System.Diagnostics.Process.GetProcessById(pid);
                    running = true;
                } catch { running = false; }
            }
            // Try get status from robot API
            string status = "unknown";
            try {
                using var http = new HttpClient(); http.Timeout = TimeSpan.FromSeconds(2);
                var resp = http.GetAsync($"http://localhost:{port}/status").Result;
                if (resp.IsSuccessStatusCode) status = "running";
            } catch { status = running ? "starting" : "stopped"; }
            result.Add(new { id, ticker, port, running, status });
        }
        return Results.Json(new { instances = result });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

app.MapPost("/api/instance/status", async (HttpRequest req) => {
    try {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var port = json.RootElement.GetProperty("port").GetInt32();
        // Check if this is a config update
        if (json.RootElement.TryGetProperty("action", out var action) && action.GetString() == "update_config") {
            using var http = new HttpClient(); http.Timeout = TimeSpan.FromSeconds(3);
            var resp = await http.PostAsync($"http://localhost:{port}/api/robot/config",
                new StringContent(json.RootElement.GetProperty("config").GetRawText(), System.Text.Encoding.UTF8, "application/json"));
            var data = await resp.Content.ReadAsStringAsync();
            return Results.Text(data, "application/json");
        }
        using var http2 = new HttpClient(); http2.Timeout = TimeSpan.FromSeconds(3);
        var resp2 = await http2.GetAsync($"http://localhost:{port}/status");
        var data2 = await resp2.Content.ReadAsStringAsync();
        return Results.Text(data2, "application/json");
    } catch (Exception ex) { return Results.Json(new { error = ex.Message, status = "offline" }); }
}).AllowAnonymous();

app.MapPost("/api/instance/delete", (HttpRequest req) => {
    try {
        using var reader = new StreamReader(req.Body);
        var body = reader.ReadToEnd();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var id = json.RootElement.GetProperty("id").GetString();
        var dir = Path.Combine(instancesBase, id);
        if (Directory.Exists(dir)) Directory.Delete(dir, true);
        return Results.Json(new { id, deleted = true });
    } catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();

// Update robot instrument in robot/.env (persists across restarts; robot may also be switched live via POST :5080/:5081/instrument)
app.MapPost("/api/robot/update-instrument", async (HttpRequest req) =>
{
    try
    {
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        var instrument = (json.RootElement.GetProperty("instrument").GetString() ?? "SiU6").Trim();
        // robot=of (Si, env ROBOT_TICKER/ROBOT_SYMBOL) | robot=of_mx (MX, env ROBOT_TICKER_MX/ROBOT_SYMBOL_MX)
        var robot = "of";
        if (json.RootElement.TryGetProperty("robot", out var rEl)) robot = rEl.GetString() ?? "of";
        var tickerKey = robot == "of_mx" ? "ROBOT_TICKER_MX" : "ROBOT_TICKER";
        var symbolKey = robot == "of_mx" ? "ROBOT_SYMBOL_MX" : "ROBOT_SYMBOL";
        var symbol = $"{instrument}@RTSX";
        // resolve robot/.env relative to server location (..\..\..\..\robot\.env) — works from bin and from repo layout
        var envPath = System.IO.Path.Combine(AppContext.BaseDirectory, "robot", ".env");
        if (!System.IO.File.Exists(envPath))
        {
            var dir = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
            for (int i = 0; i < 8 && dir != null && !System.IO.File.Exists(envPath); i++)
            {
                dir = dir.Parent;
                if (dir != null) envPath = System.IO.Path.Combine(dir.FullName, "robot", ".env");
            }
        }
        if (!System.IO.File.Exists(envPath))
        {
            // create next to robot scripts
            var dir2 = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
            while (dir2 != null && !System.IO.File.Exists(System.IO.Path.Combine(dir2.FullName, "robot", "main_of_mx.py"))) dir2 = dir2.Parent;
            if (dir2 == null) return Results.Json(new { ok = false, error = "robot dir not found" });
            envPath = System.IO.Path.Combine(dir2.FullName, "robot", ".env");
        }
        var lines = System.IO.File.Exists(envPath) ? System.IO.File.ReadAllLines(envPath) : new string[0];
        var newLines = new List<string>();
        bool foundTicker = false, foundSymbol = false;
        foreach (var line in lines)
        {
            if (line.StartsWith(tickerKey + "=")) { newLines.Add($"{tickerKey}={instrument}"); foundTicker = true; }
            else if (line.StartsWith(symbolKey + "=")) { newLines.Add($"{symbolKey}={symbol}"); foundSymbol = true; }
            else newLines.Add(line);
        }
        if (!foundTicker) newLines.Add($"{tickerKey}={instrument}");
        if (!foundSymbol) newLines.Add($"{symbolKey}={symbol}");
        System.IO.File.WriteAllLines(envPath, newLines);
        return Results.Json(new { ok = true, robot, instrument, symbol, envPath });
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
}).AllowAnonymous();


// === RISK GATE + CIRCUIT BREAKER WIRING ===
var circuitBreaker = new HedgeFund.Core.Risk.CircuitBreaker(
    riskGate,
    dailyLossLimit: 5000.0,
    emergencyCloseAll: async () => {
        Console.WriteLine("[CIRCUIT_BREAKER] Emergency close all positions");
        try {
            if (gridMm != null) gridMm.StopTrading();
            if (gridMmV8 != null) await gridMmV8.StopAsync();
            if (vpScalpGrid != null) await vpScalpGrid.StopAsync();
            if (fadeImpulseLauncher != null) await fadeImpulseLauncher.StopAsync();
            if (v8TrailLauncher != null) await v8TrailLauncher.StopAsync();
            var tradingSvc = app.Services.GetRequiredService<TradingService>();
            await tradingSvc.EmergencyStopAsync();
        } catch (Exception ex) { Console.WriteLine($"[CIRCUIT_BREAKER] Error: {ex.Message}"); }
    },
    cancelAllOrders: async () => {
        Console.WriteLine("[CIRCUIT_BREAKER] Cancelling all orders");
        try {
            var tradingSvc = app.Services.GetRequiredService<TradingService>();
            await tradingSvc.CancelAllOrdersAsync();
        } catch (Exception ex) { Console.WriteLine($"[CIRCUIT_BREAKER] Cancel error: {ex.Message}"); }
    });

// Wire RiskGate into TradingService
var tSvc = app.Services.GetRequiredService<TradingService>();
tSvc.RiskGate = riskGate;
tSvc.CircuitBreaker = circuitBreaker;
Console.WriteLine("[RISK] RiskGate + CircuitBreaker wired up");

// === CIRCUIT BREAKER MONITORING (periodic PnL check) ===
_ = Task.Run(async () =>
{
    while (true)
    {
        try
        {
            await Task.Delay(TimeSpan.FromSeconds(10));
            if (tSvc.IsConnectedToBroker)
            {
                var (equity, positions) = await tSvc.FinamBroker!.GetAccountInfoAsync();
                riskGate.SetEquityBaseline(equity);
                double unrealised = positions.Sum(p => p.avgPrice * p.qty);
                await circuitBreaker.UpdatePnLAsync(0, unrealised, equity);
            }
        }
        catch (Exception ex) { Console.WriteLine($"[RISK_MONITOR] Error: {ex.Message}"); }
    }
});

app.Run();

// === F-012: RobotProcessManager (класс в конце: top-level statements выше) ===
public static class RobotProcessManager
{
    private static readonly Dictionary<string, System.Diagnostics.Process?> _procs = new();
    private static readonly object _lock = new();

    public static (string script, int port, string args) Spec(string name) => name switch
    {
        "of" => ("main_of.py", 5080, ""),
        "of_mx" => ("main_of_mx.py", 5081, ""),
        // Arb robots: pair set via env ARB_SYMBOL_A/B per instance (start = Dmitry only, via UI)
        "arb_gazp" => ("main_arb.py", 5090, ""),
        "arb_sber" => ("main_arb.py", 5091, ""),
        "arb_br" => ("main_arb.py", 5092, ""),
        _ => throw new ArgumentException("unknown robot")
    };

    public static bool IsAlive(string name)
    {
        lock (_lock)
        {
            if (_procs.TryGetValue(name, out var p) && p != null && !p.HasExited) return true;
            // процесс мог быть запущен снаружи — проверять порт дорого; считаем по нашей записи
            return false;
        }
    }


        // Python resolve: сервер может быть автостартован с урезанным PATH,
        // где "python3" не находится — пробуем явные пути и PATH-поиск.
        static string ResolvePython()
        {
            string[] candidates = {
                System.Environment.GetEnvironmentVariable("PYTHON3_EXE"),
                @"D:\AutoClaw\resources\python\python3.exe",
                @"C:\Program Files\Python313\python3.exe",
                @"C:\Program Files\Python312\python3.exe",
                "python3.exe", "python.exe"
            };
            foreach (var c in candidates)
            {
                if (string.IsNullOrWhiteSpace(c)) continue;
                if (c.Contains('\\'))
                {
                    if (System.IO.File.Exists(c)) return c;
                }
                else
                {
                    var pathEnv = System.Environment.GetEnvironmentVariable("PATH") ?? "";
                    foreach (var d in pathEnv.Split(';'))
                    {
                        if (string.IsNullOrWhiteSpace(d)) continue;
                        try
                        {
                            var full = System.IO.Path.Combine(d.Trim(), c);
                            if (System.IO.File.Exists(full)) return full;
                        }
                        catch { }
                    }
                }
            }
            return "python3"; // прежнее поведение как last resort
        }

        public static async System.Threading.Tasks.Task<object> StartAsync(string name)
    {
        var (script, port, extraArgs) = Spec(name);
        lock (_lock)
        {
            if (_procs.TryGetValue(name, out var existing) && existing != null && !existing.HasExited)
                return new { ok = true, alreadyRunning = true, pid = existing.Id };
        }
        var root = FindRobotDir();
        if (root == null) return new { ok = false, error = "robot dir not found" };
        var psi = new System.Diagnostics.ProcessStartInfo
        {
            FileName = ResolvePython(),
            Arguments = "\"" + System.IO.Path.Combine(root, script) + "\" --port " + port + " " + extraArgs,
            WorkingDirectory = root,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        // stdout/stderr -> daily log file в robot/logs (как раньше при ручном запуске)
        var logsDir = System.IO.Path.Combine(root, "logs");
        System.IO.Directory.CreateDirectory(logsDir);
        var stamp = DateTime.Now.ToString("yyyyMMdd");
        var prefix = name == "of" ? "of" : name == "of_mx" ? "of_mx" : name;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.StandardErrorEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        if (name.StartsWith("arb_"))
        {
            // F-020: per-instance config file (pair + params live in UI-saved JSON per instance)
            psi.EnvironmentVariables["ARB_CONFIG_FILE"] = System.IO.Path.Combine(root, $"arb_config_{name.Substring(4)}.json");
            psi.EnvironmentVariables["ARB_STATE_FILE"] = System.IO.Path.Combine(root, $"arb_state_{name.Substring(4)}.json");
            psi.EnvironmentVariables["FINAM_ACCOUNT"] = "2049688";
            if (name == "arb_gazp") { psi.EnvironmentVariables["ARB_SYMBOL_A"] = "GAZP@MISX"; psi.EnvironmentVariables["ARB_TICKER_A"] = "GAZP"; psi.EnvironmentVariables["ARB_SYMBOL_B"] = "GZZ6@RTSX"; psi.EnvironmentVariables["ARB_TICKER_B"] = "GZZ6"; }
            else if (name == "arb_sber") { psi.EnvironmentVariables["ARB_SYMBOL_A"] = "SBER@MISX"; psi.EnvironmentVariables["ARB_TICKER_A"] = "SBER"; psi.EnvironmentVariables["ARB_SYMBOL_B"] = "SRZ6@RTSX"; psi.EnvironmentVariables["ARB_TICKER_B"] = "SRZ6"; }
            else if (name == "arb_br") { psi.EnvironmentVariables["ARB_SYMBOL_A"] = "BRV6@RTSX"; psi.EnvironmentVariables["ARB_TICKER_A"] = "BRV6"; psi.EnvironmentVariables["ARB_SYMBOL_B"] = "BRZ6@RTSX"; psi.EnvironmentVariables["ARB_TICKER_B"] = "BRZ6"; }
        }
        var proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return new { ok = false, error = "process failed to start" };
        _ = proc.StandardOutput.ReadToEndAsync().ContinueWith(t => System.IO.File.AppendAllText(System.IO.Path.Combine(logsDir, prefix + "_console_" + stamp + ".log"), t.Result ?? ""));
        _ = proc.StandardError.ReadToEndAsync().ContinueWith(t => System.IO.File.AppendAllText(System.IO.Path.Combine(logsDir, prefix + "_console_" + stamp + ".err"), t.Result ?? ""));
        proc.Exited += (s_, e_) => { lock (_lock) { if (_procs.GetValueOrDefault(name) == proc) _procs[name] = null; } };
        proc.EnableRaisingEvents = true;
        lock (_lock) { _procs[name] = proc; }
        // ждём подъёма API до 25с (warmup 10с + запас)
        var deadline = DateTime.UtcNow.AddSeconds(25);
        using var client = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        while (DateTime.UtcNow < deadline)
        {
            if (proc.HasExited) return new { ok = false, error = "robot process exited during startup (см. robot/logs/" + prefix + "_console_" + stamp + ".err)" };
            try {
                var resp = await client.GetAsync("http://localhost:" + port + "/health");
                if (resp.IsSuccessStatusCode) return new { ok = true, pid = proc.Id };
            } catch { }
            await System.Threading.Tasks.Task.Delay(500);
        }
        return new { ok = true, timeoutWait = true, pid = proc.Id };
    }

    public static object Stop(string name)
    {
        lock (_lock)
        {
            if (_procs.TryGetValue(name, out var p) && p != null && !p.HasExited)
            {
                try { p.Kill(entireProcessTree: true); } catch { }
                return new { ok = true, killed = true };
            }
        }
        return new { ok = true, wasNotOurs = true };
    }

    private static string? FindRobotDir()
    {
        var dir = new System.IO.DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 8 && dir != null; i++)
        {
            var cand = System.IO.Path.Combine(dir.FullName, "robot");
            if (System.IO.File.Exists(System.IO.Path.Combine(cand, "main_of_mx.py"))) return cand;
            dir = dir.Parent;
        }
        return null;
    }
}
