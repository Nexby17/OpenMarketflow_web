using Microsoft.AspNetCore.SignalR;
using HedgeFund.Core.Models;
using HedgeFund.Server.Services;

namespace HedgeFund.Server.Hubs;

/// <summary>
/// SignalR Hub для управления торговлей.
/// Клиент (WPF) → Сервер: команды.
/// Сервер → Клиент: события.
/// </summary>
public class TradingHub : Hub
{
    private readonly TradingService _tradingService;
    private readonly StrategyRunner _strategyRunner;
    private readonly ILogger<TradingHub> _logger;

    public TradingHub(TradingService tradingService, StrategyRunner strategyRunner, ILogger<TradingHub> logger)
    {
        _tradingService = tradingService;
        _strategyRunner = strategyRunner;
        _logger = logger;
    }

    // === Подключение/отключение клиентов ===

    public override async Task OnConnectedAsync()
    {
        _logger.LogInformation("Клиент подключён: {ConnectionId}", Context.ConnectionId);
        await Clients.Caller.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO", "Подключение установлено");
        
        // Отправляем текущий статус новому клиенту
        var status = _tradingService.GetStatus();
        await Clients.Caller.SendAsync("OnStatusUpdate", status);
        
        await base.OnConnectedAsync();
    }

    public override async Task OnDisconnectedAsync(Exception? exception)
    {
        _logger.LogInformation("Клиент отключён: {ConnectionId}, причина: {Error}", 
            Context.ConnectionId, exception?.Message ?? "нет");
        await base.OnDisconnectedAsync(exception);
    }

    // === Команды от клиента (WPF) к серверу ===

    /// <summary>🔴 Экстренная остановка — закрыть все позиции, остановить стратегии. ПРИОРИТЕТ №1.</summary>
    public async Task EmergencyStop()
    {
        _logger.LogWarning("⚠ ЭКСТРЕННАЯ ОСТАНОВКА от клиента {ConnectionId}", Context.ConnectionId);
        
        await _tradingService.EmergencyStopAsync();
        
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "CRITICAL", 
            "🔴 ЭКСТРЕННАЯ ОСТАНОВКА АКТИВИРОВАНА");
        await Clients.All.SendAsync("OnStatusUpdate", _tradingService.GetStatus());
    }

    /// <summary>⏸️ Пауза — не открывать новые позиции</summary>
    public async Task PauseTrading()
    {
        _logger.LogInformation("Пауза торговли от клиента {ConnectionId}", Context.ConnectionId);
        
        _tradingService.Pause();
        
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO", 
            "⏸ Торговля приостановлена");
        await Clients.All.SendAsync("OnStatusUpdate", _tradingService.GetStatus());
    }

    /// <summary>▶️ Возобновить торговлю</summary>
    public async Task ResumeTrading()
    {
        _logger.LogInformation("Возобновление торговли от клиента {ConnectionId}", Context.ConnectionId);
        
        _tradingService.Resume();
        
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO", 
            "▶ Торговля возобновлена");
        await Clients.All.SendAsync("OnStatusUpdate", _tradingService.GetStatus());
    }

    /// <summary>Запустить стратегию</summary>
    public async Task StartStrategy(string strategyName, string ticker, Dictionary<string, double> parameters)
    {
        _logger.LogInformation("Запуск стратегии {Strategy} на {Ticker}", strategyName, ticker);
        
        try
        {
            await _strategyRunner.StartAsync(strategyName, ticker, parameters);
            
            await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
                $"Стратегия {strategyName} запущена на {ticker}");
            await Clients.All.SendAsync("OnStatusUpdate", _tradingService.GetStatus());
        }
        catch (Exception ex)
        {
            await Clients.Caller.SendAsync("OnError", $"Ошибка запуска стратегии: {ex.Message}");
        }
    }

    /// <summary>Остановить стратегию</summary>
    public async Task StopStrategy(string strategyName)
    {
        _logger.LogInformation("Остановка стратегии {Strategy}", strategyName);
        
        _strategyRunner.Stop(strategyName);
        
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
            $"Стратегия {strategyName} остановлена");
        await Clients.All.SendAsync("OnStatusUpdate", _tradingService.GetStatus());
    }

    /// <summary>Одобрить или отклонить сделку (режим подтверждения)</summary>
    public async Task ApproveTrade(string tradeId, bool approved)
    {
        _logger.LogInformation("Решение по сделке {TradeId}: {Decision}", tradeId, approved ? "одобрено" : "отклонено");
        
        await _tradingService.ProcessTradeApprovalAsync(tradeId, approved);
        
        var decision = approved ? "✅ одобрена" : "❌ отклонена";
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
            $"Сделка {tradeId} {decision}");
    }

    /// <summary>Изменить параметры стратегии на лету</summary>
    public async Task UpdateParameters(string strategyName, Dictionary<string, double> parameters)
    {
        _logger.LogInformation("Обновление параметров стратегии {Strategy}: {Params}", 
            strategyName, string.Join(", ", parameters.Select(p => $"{p.Key}={p.Value}")));
        
        _strategyRunner.UpdateParameters(strategyName, parameters);
        
        await Clients.All.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
            $"Параметры {strategyName} обновлены");
    }

    /// <summary>Запросить текущий статус</summary>
    public async Task RequestStatus()
    {
        var status = _tradingService.GetStatus();
        await Clients.Caller.SendAsync("OnStatusUpdate", status);
    }

    /// <summary>Запустить бэктест</summary>
    public async Task RunBacktest(string strategyName, string ticker, int timeframe, 
        string from, string to, Dictionary<string, double> parameters)
    {
        _logger.LogInformation("Бэктест: {Strategy} на {Ticker}, TF={TF}, {From} - {To}", 
            strategyName, ticker, timeframe, from, to);

        await Clients.Caller.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
            $"Запуск бэктеста {strategyName} на {ticker}...");

        try
        {
            var result = await _tradingService.RunBacktestAsync(strategyName, ticker, timeframe, from, to, parameters);
            await Clients.Caller.SendAsync("OnBacktestResult", result);
            
            await Clients.Caller.SendAsync("OnLogMessage", DateTime.UtcNow.ToString("o"), "INFO",
                $"Бэктест завершён: PnL={result.TotalPnL:F2}, сделок={result.TotalTrades}, WR={result.WinRate:F1}%");
        }
        catch (Exception ex)
        {
            await Clients.Caller.SendAsync("OnError", $"Ошибка бэктеста: {ex.Message}");
        }
    }
}
