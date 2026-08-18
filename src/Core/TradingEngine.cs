using HedgeFund.Core.Averaging;
using HedgeFund.Core.Risk;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;

namespace HedgeFund.Core;

/// <summary>
/// Универсальный торговый движок.
/// Принимает стратегию, усреднение и брокерские операции.
/// </summary>
public class TradingEngine : IDisposable
{
    private readonly IBrokerConnector _broker;
    private readonly IStrategy _strategy;
    private readonly AveragingEngine _averaging;
    private readonly AveragingSettings _settings;
    private RiskGate? _riskGate;
    
    private readonly Dictionary<string, Position> _positions = new();
    private readonly List<Trade> _tradeLog = new();
    private readonly List<string> _eventLog = new();
    
    private bool _isRunning;

    public TradingEngine(IBrokerConnector broker, IStrategy strategy, AveragingSettings settings)
    {
        _broker = broker;
        _strategy = strategy;
        _settings = settings;
        _averaging = new AveragingEngine(settings);
        
        _broker.OnTrade += HandleTrade;
        _broker.OnOrderUpdate += HandleOrderUpdate;
        _broker.OnError += HandleError;
    }

    public void SetRiskGate(RiskGate gate) => _riskGate = gate;

    // === Свойства (для UI) ===
    public bool IsRunning => _isRunning;
    public IReadOnlyDictionary<string, Position> Positions => _positions;
    public IReadOnlyList<Trade> TradeLog => _tradeLog;
    public IReadOnlyList<string> EventLog => _eventLog;
    public AveragingSettings Settings => _settings;

    // === Ручное управление (для UI / тестов) ===
    
    public void Start() => _isRunning = true;
    public void Stop() => _isRunning = false;
    
    /// <summary>Включить/выключить мартингейл (потребуется UI)</summary>
    public void ToggleMartingale()
    {
        _settings.Mode = _settings.Mode == AveragingMode.Fixed 
            ? AveragingMode.Martingale 
            : AveragingMode.Fixed;
        Log($"Режим усреднения: {_settings.Mode}");
    }
    
    /// <summary>Включить/выключить стоп-лосс (потребуется UI)</summary>
    public void ToggleStopLoss()
    {
        _settings.StopLossEnabled = !_settings.StopLossEnabled;
        Log($"Стоп-лосс: {(_settings.StopLossEnabled ? "Вкл" : "Выкл")}");
    }
    
    /// <summary>Установить лимит усреднений (потребуется UI)</summary>
    public void SetMaxAveraging(int max)
    {
        _settings.MaxAveragingCount = max;
        Log($"Лимит усреднений: {(max <= 0 ? "без лимита" : max.ToString())}");
    }
    
    /// <summary>Установить параметры стоп-лосса</summary>
    public void SetStopLoss(double value, bool usePercent)
    {
        _settings.UsePercentStopLoss = usePercent;
        if (usePercent)
            _settings.StopLossPercent = value;
        else
            _settings.StopLossPoints = value;
        Log($"Стоп-лосс: {value}{(usePercent ? "%" : " пт.")}");
    }

    // === Основной цикл ===

    /// <summary>
    /// Обработка нового бара. Вызывается при подписке на свечи.
    /// </summary>
    public async void OnNewCandle(Candle candle, string ticker)
    {
        if (!_isRunning) return;

        // 1. Получаем сигнал от стратегии
        var signal = _strategy.OnCandle(candle, ticker);
        bool hasSignal = signal != null && signal.Direction != SignalDirection.None;

        // 2. Проверяем текущую позицию
        if (_positions.TryGetValue(ticker, out var position) && position.IsOpen)
        {
            // Позиция открыта — принимаем решение об усреднении
            var result = _averaging.Evaluate(position, candle.Close, hasSignal);
            
            switch (result.Decision)
            {
                case AveragingDecision.CloseAll:
                    Log($"[{ticker}] Закрытие: {result.Reason}");
                    await ClosePositionAsync(ticker, position, candle.Close);
                    break;
                    
                case AveragingDecision.StopLoss:
                    Log($"[{ticker}] Стоп-лосс: {result.Reason}");
                    await ClosePositionAsync(ticker, position, candle.Close);
                    break;
                    
                case AveragingDecision.Average:
                    Log($"[{ticker}] Усреднение: {result.Reason}");
                    await AddToPositionAsync(ticker, position, candle.Close, result.Volume, signal!.Direction);
                    break;
                    
                case AveragingDecision.Hold:
                    // Ничего не делаем
                    break;
            }
        }
        else if (hasSignal)
        {
            // Нет позиции — открываем по сигналу стратегии
            Log($"[{ticker}] Вход: {signal!.Comment}");
            await OpenPositionAsync(ticker, signal, candle.Close);
        }
    }

    // === Story 2.1: async void -> async Task ===
    // === Story 2.2: синхронизация позиции с брокером ===

