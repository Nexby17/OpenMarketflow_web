using Microsoft.AspNetCore.SignalR;
using HedgeFund.Server.Hubs;
using HedgeFund.Server.Services;

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

// === Маппинг SignalR Hub ===
app.MapHub<TradingHub>("/trading");

// === Health-check endpoint ===
app.MapGet("/health", () => Results.Ok(new { status = "ok", time = DateTime.UtcNow }));

// === REST API для управления ===
app.MapGet("/status", (TradingService svc) => Results.Ok(svc.GetStatus()));

app.MapPost("/connect-broker", async (TradingService svc) =>
{
    var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
    if (string.IsNullOrEmpty(token))
        return Results.BadRequest(new { error = "FINAM_TOKEN не задан" });

    var success = await svc.ConnectBrokerAsync(token);
    return success
        ? Results.Ok(new { status = "connected", broker = "Finam" })
        : Results.Problem("Не удалось подключиться к Finam");
});

app.MapPost("/send-log", async (IHubContext<TradingHub> hub, HttpRequest req) =>
{
    using var reader = new StreamReader(req.Body);
    var message = await reader.ReadToEndAsync();
    await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "INFO", message);
    return Results.Ok(new { sent = true });
});

Console.WriteLine($"═══════════════════════════════════════════");
Console.WriteLine($"  OpenMarketflow Trading Server");
Console.WriteLine($"  SignalR Hub: http://0.0.0.0:{port}/trading");
Console.WriteLine($"  Health:     http://0.0.0.0:{port}/health");
Console.WriteLine($"  Status:     http://0.0.0.0:{port}/status");
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
        }
        else
        {
            Console.WriteLine("❌ Не удалось подключиться к Финам");
            await hub.Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("HH:mm:ss"), "ERROR", "❌ Не удалось подключиться к Финам");
        }
    });
}

app.Run();
