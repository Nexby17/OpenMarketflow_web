using Microsoft.AspNetCore.SignalR;
using HedgeFund.Server.Hubs;
using HedgeFund.Server.Services;
using HedgeFund.Server.Connectors;
using HedgeFund.Core.Strategies;
using HedgeFund.Core.Connectors;
using System.Text.Json;
using Microsoft.AspNetCore.Http.Extensions;
using System.Collections.Generic;

var builder = WebApplication.CreateBuilder(args);

// === SignalR ===
builder.Services.AddSignalR(options =>
{
    options.EnableDetailedErrors = true;
    options.MaximumReceiveMessageSize = 1024 * 1024; // 1 МБ
});

// === CORS — разрешить подключения с любого клиента ===
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
    {
        policy.AllowAnyHeader()
              .AllowAnyMethod()
              .AllowCredentials()
              .SetIsOriginAllowed(_ => true);
    });
});

// === Сервисы ===
builder.Services.AddSingleton<TradingService>();
builder.Services.AddSingleton<StrategyRunner>();

// === Порт (по умолчанию 5050, настраиваемый через --urls или ASPNETCORE_URLS) ===
var port = builder.Configuration.GetValue<int>("Port", 5050);
builder.WebHost.UseUrls($"http://0.0.0.0:{port}");

var app = builder.Build();

app.UseCors();
app.UseDefaultFiles();
app.UseStaticFiles();

// === Connector Manager ===
var connectorMgr = new HedgeFund.Core.Connectors.ConnectorManager();

// Текущий коннектор по умолчанию: Finam
var _activeConnectorName = "Finam";

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
        "QUIK" => new HedgeFund.Server.Connectors.QuikConnectorAdapter(),
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

// === Маппинг SignalR Hub ===
app.MapHub<TradingHub>("/trading");

// === QUIK Bridge State ===
var quikData = new Dictionary<string, object>();
var quikConnected = false;
DateTime quikLastHeartbeat = DateTime.MinValue;
object _quikStateLock = new object();

// === Candle Aggregator from QUIK ticks ===
var candleBuilderLock = new object();
GridMmRegimeLauncher? gridMm = null;
var candleBuilderCurrent = (double[]?)null;
var candleBuilderHistory = new LinkedList<double[]>();
const int CANDLE_TF_MINUTES = 5;
const int MAX_CANDLES = 500;

bool IsQuikAlive()
{
    lock (_quikStateLock)
        return quikConnected && (DateTime.UtcNow - quikLastHeartbeat).TotalSeconds < 10;
}

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
app.MapGet("/health", () => Results.Ok(new { status = "ok", time = DateTime.UtcNow }));

// === REST API для управления ===
app.MapGet("/status", (TradingService svc) => Results.Ok(svc.GetStatus()));

