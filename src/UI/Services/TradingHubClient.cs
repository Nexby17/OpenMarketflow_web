using Microsoft.AspNetCore.SignalR.Client;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.Services;

/// <summary>
/// SignalR клиент для подключения WPF к Trading Server.
/// Отправляет команды и получает события в реальном времени.
/// </summary>
public class TradingHubClient : IAsyncDisposable
{
    private HubConnection? _connection;
    private bool _isConnected;

    // === Состояние подключения ===

    public bool IsConnected
    {
        get => _isConnected;
        private set
        {
            if (_isConnected != value)
            {
                _isConnected = value;
                OnConnectionChanged?.Invoke(value);
            }
        }
    }

    // === События от сервера (для привязки в ViewModel) ===

    public event Action<TradeEvent>? OnTradeExecuted;
    public event Action<SignalEvent>? OnSignalGenerated;
    public event Action<PositionEvent>? OnPositionUpdated;
    public event Action<ServerStatus>? OnStatusUpdate;
    public event Action<string, string, string>? OnLogMessage;
    public event Action<double>? OnEquityUpdate;
    public event Action<BacktestResultEvent>? OnBacktestResult;
    public event Action<TradeApprovalEvent>? OnTradeApprovalRequired;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    // === Подключение ===

    /// <summary>Подключиться к серверу (например "http://localhost:5050/trading")</summary>
    public async Task ConnectAsync(string serverUrl)
    {
        if (_connection != null)
            await DisconnectAsync();

        _connection = new HubConnectionBuilder()
            .WithUrl(serverUrl)
            .WithAutomaticReconnect(new[] { TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(3), TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(10) })
            .Build();

        // Подписка на события от сервера
        _connection.On<TradeEvent>("OnTradeExecuted", e => OnTradeExecuted?.Invoke(e));
        _connection.On<SignalEvent>("OnSignalGenerated", e => OnSignalGenerated?.Invoke(e));
        _connection.On<PositionEvent>("OnPositionUpdated", e => OnPositionUpdated?.Invoke(e));
        _connection.On<ServerStatus>("OnStatusUpdate", e => OnStatusUpdate?.Invoke(e));
        _connection.On<string, string, string>("OnLogMessage", (ts, level, msg) => OnLogMessage?.Invoke(ts, level, msg));
        _connection.On<double>("OnEquityUpdate", e => OnEquityUpdate?.Invoke(e));
        _connection.On<BacktestResultEvent>("OnBacktestResult", e => OnBacktestResult?.Invoke(e));
        _connection.On<TradeApprovalEvent>("OnTradeApprovalRequired", e => OnTradeApprovalRequired?.Invoke(e));
        _connection.On<string>("OnError", e => OnError?.Invoke(e));

        // Обработка состояния подключения
        _connection.Closed += _ =>
        {
            IsConnected = false;
            return Task.CompletedTask;
        };
        _connection.Reconnecting += _ =>
        {
            IsConnected = false;
            return Task.CompletedTask;
        };
        _connection.Reconnected += _ =>
        {
            IsConnected = true;
            return Task.CompletedTask;
        };

        await _connection.StartAsync();
        IsConnected = true;
    }

    /// <summary>Отключиться от сервера</summary>
    public async Task DisconnectAsync()
    {
        if (_connection != null)
        {
            await _connection.StopAsync();
            await _connection.DisposeAsync();
            _connection = null;
        }
        IsConnected = false;
    }

    // === Команды к серверу ===

    /// <summary>🔴 ЭКСТРЕННАЯ ОСТАНОВКА</summary>
    public async Task EmergencyStopAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("EmergencyStop");
    }

    /// <summary>⏸ Пауза торговли</summary>
    public async Task PauseTradingAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("PauseTrading");
    }

    /// <summary>▶ Возобновить торговлю</summary>
    public async Task ResumeTradingAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("ResumeTrading");
    }

    /// <summary>Запустить стратегию</summary>
    public async Task StartStrategyAsync(string name, string ticker, Dictionary<string, double> parameters)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("StartStrategy", name, ticker, parameters);
    }

    /// <summary>Остановить стратегию</summary>
    public async Task StopStrategyAsync(string name)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("StopStrategy", name);
    }

    /// <summary>Одобрить/отклонить сделку</summary>
    public async Task ApproveTradeAsync(string tradeId, bool approved)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("ApproveTrade", tradeId, approved);
    }

    /// <summary>Изменить параметры стратегии</summary>
    public async Task UpdateParametersAsync(string strategyName, Dictionary<string, double> parameters)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("UpdateParameters", strategyName, parameters);
    }

    /// <summary>Запросить текущий статус</summary>
    /// <summary>Передать токен Финам серверу для подключения к брокеру</summary>
    public async Task ConnectBrokerAsync(string token)
    {
        if (_connection?.State == HubConnectionState.Connected)
            await _connection.InvokeAsync("ConnectBroker", token);
    }

    public async Task RequestStatusAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("RequestStatus");
    }

    /// <summary>Запустить бэктест на сервере</summary>
    public async Task RunBacktestAsync(string strategyName, string ticker, int timeframe,
        string from, string to, Dictionary<string, double> parameters)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("RunBacktest", strategyName, ticker, timeframe, from, to, parameters);
    }

    // === Вспомогательные ===

    private void EnsureConnected()
    {
        if (_connection == null || _connection.State != HubConnectionState.Connected)
            throw new InvalidOperationException("Нет подключения к серверу");
    }

    public async ValueTask DisposeAsync()
    {
        await DisconnectAsync();
    }
}
