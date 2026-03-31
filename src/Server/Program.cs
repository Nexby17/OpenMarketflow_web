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

Console.WriteLine($"═══════════════════════════════════════════");
Console.WriteLine($"  OpenMarketflow Trading Server");
Console.WriteLine($"  SignalR Hub: http://0.0.0.0:{port}/trading");
Console.WriteLine($"  Health:     http://0.0.0.0:{port}/health");
Console.WriteLine($"═══════════════════════════════════════════");

app.Run();
