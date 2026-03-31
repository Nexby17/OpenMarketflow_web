
using HedgeFund.Core.Averaging;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;

namespace HedgeFund.Core;

/// <summary>
/// Центральный торговый движок.
/// Связывает стратегии, усреднение и брокерские коннекторы.
/// </summary>
public class TradingEngine : IDisposable
{
    private readonly IBrokerConnector _broker;
    private readonly IStrategy _strategy;
    private readonly AveragingEngine _averaging;
    private readonly AveragingSettings _settings;
    
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

    // === Состояние (для UI) ===
    public bool IsRunning => _isRunning;
    public IReadOnlyDictionary<string, Position> Positions => _positions;
    public IReadOnlyList<Trade> TradeLog => _tradeLog;
    public IReadOnlyList<string> EventLog => _eventLog;
    public AveragingSettings Settings => _settings;

    // === Управление (для UI кнопок) ===
    
    public void Start() => _isRunning = true;
    public void Stop() => _isRunning = false;
    
    /// <summary>Переключить мартингейл (кнопка UI)</summary>
    public void ToggleMartingale()
    {
        _settings.Mode = _settings.Mode == AveragingMode.Fixed 
            ? AveragingMode.Martingale 
            : AveragingMode.Fixed;
        Log($"Режим усреднения: {_settings.Mode}");
    }
    
    /// <summary>Включить/выключить стоп-лосс (кнопка UI)</summary>
    public void ToggleStopLoss()
    {
        _settings.StopLossEnabled = !_settings.StopLossEnabled;
        Log($"Стоп-лосс: {(_settings.StopLossEnabled ? "ВКЛ" : "ВЫКЛ")}");
    }
    
    /// <summary>Установить лимит усреднений (кнопка UI)</summary>
    public void SetMaxAveraging(int max)
    {
        _settings.MaxAveragingCount = max;
        Log($"Лимит усреднений: {(max <= 0 ? "без лимита" : max.ToString())}");
    }
    
    /// <summary>Установить размер стоп-лосса</summary>
    public void SetStopLoss(double value, bool usePercent)
    {
        _settings.UsePercentStopLoss = usePercent;
        if (usePercent)
            _settings.StopLossPercent = value;
        else
            _settings.StopLossPoints = value;
        Log($"Стоп-лосс: {value}{(usePercent ? "%" : " п.")}");
    }

    // === Основная логика ===

    /// <summary>
    /// Обработчик новой свечи. Вызывается из подписки на маркетдату.
    /// </summary>
    public void OnNewCandle(Candle candle, string ticker)
    {
        if (!_isRunning) return;

        // 1. Получаем сигнал от стратегии
        var signal = _strategy.OnCandle(candle, ticker);
        bool hasSignal = signal != null && signal.Direction != SignalDirection.None;

        // 2. Проверяем текущую позицию
        if (_positions.TryGetValue(ticker, out var position) && position.IsOpen)
        {
            // Позиция открыта — работает модуль усреднения
            var result = _averaging.Evaluate(position, candle.Close, hasSignal);
            
            switch (result.Decision)
            {
                case AveragingDecision.CloseAll:
                    Log($"[{ticker}] ЗАКРЫТИЕ: {result.Reason}");
                    ClosePosition(ticker, position, candle.Close);
                    break;
                    
                case AveragingDecision.StopLoss:
                    Log($"[{ticker}] СТОП-ЛОСС: {result.Reason}");
                    ClosePosition(ticker, position, candle.Close);
                    break;
                    
                case AveragingDecision.Average:
                    Log($"[{ticker}] УСРЕДНЕНИЕ: {result.Reason}");
                    AddToPosition(ticker, position, candle.Close, result.Volume, signal!.Direction);
                    break;
                    
                case AveragingDecision.Hold:
                    // Ничего не делаем
                    break;
            }
        }
        else if (hasSignal)
        {
            // Нет позиции — открываем по сигналу стратегии
            Log($"[{ticker}] ВХОД: {signal!.Comment}");
            OpenPosition(ticker, signal, candle.Close);
        }
    }

    // === Исполнение ===

    private async void OpenPosition(string ticker, Signal signal, double price)
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
            await _broker.PlaceOrderAsync(order);
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] ОШИБКА ЗАЯВКИ: {ex.Message}");
        }
    }

    private async void AddToPosition(string ticker, Position position, double price, int volume, SignalDirection direction)
    {
        position.Entries.Add(new PositionEntry
        {
            Timestamp = DateTime.UtcNow,
            Price = price,
            Volume = volume,
            Comment = $"Усреднение #{position.AveragingCount}"
        });

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
            await _broker.PlaceOrderAsync(order);
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] ОШИБКА УСРЕДНЕНИЯ: {ex.Message}");
        }
    }

    private async void ClosePosition(string ticker, Position position, double price)
    {
        // Обратное направление для закрытия
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
            await _broker.PlaceOrderAsync(order);
            _positions.Remove(ticker);
        }
        catch (Exception ex)
        {
            Log($"[{ticker}] ОШИБКА ЗАКРЫТИЯ: {ex.Message}");
        }
    }

    // === Обработчики событий ===

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
        Log($"ОШИБКА БРОКЕРА: {error}");
    }

    private void Log(string message)
    {
        var entry = $"[{DateTime.UtcNow:HH:mm:ss}] {message}";
        _eventLog.Add(entry);
        // TODO: запись в файл / UI обновление
    }

    public void Dispose()
    {
        _broker.OnTrade -= HandleTrade;
        _broker.OnOrderUpdate -= HandleOrderUpdate;
        _broker.OnError -= HandleError;
    }
}
