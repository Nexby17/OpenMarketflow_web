using Microsoft.AspNetCore.SignalR.Client;
using HedgeFund.Core.Models;
using System.Collections.Generic;

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

    // Новые события
    public event Action<Order[]>? OnActiveOrdersReceived;
    public event Action<Order>? OnOrderUpdated;
    public event Action<QuoteData>? OnQuoteUpdate;
    public event Action<StrategyStatus[]>? OnStrategyStatusesReceived;
    public event Action<OrderBookSnapshot>? OnOrderBookUpdate;
    public event Action<List<ClusterCandle>>? OnCandlesReceived;

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
        _connection.On<Order[]>("OnActiveOrders", e => OnActiveOrdersReceived?.Invoke(e));
        _connection.On<Order>("OnOrderUpdated", e => OnOrderUpdated?.Invoke(e));
        _connection.On<QuoteData>("OnQuoteUpdate", e => OnQuoteUpdate?.Invoke(e));
        _connection.On<StrategyStatus[]>("OnStrategyStatuses", e => OnStrategyStatusesReceived?.Invoke(e));
        _connection.On<OrderBookSnapshot>("OnOrderBookUpdate", e => OnOrderBookUpdate?.Invoke(e));
        _connection.On<List<ClusterCandle>>("OnCandlesUpdate", e => OnCandlesReceived?.Invoke(e));

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

    // === Заявки ===

    public async Task GetActiveOrdersAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("GetActiveOrders");
    }

    public async Task ModifyOrderAsync(string orderId, double newPrice, int newVolume)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("ModifyOrder", orderId, newPrice, newVolume);
    }

    public async Task CancelOrderAsync(string orderId)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("CancelOrder", orderId);
    }

    public async Task CancelAllOrdersAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("CancelAllOrders");
    }

    // === Котировки ===

    public async Task SubscribeQuotesAsync(string ticker)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("SubscribeQuotes", ticker);
    }

    // === Стратегии ===

    public async Task GetStrategyStatusesAsync()
    {
        EnsureConnected();
        await _connection!.InvokeAsync("GetStrategyStatuses");
    }

    public async Task PauseStrategyAsync(string name)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("PauseStrategy", name);
    }

    public async Task StopAndCloseAllAsync(string strategyName)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("StopAndCloseAll", strategyName);
    }

    // === Стакан + График ===

    public async Task SubscribeOrderBookAsync(string ticker)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("SubscribeOrderBook", ticker);
    }

    public async Task GetCandlesAsync(string ticker, string timeframe)
    {
        EnsureConnected();
        await _connection!.InvokeAsync("GetCandles", ticker, timeframe);
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
