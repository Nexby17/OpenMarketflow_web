using Microsoft.AspNetCore.SignalR;
using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Brokers.Finam;
using HedgeFund.Server.Hubs;
using HedgeFund.Server.Models;

namespace HedgeFund.Server.Services;

/// <summary>
/// Синглтон-сервис управления торговлей.
/// Thread-safe: все мутации состояния через lock.
/// </summary>
public class TradingService : IDisposable
{
    private readonly IHubContext<TradingHub> _hubContext;
    private readonly ILogger<TradingService> _logger;
    private readonly IConfiguration _configuration;

    private readonly object _lock = new();

    // === Состояние ===
    private bool _isRunning;
    private bool _isPaused;
    private FinamConnector? _connector;
    private double _balance;
    private double _equity;
    private double _todayPnL;
    private double _totalPnL;
    private int _todayTrades;

    // Открытые позиции: ticker → PositionEvent
    private readonly Dictionary<string, PositionEvent> _positions = new();

    // Активные стратегии
    private readonly HashSet<string> _activeStrategies = new();

    // Режим подтверждения: ожидающие одобрения сделки
    private readonly Dictionary<string, PendingTrade> _pendingApprovals = new();

    /// <summary>Режим подтверждения сделок (true = запрашивать одобрение перед исполнением)</summary>
    public bool ApprovalMode { get; set; }

    public TradingService(IHubContext<TradingHub> hubContext, ILogger<TradingService> logger, IConfiguration configuration)
    {
        _hubContext = hubContext;
        _logger = logger;
        _configuration = configuration;
    }

    // === Публичные свойства (thread-safe через lock) ===

    public bool IsRunning { get { lock (_lock) return _isRunning; } }
    public bool IsPaused { get { lock (_lock) return _isPaused; } }
    public bool IsConnectedToBroker { get { lock (_lock) return _connector?.IsConnected ?? false; } }

    // === Управление ===