app.MapPost("/connect-broker", async (TradingService svc, HttpRequest req) =>
{
    // Токен: из тела запроса (JSON {token:"..."}) или из переменной окружения
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
    
    token ??= Environment.GetEnvironmentVariable("FINAM_TOKEN");
    if (string.IsNullOrEmpty(token))
        return Results.BadRequest(new { error = "Токен не передан и FINAM_TOKEN не задан" });

    // Сохраняем для арбитража и других сервисов
    Environment.SetEnvironmentVariable("FINAM_TOKEN", token);

    // Подключаем с таймаутом 30 сек
    var hub = app.Services.GetRequiredService<IHubContext<TradingHub>>();
    await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "🔌 Подключаюсь к Финам...");
    
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
                await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "✅ Подключено к Финам!");
                return Results.Ok(new { status = "connected", broker = "Finam" });
            }
            else
            {
                await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "❌ Не удалось подключиться");
                return Results.BadRequest(new { error = "Подключение не удалось. Проверьте токен." });
            }
        }
        else
        {
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "❌ Таймаут подключения (30 сек). Проверьте токен.");
            return Results.BadRequest(new { error = "Таймаут подключения 30сек. Проверьте токен и доступность Finam API." });
        }
    }
    catch (Exception ex)
    {
        await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", $"❌ Ошибка: {ex.Message}");
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

// === REST: свечи через Finam API ===
app.MapGet("/api/candles", async (TradingService svc, string ticker, int tf, int days) =>
{
    try
    {
        // Получаем FinamConnector из TradingService
        var connector = svc.Connector;
        if (connector != null && connector.IsConnected)
        {
            var to = DateTime.UtcNow;
            var from = to.AddDays(-Math.Max(1, Math.Min(days, 30)));
            var timeframe = TimeSpan.FromMinutes(tf > 0 ? tf : 5);
            var candles = await connector.GetHistoricalCandlesAsync(ticker, timeframe, from, to);
            return Results.Ok(candles.Select(c => new {
                t = new DateTimeOffset(c.Timestamp.ToUniversalTime()).ToUnixTimeSeconds(),
                o = c.Open, h = c.High, l = c.Low, c = c.Close, v = c.Volume
            }));
        }
        return Results.Ok(Array.Empty<object>());
    }
    catch (Exception ex) { return Results.BadRequest(new { error = ex.Message }); }
});

// === QUIK Bridge Endpoints ===
app.MapPost("/quik/data", async (HttpRequest req) =>
{
    lock (_quikStateLock)
    {
        quikLastHeartbeat = DateTime.UtcNow;
        quikConnected = true;
    }
    try
    {
        using var sr = new StreamReader(req.Body);
        var body = await sr.ReadToEndAsync();
        
        // Логирование для отладки (только первые 200 символов)
        if (body.Length > 0 && !body.Contains("\"quotes\":[]"))
            Console.WriteLine($"[QUIK] Data: {body.Substring(0, Math.Min(200, body.Length))}...");
        
        using var doc = System.Text.Json.JsonDocument.Parse(body);
        
        // Store in quikData as plain string JSON for easy serialization
        var rawJson = body;
        lock (quikData)
        {
            quikData["raw"] = rawJson;
            quikData["quotes"] = doc.RootElement.TryGetProperty("quotes", out var q) ? q.ToString() : "[]";
            quikData["pos"] = doc.RootElement.TryGetProperty("pos", out var p) ? p.ToString() : "[]";
            quikData["orders"] = doc.RootElement.TryGetProperty("orders", out var o) ? o.ToString() : "[]";
            quikData["bal"] = doc.RootElement.TryGetProperty("bal", out var b) ? b.GetDouble() : 0.0;
            quikData["free"] = doc.RootElement.TryGetProperty("free", out var f) ? f.GetDouble() : 0.0;
            quikData["acc"] = doc.RootElement.TryGetProperty("acc", out var a) ? a.GetString() : "";
            quikData["ob"] = doc.RootElement.TryGetProperty("ob", out var ob) ? ob.ToString() : "[]";
            // Также сохраняем orderbook из QUIK bridge (формат: {ticker, bids, asks})
            if (doc.RootElement.TryGetProperty("orderbook", out var orderbook))
                quikData["ob"] = orderbook.ToString();
            quikData["ts"] = doc.RootElement.TryGetProperty("ts", out var t) ? t.GetInt64() : 0;
        }
        
        // Aggregate candles from quotes
        if (doc.RootElement.TryGetProperty("quotes", out var quotesArr))
        {
            foreach (var qEl in quotesArr.EnumerateArray())
            {
                var last = qEl.TryGetProperty("l", out var lp) ? lp.GetDouble() : 0;
                var vol = qEl.TryGetProperty("v", out var vp) ? vp.GetDouble() : 0;
                var ts = doc.RootElement.TryGetProperty("ts", out var tsEl) ? tsEl.GetInt64() : DateTimeOffset.UtcNow.ToUnixTimeSeconds();
                if (last > 0) AggregateCandleTick(last, vol, ts);
            }
        }
        
        // Broadcast via SignalR
        var hub = app.Services.GetRequiredService<IHubContext<TradingHub>>();
        if (doc.RootElement.TryGetProperty("quotes", out var quotes))
            _ = hub.Clients.All.SendAsync("OnQuoteUpdate", quotes.ToString());
        if (doc.RootElement.TryGetProperty("pos", out var pos) && pos.GetArrayLength() > 0)
            _ = hub.Clients.All.SendAsync("OnPositionUpdate", pos.ToString());
        if (doc.RootElement.TryGetProperty("orders", out var orders) && orders.GetArrayLength() > 0)
            _ = hub.Clients.All.SendAsync("OnOrderUpdate", orders.ToString());
        if (doc.RootElement.TryGetProperty("bal", out var bal))
            _ = hub.Clients.All.SendAsync("OnBalanceUpdate", new { balance = bal.GetDouble(), free = doc.RootElement.TryGetProperty("free", out var fr) ? fr.GetDouble() : bal.GetDouble(), account = doc.RootElement.TryGetProperty("acc", out var ac) ? ac.GetString() : "", ts = doc.RootElement.TryGetProperty("ts", out var t2) ? t2.GetInt64() : 0 });
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[QUIK] Error processing data: {ex.Message}");
    }
    return Results.Json(new { status = "ok" });
});

app.MapGet("/quik/status", () =>
{
    DateTime lastHeartbeat;
    bool connected;
    lock (_quikStateLock)
    {
        lastHeartbeat = quikLastHeartbeat;
        connected = quikConnected;
    }
    var age = DateTime.UtcNow - lastHeartbeat;
    var alive = connected && age.TotalSeconds < 10;
    return Results.Json(new
    {
        connected = alive,
        lastHeartbeat = lastHeartbeat.ToString("o"),
        age = (int)age.TotalSeconds
    });
});

app.MapGet("/quik/latest", () =>
{
    lock (quikData)
    {
        if (quikData.ContainsKey("raw"))
            return Results.Json(new { source = "QUIK", data = quikData["raw"], quotes = quikData["quotes"], pos = quikData["pos"], orders = quikData["orders"], ob = quikData.ContainsKey("ob") ? quikData["ob"] : "[]", bal = quikData["bal"], free = quikData["free"], acc = quikData["acc"] });
        return Results.Json(new { error = "no data" });
    }
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

// === Unified Data Provider API ===
app.MapGet("/transaq/health", (TradingService svc) =>
{
    DateTime lastHeartbeat;
    bool connected;
    lock (_quikStateLock)
    {
        lastHeartbeat = quikLastHeartbeat;
        connected = quikConnected;
    }
    var quikAge = DateTime.UtcNow - lastHeartbeat;
    var quikAlive = connected && quikAge.TotalSeconds < 10;
    var finamOk = svc.Connector?.IsConnected == true;
    return Results.Json(new
    {
        finam = new { status = finamOk ? "connected" : "not_connected", broker = "Finam Trade API (gRPC)" },
        transaq = new { status = "not_connected", broker = "Transaq (not configured)" },
        quik = new { status = quikAlive ? "connected" : "not_connected", broker = "QUIK Bridge (Lua)", lastHeartbeat = lastHeartbeat.ToString("o"), age = (int)quikAge.TotalSeconds },
        dataSource = quikAlive ? "QUIK" : (finamOk ? "Finam" : "none"),
        timestamp = DateTime.UtcNow.ToString("o")
    });
});

app.MapGet("/api/quotes", () =>
{
    if (IsQuikAlive())
    {
        lock (quikData)
        {
            if (quikData.ContainsKey("quotes"))
                return Results.Json(new { source = "QUIK", data = quikData["quotes"] });
        }
    }
    return Results.Json(new { source = "Finam", data = "[]", note = "QUIK offline, use /api/quote?ticker=..." });
});

app.MapGet("/api/balance", () =>
{
    if (IsQuikAlive())
    {
        lock (quikData)
        {
            if (quikData.ContainsKey("bal"))
            {
                return Results.Json(new { source = "QUIK", balance = quikData["bal"], free = quikData["free"], account = quikData["acc"] });
            }
        }
    }
    return Results.Json(new { source = "Finam", note = "QUIK offline, use /api/accounts" });
});

app.MapGet("/api/orderbook/{ticker}", (string ticker) =>
{
    if (IsQuikAlive())
    {
        lock (quikData)
        {
            if (quikData.ContainsKey("ob"))
                return Results.Json(new { source = "QUIK", ticker, data = quikData["ob"] });
        }
    }
    return Results.Json(new { source = "Finam", note = "QUIK offline, use /api/orderbook?ticker=..." });
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

// === QUIK candles for Grid MM (ISO format) ===
app.MapGet("/quik/candles", () =>
{
    lock (candleBuilderLock)
    {
        var candles = candleBuilderHistory.ToList();
        if (candleBuilderCurrent != null) candles.Add(candleBuilderCurrent);
        return Results.Json(candles.Select(c => new 
        {
            Timestamp = DateTimeOffset.FromUnixTimeSeconds((long)c[0]).ToString("o"),
            Open = c[1],
            High = c[2],
            Low = c[3],
            Close = c[4],
            Volume = c[5]
        }));
    }
});

// === QUIK current price (for Grid MM signals) ===
app.MapGet("/quik/price", () =>
{
    lock (quikData)
    {
        if (quikData.TryGetValue("quotes", out var quotesJson))
        {
            var doc = JsonDocument.Parse(quotesJson.ToString());
            if (doc.RootElement.GetArrayLength() > 0)
            {
                var first = doc.RootElement[0];
                if (first.TryGetProperty("l", out var last))
                    return Results.Json(new { price = last.GetDouble(), timestamp = DateTime.UtcNow.ToString("o") });
            }
        }
        return Results.Json(new { price = 0.0, timestamp = DateTime.UtcNow.ToString("o") });
    }
});

// === REST: котировки через Finam API ===
// === Accounts API (Finam) ===
app.MapGet("/api/accounts", async (TradingService svc) =>
{
    try
    {
        if (svc.Connector?.IsConnected != true) return Results.Json(new { error = "not connected" });
        var info = await svc.Connector.GetAccountInfoAsync();
        return Results.Json(new {
            accounts = new[] {
                new { id = "1225953", name = "Main", balance = info.equity, free = info.equity, margin = 0.0, go = 0.0, pnlToday = 0.0, pnlTotal = 0.0 },
                new { id = "1225953-EDP", name = "EDP", balance = 0.0, free = 0.0, margin = 0.0, go = 0.0, pnlToday = 0.0, pnlTotal = 0.0 }
            }
        });
    }
    catch (Exception ex) { return Results.Json(new { error = ex.Message }); }
});

// === Positions API (unified) ===
app.MapGet("/api/positions", async (TradingService svc) =>
{
    if (IsQuikAlive())
    {
        lock (quikData)
        {
            if (quikData.ContainsKey("pos"))
            {
                try {
                    var posJson = quikData["pos"].ToString();
                    var doc = System.Text.Json.JsonDocument.Parse(posJson);
                    var arr = doc.RootElement.EnumerateArray().Select(p => new {
                        ticker = p.TryGetProperty("t", out var t) ? t.GetString() : "",
                        dir = p.TryGetProperty("l", out var l) ? (l.GetInt32() > 0 ? "Buy" : "Sell") : "",
                        qty = p.TryGetProperty("l", out var l2) ? Math.Abs(l2.GetInt32()) : 0,
                        avgPrice = p.TryGetProperty("p", out var p2) ? p2.GetDouble() : 0,
                        pnlToday = p.TryGetProperty("tb", out var tb) ? tb.GetDouble() : 0
                    }).ToList();
                    if (arr.Count > 0) return Results.Json(arr);
                } catch {}
            }
        }
    }
    // Fallback: return empty
    return Results.Json(new object[] {});
});

// === Orders API (unified) ===
app.MapGet("/api/orders", async (TradingService svc) =>
{
    if (IsQuikAlive())
    {
        lock (quikData)
        {
            if (quikData.ContainsKey("orders"))
            {
                try {
                    var ordJson = quikData["orders"].ToString();
                    var doc = System.Text.Json.JsonDocument.Parse(ordJson);
                    if (doc.RootElement.GetArrayLength() > 0)
                        return Results.Text(ordJson, "application/json");
                } catch {}
            }
        }
    }
    return Results.Json(new object[] {});
});

app.MapGet("/api/quote", (string ticker) =>
{
    lock (quikData)
    {
        if (!quikData.TryGetValue("quotes", out var quotesJson))
            return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, source = "QUIK (no data)" });
        
        try
        {
            var doc = JsonDocument.Parse(quotesJson.ToString());
            if (doc.RootElement.GetArrayLength() == 0)
                return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, source = "QUIK (empty)" });
            
            var first = doc.RootElement[0];
            var bid = first.TryGetProperty("b", out var b) ? b.GetDouble() : 0.0;
            var ask = first.TryGetProperty("a", out var a) ? a.GetDouble() : 0.0;
            var last = first.TryGetProperty("l", out var l) ? l.GetDouble() : 0.0;
            var spread = ask > 0 && bid > 0 ? ask - bid : 0.0;
            return Results.Ok(new { bid, ask, last, spread, source = "QUIK" });
        }
        catch
        {
            return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, source = "QUIK (error)" });
        }
    }
});
// === QUICK ORDERBOOK (из QUIK данных) ===
app.MapGet("/api/orderbook", async (string ticker) =>
{
    // Сначала пробуем QUIK
    lock (quikData)
    {
        if (quikData.TryGetValue("ob", out var obJson) && obJson.ToString() != "[]")
        {
            try
            {
                var doc = JsonDocument.Parse(obJson.ToString());
                var rows = new List<object>();
                
                // Формат: {ticker, bids: [{price, qty}], asks: [{price, qty}]}
                var bids = doc.RootElement.TryGetProperty("bids", out var bArr) ? bArr : default;
                var asks = doc.RootElement.TryGetProperty("asks", out var aArr) ? aArr : default;
                
                if (bids.ValueKind == JsonValueKind.Array || asks.ValueKind == JsonValueKind.Array)
                {
                    var priceMap = new Dictionary<double, double[]>();
                    
                    if (bids.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var b in bids.EnumerateArray())
                        {
                            var price = b.TryGetProperty("price", out var p) ? p.GetDouble() : 0;
                            var qty = b.TryGetProperty("qty", out var q) ? q.GetDouble() : 0;
                            if (price > 0)
                            {
                                if (!priceMap.ContainsKey(price)) priceMap[price] = new double[2];
                                priceMap[price][0] = qty;
                            }
                        }
                    }
                    if (asks.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var a in asks.EnumerateArray())
                        {
                            var price = a.TryGetProperty("price", out var p) ? p.GetDouble() : 0;
                            var qty = a.TryGetProperty("qty", out var q) ? q.GetDouble() : 0;
                            if (price > 0)
                            {
                                if (!priceMap.ContainsKey(price)) priceMap[price] = new double[2];
                                priceMap[price][1] = qty;
                            }
                        }
                    }
                    
                    foreach (var kv in priceMap.OrderByDescending(x => x.Key))
                        rows.Add(new { price = kv.Key, bid = kv.Value[0], ask = kv.Value[1] });
                }
                
                if (rows.Count > 0)
                    return Results.Ok(new { rows, source = "QUIK" });
            }
            catch { }
        }
        
        // Фолбэк: Генерируем стакан из котировок (если QUIK не отправляет стакан)
        try
        {
            if (quikData.TryGetValue("quotes", out var quotesJson))
            {
                var doc = JsonDocument.Parse(quotesJson.ToString());
                if (doc.RootElement.GetArrayLength() > 0)
                {
                    var quote = doc.RootElement[0];
                    var bid = quote.TryGetProperty("b", out var b) ? b.GetDouble() : 0;
                    var ask = quote.TryGetProperty("a", out var a) ? a.GetDouble() : 0;
                    var last = quote.TryGetProperty("l", out var l) ? l.GetDouble() : 0;
                    
                    if (bid > 0 && ask > 0)
                    {
                        var rows = new List<object>();
                        // Генерируем 10 уровней
                        for (int i = 0; i < 10; i++)
                        {
                            var bidPrice = bid - i * 0.1;
                            var askPrice = ask + i * 0.1;
                            rows.Add(new { price = bidPrice, bid = 100, ask = 0 });
                            rows.Add(new { price = askPrice, bid = 0, ask = 100 });
                        }
                        return Results.Ok(new { rows, source = "Generated from quotes" });
                    }
                }
            }
        }
        catch { }
    }
    
    return Results.Ok(new { rows = Array.Empty<object>(), source = "No data" });
});