    private async Task OpenPositionAsync(string ticker, Signal signal, double price)
    {
        var order = new Order
        {
            Ticker = ticker,
            Direction = signal.Direction,
            Type = OrderType.Market,
            Price = price,
            Volume = _settings.BaseLotSize,
            Comment = $"Open: {signal.StrategyName}"
        };

        try
        {
            var result = await _broker.PlaceOrderAsync(order);
            // ТОЛЬКО после успешного ордера создаём позицию
            if (result != null && result.Status != OrderStatus.Rejected)
            {
                var position = new Position
                {
                    Ticker = ticker,
                    Direction = signal.Direction,
                    Entries = new List<PositionEntry>
                    {
                        new() { Timestamp = signal.Timestamp, Price = price, Volume = _settings.BaseLotSize, Comment = "Вход" }
                    }
                };
                _positions[ticker] = position;
            }
            else
            {
                Log($"[{ticker}] Ордер отклонён или не подтверждён — позиция НЕ создана");
            }
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] Ошибка открытия: {ex.Message}");
            // Fallback: запрос позиций у брокера
            try
            {
                var brokerPositions = await _broker.GetPositionsAsync();
                var brokerPos = brokerPositions?.FirstOrDefault(p => p.Ticker == ticker);
                if (brokerPos != null && brokerPos.IsOpen)
                {
                    Log($"[{ticker}] FALLBACK: брокер показывает открытую позицию — синхронизируем");
                    _positions[ticker] = new Position
                    {
                        Ticker = ticker,
                        Direction = brokerPos.Direction,
                        Entries = new List<PositionEntry>
                        {
                            new() { Timestamp = DateTime.UtcNow, Price = brokerPos.AveragePrice, Volume = brokerPos.TotalVolume, Comment = "Восстановлено из брокера" }
                        }
                    };
                }
            }
            catch (Exception fallbackEx)
            {
                Log($"[{ticker}] FALLBACK не удался: {fallbackEx.Message}");
            }
        }
    }

    private async Task AddToPositionAsync(string ticker, Position position, double price, int volume, SignalDirection direction)
    {
        var order = new Order
        {
            Ticker = ticker,
            Direction = direction,
            Type = OrderType.Market,
            Price = price,
            Volume = volume,
            Comment = $"Avg #{position.AveragingCount}"
        };

        try
        {
            var result = await _broker.PlaceOrderAsync(order);
            // ТОЛЬКО после успешного ордера обновляем позицию
            if (result != null && result.Status != OrderStatus.Rejected)
            {
                position.Entries.Add(new PositionEntry
                {
                    Timestamp = DateTime.UtcNow,
                    Price = price,
                    Volume = volume,
                    Comment = $"Усреднение #{position.AveragingCount}"
                });
            }
            else
            {
                Log($"[{ticker}] Усреднение отклонено — позиция НЕ обновлена");
            }
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] Ошибка усреднения: {ex.Message}");
            // Fallback: сверка с брокером
            try
            {
                var brokerPositions = await _broker.GetPositionsAsync();
                var brokerPos = brokerPositions?.FirstOrDefault(p => p.Ticker == ticker);
                if (brokerPos != null && brokerPos.TotalVolume != position.TotalVolume)
                {
                    Log($"[{ticker}] FALLBACK: расхождение объёмов (internal={position.TotalVolume}, broker={brokerPos.TotalVolume}) — синхронизируем");
                    position.Entries.Clear();
                    position.Entries.Add(new PositionEntry
                    {
                        Timestamp = DateTime.UtcNow,
                        Price = brokerPos.AveragePrice,
                        Volume = brokerPos.TotalVolume,
                        Comment = "Восстановлено из брокера"
                    });
                }
            }
            catch (Exception fallbackEx)
            {
                Log($"[{ticker}] FALLBACK не удался: {fallbackEx.Message}");
            }
        }
    }

    private async Task ClosePositionAsync(string ticker, Position position, double price)
    {
        // Определяем противоположное направление для закрытия
        var closeDirection = position.Direction == SignalDirection.Buy 
            ? SignalDirection.Sell 
            : SignalDirection.Buy;

        var order = new Order
        {
            Ticker = ticker,
            Direction = closeDirection,
            Type = OrderType.Market,
            Price = price,
            Volume = position.TotalVolume,
            Comment = $"Close. PnL: {position.GetPnL(price):F2}"
        };

        try
        {
            var result = await _broker.PlaceOrderAsync(order);
            // ТОЛЬКО после успешного ордера удаляем позицию
            if (result != null && result.Status != OrderStatus.Rejected)
            {
                _positions.Remove(ticker);
            }
            else
            {
                Log($"[{ticker}] Закрытие отклонено — позиция сохранена");
            }
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] Ошибка закрытия: {ex.Message}");
            // Fallback: проверяем брокера — действительно ли позиция закрылась
            try
            {
                var brokerPositions = await _broker.GetPositionsAsync();
                var stillOpen = brokerPositions?.Any(p => p.Ticker == ticker && p.IsOpen) ?? true;
                if (!stillOpen)
                {
                    Log($"[{ticker}] FALLBACK: брокер показывает позицию закрытой — удаляем из трекинга");
                    _positions.Remove(ticker);
                }
                else
                {
                    Log($"[{ticker}] FALLBACK: брокер подтверждает открытую позицию — сохраняем");
                }
            }
            catch (Exception fallbackEx)
            {
                Log($"[{ticker}] FALLBACK не удался: {fallbackEx.Message}");
            }
        }
    }

    // === Обработка событий ===

    private void HandleTrade(Trade trade)
    {
        _tradeLog.Add(trade);
        Log($"[{trade.Ticker}] Сделка: {trade.Direction} {trade.Volume} @ {trade.Price:F2}");
    }

    private void HandleOrderUpdate(Order order)
    {
        Log($"[{order.Ticker}] Заявка {order.Id}: {order.Status}");
    }

    private void HandleError(string error)
    {
        Log($"Ошибка брокера: {error}");
    }

    private void Log(string message)
    {
        var entry = $"[{DateTime.UtcNow:HH:mm:ss}] {message}";
        _eventLog.Add(entry);
        // TODO: записывать в файл / UI обновление
    }

    public void Dispose()
    {
        _broker.OnTrade -= HandleTrade;
        _broker.OnOrderUpdate -= HandleOrderUpdate;
        _broker.OnError -= HandleError;
    }
}