    /// <summary>🔴 ЭКСТРЕННАЯ ОСТАНОВКА — приоритет №1, работает мгновенно</summary>
    public async Task EmergencyStopAsync()
    {
        _logger.LogCritical("🔴 ЭКСТРЕННАЯ ОСТАНОВКА");

        lock (_lock)
        {
            _isRunning = false;
            _isPaused = false;
            _activeStrategies.Clear();
        }

        // Закрываем все позиции через API брокера
        if (_connector?.IsConnected == true)
        {
            try
            {
                var positions = await _connector.GetPositionsAsync();
                foreach (var pos in positions)
                {
                    if (!pos.IsOpen) continue;

                    var closeDirection = pos.Direction == SignalDirection.Buy
                        ? SignalDirection.Sell
                        : SignalDirection.Buy;

                    var order = new Order
                    {
                        Ticker = pos.Ticker,
                        Direction = closeDirection,
                        Type = OrderType.Market,
                        Volume = pos.TotalVolume,
                        Comment = "EMERGENCY STOP"
                    };

                    await _connector.PlaceOrderAsync(order);
                    _logger.LogWarning("Закрыта позиция {Ticker}: {Dir} x{Vol}", pos.Ticker, closeDirection, pos.TotalVolume);
                }

                // Отменяем все активные заявки
                var orders = await _connector.GetActiveOrdersAsync();
                foreach (var order in orders)
                {
                    await _connector.CancelOrderAsync(order.BrokerOrderId);
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Ошибка при экстренном закрытии позиций");
                await SendError($"Ошибка при экстренной остановке: {ex.Message}");
            }
        }

        // Очищаем ожидающие одобрения
        lock (_lock)
        {
            _pendingApprovals.Clear();
            _positions.Clear();
        }

        await _hubContext.Clients.All.SendAsync("OnStatusUpdate", GetStatus());
    }

    /// <summary>⏸ Пауза — не открывать новые позиции</summary>
    public void Pause()
    {
        lock (_lock) _isPaused = true;
        _logger.LogInformation("Торговля приостановлена");
    }

    /// <summary>▶ Возобновить торговлю</summary>
    public void Resume()
    {
        lock (_lock) _isPaused = false;
        _logger.LogInformation("Торговля возобновлена");
    }

    /// <summary>Подключиться к Финам</summary>
    public async Task<bool> ConnectBrokerAsync(string token, string? accountId = null)
    {
        var connector = new FinamConnector();

        connector.OnTrade += OnBrokerTrade;
        connector.OnOrderUpdate += OnBrokerOrderUpdate;
        connector.OnError += msg => _logger.LogWarning("Финам: {Error}", msg);
        connector.OnConnectionChanged += connected =>
        {
            _logger.LogInformation("Финам подключение: {Status}", connected ? "установлено" : "потеряно");
        };

        var success = await connector.ConnectAsync(token, accountId ?? string.Empty);
        if (success)
        {
            lock (_lock)
            {
                _connector = connector;
                _isRunning = true;
            }

            var balance = await connector.GetBalanceAsync();
            lock (_lock)
            {
                _balance = balance;
                _equity = balance;
            }

            _logger.LogInformation("Подключено к Финам. Баланс: {Balance:N0} ₽", balance);
        }

        return success;
    }

    /// <summary>Зарегистрировать активную стратегию</summary>
    public void RegisterStrategy(string strategyName)
    {
        lock (_lock) _activeStrategies.Add(strategyName);
    }

    /// <summary>Убрать стратегию из активных</summary>
    public void UnregisterStrategy(string strategyName)
    {
        lock (_lock) _activeStrategies.Remove(strategyName);
    }

    // === Обработка сигналов от стратегий ===

    /// <summary>Обработать сигнал от стратегии. При ApprovalMode — запросить одобрение клиента.</summary>
    public async Task ProcessSignalAsync(Signal signal, string strategyName)
    {
        // Отправляем событие сигнала
        await _hubContext.Clients.All.SendAsync("OnSignalGenerated", new SignalEvent
        {
            Time = signal.Timestamp,
            Ticker = signal.Ticker,
            Direction = signal.Direction.ToString(),
            Strategy = strategyName,
            Price = signal.Price,
            Reason = signal.Comment
        });

        lock (_lock)
        {
            if (_isPaused)
            {
                _logger.LogInformation("Сигнал проигнорирован (пауза): {Ticker} {Dir}", signal.Ticker, signal.Direction);
                return;
            }
        }

        if (ApprovalMode)
        {
            // Режим подтверждения: запрашиваем одобрение
            var approval = new TradeApprovalEvent
            {
                TradeId = Guid.NewGuid().ToString(),
                Ticker = signal.Ticker,
                Direction = signal.Direction.ToString(),
                Price = signal.Price,
                Volume = signal.Volume,
                Strategy = strategyName,
                Reason = signal.Comment,
                Deadline = DateTime.UtcNow.AddMinutes(1)
            };

            lock (_lock)
            {
                _pendingApprovals[approval.TradeId] = new PendingTrade
                {
                    Signal = signal,
                    StrategyName = strategyName,
                    Deadline = approval.Deadline
                };
            }

            await _hubContext.Clients.All.SendAsync("OnTradeApprovalRequired", approval);
        }
        else
        {
            // Автоматическое исполнение
            await ExecuteSignalAsync(signal, strategyName);
        }
    }

    /// <summary>Обработать одобрение/отклонение сделки</summary>
    public async Task ProcessTradeApprovalAsync(string tradeId, bool approved)
    {
        PendingTrade? pending;
        lock (_lock)
        {
            if (!_pendingApprovals.TryGetValue(tradeId, out pending))
            {
                _logger.LogWarning("Сделка {TradeId} не найдена в ожидающих", tradeId);
                return;
            }
            _pendingApprovals.Remove(tradeId);
        }

        if (approved && DateTime.UtcNow < pending.Deadline)
        {
            await ExecuteSignalAsync(pending.Signal, pending.StrategyName);
        }
        else if (!approved)
        {
            _logger.LogInformation("Сделка {TradeId} отклонена клиентом", tradeId);
        }
        else
        {
            _logger.LogWarning("Сделка {TradeId} просрочена", tradeId);
            await SendError($"Сделка {tradeId} просрочена (deadline прошёл)");
        }
    }

    /// <summary>Исполнить сигнал через брокера</summary>
    private async Task ExecuteSignalAsync(Signal signal, string strategyName)
    {
        if (_connector?.IsConnected != true)
        {
            await SendError("Нет подключения к брокеру — сделка не исполнена");
            return;
        }

        try
        {
            var order = new Order
            {
                Ticker = signal.Ticker,
                Direction = signal.Direction,
                Type = OrderType.Market,
                Price = signal.Price,
                Volume = signal.Volume,
                Comment = $"{strategyName}: {signal.Comment}"
            };

            await _connector.PlaceOrderAsync(order);

            var tradeEvent = new TradeEvent
            {
                Time = DateTime.UtcNow,
                Ticker = signal.Ticker,
                Direction = signal.Direction.ToString(),
                Price = signal.Price,
                Volume = signal.Volume,
                Strategy = strategyName,
                Comment = signal.Comment
            };

            await _hubContext.Clients.All.SendAsync("OnTradeExecuted", tradeEvent);

            lock (_lock) _todayTrades++;

            _logger.LogInformation("Сделка исполнена: {Ticker} {Dir} {Vol}@{Price:F2} ({Strategy})",
                signal.Ticker, signal.Direction, signal.Volume, signal.Price, strategyName);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Ошибка исполнения сделки");
            await SendError($"Ошибка исполнения: {ex.Message}");
        }
    }

    // === Обновление позиций ===

    /// <summary>Обновить позицию и отправить клиентам</summary>
    public async Task UpdatePositionAsync(string ticker, string direction, double entryPrice, int volume, double currentPrice, double pnl)
    {
        var posEvent = new PositionEvent
        {
            Ticker = ticker,
            Direction = direction,
            EntryPrice = entryPrice,
            Volume = volume,
            CurrentPrice = currentPrice,
            PnL = pnl
        };

        lock (_lock) _positions[ticker] = posEvent;

        await _hubContext.Clients.All.SendAsync("OnPositionUpdated", posEvent);
    }

    /// <summary>Обновить equity и отправить клиентам</summary>
    public async Task UpdateEquityAsync(double equity)
    {
        lock (_lock) _equity = equity;
        await _hubContext.Clients.All.SendAsync("OnEquityUpdate", equity);
    }

    // === Бэктест ===

    /// <summary>Запустить бэктест</summary>
    public Task<BacktestResultEvent> RunBacktestAsync(string strategyName, string ticker, int timeframe,
        string from, string to, Dictionary<string, double> parameters)
    {
        // TODO: интеграция с BacktestEngine из Core
        // Пока возвращаем заглушку
        var result = new BacktestResultEvent
        {
            StrategyName = strategyName,
            Ticker = ticker,
            TotalPnL = 0,
            TotalTrades = 0,
            WinRate = 0,
            ProfitFactor = 0,
            SharpeRatio = 0,
            MaxDrawdown = 0,
            EquityCurve = new List<double>(),
            Duration = TimeSpan.Zero
        };

        _logger.LogInformation("Бэктест {Strategy} на {Ticker}: TODO — интеграция с BacktestEngine", strategyName, ticker);

        return Task.FromResult(result);
    }

    // === Статус ===

    /// <summary>Получить текущий статус сервера</summary>
    public ServerStatus GetStatus()
    {
        lock (_lock)
        {
            return new ServerStatus
            {
                IsRunning = _isRunning,
                IsPaused = _isPaused,
                IsConnectedToBroker = _connector?.IsConnected ?? false,
                Balance = _balance,
                Equity = _equity,
                TodayPnL = _todayPnL,
                TotalPnL = _totalPnL,
                OpenPositions = _positions.Count,
                TodayTrades = _todayTrades,
                ActiveStrategies = _activeStrategies.ToList(),
                Positions = _positions.Values.ToList(),
                ServerTime = DateTime.UtcNow,
                BrokerStatus = _connector?.IsConnected == true ? "Подключён" : "Отключён"
            };
        }
    }

    // === Вспомогательные ===

    private void OnBrokerTrade(Trade trade)
    {
        _logger.LogInformation("Брокер: сделка {Ticker} {Dir} {Vol}@{Price:F2}", 
            trade.Ticker, trade.Direction, trade.Volume, trade.Price);
    }

    private void OnBrokerOrderUpdate(Order order)
    {
        _logger.LogInformation("Брокер: ордер {Id} статус={Status}", order.Id, order.Status);
    }

    private async Task SendError(string message)
    {
        await _hubContext.Clients.All.SendAsync("OnError", message);
    }

    public void Dispose()
    {
        _connector?.Dispose();
    }

    // === Внутренние модели ===

    private class PendingTrade
    {
        public Signal Signal { get; set; } = null!;
        public string StrategyName { get; set; } = string.Empty;
        public DateTime Deadline { get; set; }
    }
}