app.MapPost("/strategy/grid-mm/start", () =>
{
    try
    {
        var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (string.IsNullOrEmpty(token))
            return Results.Json(new { error = "FINAM_TOKEN not set" }, statusCode: 400);
        if (gridMm != null)
            return Results.Json(new { status = "already_running", detail = gridMm.GetStatus() });
        
        gridMm = new GridMmRegimeLauncher(token, "SiM6", useQuikData: true);
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
    gridMm.StopTrading();
    return Results.Json(new { status = "stopped", detail = gridMm.GetStatus() });
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
        if (root.TryGetProperty("minProfitPerLot", out var minProfit)) gridMm.Strategy.Params.MinProfitPerLot = minProfit.GetDouble();
        if (root.TryGetProperty("closePct", out var closePct)) gridMm.Strategy.Params.ClosePct = closePct.GetDouble();
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

// NOTE: NoSignal и PSAR Grid — удалены. Используем Grid MM Regime.

// === REST API: Арбитраж ===
ArbLauncher? arbLauncher = null;


// === Volume Reversal (RTS) ===
VolumeReversalLauncher volRev = null;

// === Единый endpoint: все активные стратегии ===
app.MapGet("/api/active-strategies", () =>
{
    var strategies = new List<object>();
    if (gridMm != null)
    {
        var s = gridMm.Strategy;
        strategies.Add(new {
            id = "grid-mm", name = "Grid MM v6", instrument = "SiM6", tf = "5 мин",
            mode = s.Mode.ToString(), posDir = s.PositionDirection, entryPrice = s.EntryPrice,
            lots = s.CurrentLotLevel, openLots = s.OpenLots, filledGrid = s.FilledGridLevels,
            totalTrades = s.TotalTrades, totalPnL = s.TotalPnL,
            sar = s.CurrentSar, ema = s.CurrentEma, connected = gridMm.IsConnected,
            detail = gridMm.GetStatus()
        });
    }
    if (volRev != null)
    {
        var s = volRev.Strategy;
        strategies.Add(new {
            id = "vol-rev", name = "Volume Reversal", instrument = "SiM6", tf = "5 мин",
            mode = s.PositionDirection != 0 ? "Running" : "Waiting",
            posDir = s.PositionDirection, entryPrice = 0.0, lots = 0, openLots = 0, filledGrid = 0,
            totalTrades = s.TotalTrades, totalPnL = s.TotalPnL,
            sar = 0.0, ema = 0.0, connected = false, detail = volRev.GetStatus()
        });
    }
    if (arbLauncher != null)
    {
        strategies.Add(new {
            id = "arb", name = "Arbitrage", instrument = "Multi", tf = "—",
            mode = "Running", posDir = 0, entryPrice = 0.0, lots = 0, openLots = 0, filledGrid = 0,
            totalTrades = 0, totalPnL = 0.0, sar = 0.0, ema = 0.0,
            connected = arbLauncher.IsConnected, detail = arbLauncher.GetStatus()
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

// === Arbitrage ===
app.MapPost("/arb/start", () =>
{
    if (arbLauncher == null) return Results.BadRequest(new { error = "Арбитраж не инициализирован. POST /arb/init" });
    arbLauncher.Start();
    return Results.Ok(new { status = "running", detail = arbLauncher.GetStatus() });
});

app.MapPost("/arb/stop", () =>
{
    if (arbLauncher == null) return Results.BadRequest(new { error = "Арбитраж не инициализирован" });
    arbLauncher.StopTrading();
    return Results.Ok(new { status = "stopped", detail = arbLauncher.GetStatus() });
});

app.MapPost("/arb/pause", () =>
{
    if (arbLauncher == null) return Results.BadRequest(new { error = "Арбитраж не инициализирован" });
    arbLauncher.Pause();
    return Results.Ok(new { status = "paused", detail = arbLauncher.GetStatus() });
});

app.MapGet("/arb/status", () =>
{
    if (arbLauncher == null) return Results.Ok(new { status = "not_initialized" });
    return Results.Ok(new { status = "ok", detail = arbLauncher.GetStatus(), connected = arbLauncher.IsConnected });
});

app.MapPost("/arb/init", async (HttpRequest req) =>
{
    try
    {
        // Токен: из тела запроса или из переменной окружения
        string? token = null;
        try
        {
            using var reader = new StreamReader(req.Body);
            var body = await reader.ReadToEndAsync();
            Console.WriteLine($"[ARB/INIT] body: {body?.Substring(0, Math.Min(body?.Length ?? 0, 100))}");
            if (!string.IsNullOrWhiteSpace(body))
            {
                var json = System.Text.Json.JsonDocument.Parse(body);
                if (json.RootElement.TryGetProperty("token", out var t))
                {
                    var val = t.GetString();
                    if (!string.IsNullOrWhiteSpace(val))
                        token = val;
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[ARB/INIT] Ошибка парсинга body: {ex.Message}");
        }
        
        if (string.IsNullOrEmpty(token))
            token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        
        Console.WriteLine($"[ARB/INIT] token: {(string.IsNullOrEmpty(token) ? "EMPTY" : token.Substring(0, Math.Min(4, token.Length)) + "...")}");
        
        if (string.IsNullOrEmpty(token))
            return Results.Json(new { error = "Токен не передан. Сначала подключитесь на вкладке Торговля" }, statusCode: 400);

        if (arbLauncher != null)
            return Results.Json(new { status = "already_initialized", detail = arbLauncher.GetStatus() });

        var capital = 10_000_000.0;
        Console.WriteLine($"[ARB/INIT] Создаю ArbLauncher...");
        arbLauncher = new ArbLauncher(token, capital: capital);
        Console.WriteLine($"[ARB/INIT] ✅ ArbLauncher создан");
        return Results.Json(new { status = "initialized", capital });
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[ARB/INIT] ❌ Ошибка: {ex}");
        return Results.Json(new { error = $"Ошибка инициализации: {ex.Message}" }, statusCode: 500);
    }
});

app.MapPost("/arb/pair/{spot}/start", (string spot) =>
{
    arbLauncher?.SetPairMode(spot, StrategyMode.Running);
    return Results.Ok(new { pair = spot, mode = "running" });
});

app.MapPost("/arb/pair/{spot}/stop", (string spot) =>
{
    arbLauncher?.SetPairMode(spot, StrategyMode.Stopped);
    return Results.Ok(new { pair = spot, mode = "stopped" });
});

// Установить лоты: POST /arb/pair/ROSN/lots с JSON {spotLots: 100, futLots: 1}
app.MapPost("/arb/pair/{spot}/lots", async (string spot, HttpRequest req) =>
{
    try
    {
        if (arbLauncher == null)
            return Results.Json(new { error = "Арбитраж не инициализирован" }, statusCode: 400);
        
        var name = $"ARB_{spot}";
        var strategy = arbLauncher.Portfolio.Strategies.Values.FirstOrDefault(s => s.Name == name);
        if (strategy == null)
            return Results.Json(new { error = $"Пара {spot} не найдена" }, statusCode: 404);
        
        using var reader = new StreamReader(req.Body);
        var body = await reader.ReadToEndAsync();
        var json = System.Text.Json.JsonDocument.Parse(body);
        
        if (json.RootElement.TryGetProperty("spotLots", out var sl))
            strategy.SpotLots = sl.GetInt32();
        if (json.RootElement.TryGetProperty("futLots", out var fl))
            strategy.FutLots = fl.GetInt32();
        
        Console.WriteLine($"[ARB] {name}: spotLots={strategy.SpotLots} (эфф.={strategy.EffectiveSpotLots}), futLots={strategy.FutLots}");
        return Results.Json(new { pair = spot, spotLots = strategy.EffectiveSpotLots, futLots = strategy.FutLots, status = "ok" });
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message }, statusCode: 400);
    }
});

app.MapGet("/arb/pairs", () =>
{
    if (arbLauncher == null)
        return Results.Json(new { pairs = Array.Empty<object>() });
    
    var pairs = arbLauncher.Portfolio.Strategies.Values.Select(s => new
    {
        name = s.Name,
        spot = s.SpotTicker,
        futures = s.FuturesTicker,
        spotLots = s.EffectiveSpotLots,
        futLots = s.FutLots,
        spotValueRub = Math.Round(s.SpotValueRub, 0),
        futGORub = Math.Round(s.FutGORub, 0),
        totalValueRub = Math.Round(s.SpotValueRub + s.FutGORub, 0),
        zScore = Math.Round(s.LastZScore, 2),
        basisAnnual = Math.Round(s.LastBasisAnnual, 1),
        isOpen = s.CurrentPosition.IsOpen,
        posDirection = s.CurrentPosition.Direction.ToString(),
        posSpotLots = s.CurrentPosition.SpotLots,
        posFutLots = s.CurrentPosition.FutLots,
        pnl = Math.Round(s.TotalPnL, 0),
        trades = s.TotalTrades,
        winRate = s.TotalTrades > 0 ? Math.Round(s.WinRate, 0) : 0,
        mode = s.Mode.ToString(),
        spotPrice = Math.Round(s.LastSpotPrice, 2),
        futPrice = Math.Round(s.LastFuturesPrice, 2),
        sharesPerSpotLot = s.SharesPerSpotLot,
        futuresGO = s.FuturesGO,
    }).ToArray();
    
    return Results.Json(new { pairs });
});

Console.WriteLine($"═══════════════════════════════════════════");
Console.WriteLine($"  OpenMarketflow Trading Server");
Console.WriteLine($"  SignalR Hub: http://0.0.0.0:{port}/trading");
Console.WriteLine($"  Health:     http://0.0.0.0:{port}/health");
Console.WriteLine($"  Status:     http://0.0.0.0:{port}/status");
Console.WriteLine($"  Grid MM:   POST /strategy/grid-mm/start → /run");
Console.WriteLine($"═══════════════════════════════════════════");

// Автоподключение к Финам если токен задан
var finamToken = Environment.GetEnvironmentVariable("FINAM_TOKEN");
if (!string.IsNullOrEmpty(finamToken))
{
    _ = Task.Run(async () =>
    {
        await Task.Delay(2000); // дождаться полного старта
        var tradingService = app.Services.GetRequiredService<TradingService>();
        var hub = app.Services.GetRequiredService<IHubContext<TradingHub>>();
        
        Console.WriteLine("🔌 Автоподключение к Финам...");
        await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "🔌 Подключаюсь к Финам...");
        
        var success = await tradingService.ConnectBrokerAsync(finamToken);
        if (success)
        {
            Console.WriteLine("✅ Подключено к Финам!");
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "✅ Подключено к Финам!");
            await hub.Clients.All.SendAsync("OnStatusUpdate", tradingService.GetStatus());
            
            // Автозапуск арбитража
            Console.WriteLine("🔄 Инициализация арбитражного портфеля...");
            arbLauncher = new ArbLauncher(finamToken);
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", "📊 Арбитраж инициализирован. POST /arb/start для запуска");
        }
        else
        {
            Console.WriteLine("❌ Не удалось подключиться к Финам");
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "❌ Не удалось подключиться к Финам");
        }
    });
}

app.Run();
