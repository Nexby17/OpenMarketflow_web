using Microsoft.AspNetCore.SignalR;
using HedgeFund.Server.Hubs;
using HedgeFund.Server.Services;
using HedgeFund.Core.Strategies;

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

// === Маппинг SignalR Hub ===
app.MapHub<TradingHub>("/trading");

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

// === REST: котировки через Finam API ===
app.MapGet("/api/quote", async (TradingService svc, string ticker) =>
{
    try
    {
        var connector = svc.Connector;
        if (connector?.IsConnected != true) return Results.Ok(new { error = "not connected" });
        var grpc = connector.GrpcClient;
        if (grpc == null) return Results.Ok(new { error = "no grpc" });
        var (bid, ask, last) = await grpc.GetLastQuoteAsync(
            ticker.Contains('@') ? ticker : (new[] {"Si","BR","GD","MX","RI","GOLD","ED","Eu","SBRF","GAZR"}.Any(p => ticker.StartsWith(p, StringComparison.OrdinalIgnoreCase)) ? $"{ticker}@RTSX" : $"{ticker}@MISX"));
        return Results.Ok(new { bid, ask, last, spread = ask - bid });
    }
    catch (Exception ex) { return Results.Ok(new { bid = 0.0, ask = 0.0, last = 0.0, error = ex.Message }); }
});

// === REST: стакан (snapshot) через Finam REST API ===
app.MapGet("/api/orderbook", async (TradingService svc, string ticker) =>
{
    try
    {
        var connector = svc.Connector;
        if (connector?.IsConnected != true) return Results.Ok(new { rows = Array.Empty<object>() });
        var restClient = connector.RestClient;
        if (restClient == null) return Results.Ok(new { rows = Array.Empty<object>() });

        var futPrefixes = new[] {"Si","BR","GD","MX","RI","GOLD","ED","Eu","SBRF","GAZR"};
        var symbol = ticker.Contains('@') ? ticker 
            : (futPrefixes.Any(p => ticker.StartsWith(p, StringComparison.OrdinalIgnoreCase)) ? $"{ticker}@RTSX" : $"{ticker}@MISX");

        // Вызываем Finam REST: GET /v1/instruments/{symbol}/orderbook
        var ob = await restClient.GetOrderBookAsync(symbol);
        return Results.Ok(ob);
    }
    catch (Exception ex) { return Results.Ok(new { rows = Array.Empty<object>(), error = ex.Message }); }
});

// === REST API: Арбитраж ===
ArbLauncher? arbLauncher = null;

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
Console.WriteLine($"  Арбитраж:   POST /arb/init → /arb/start");
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
