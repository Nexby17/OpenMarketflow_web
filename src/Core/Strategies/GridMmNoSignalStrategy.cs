using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;
using HedgeFund.Core.Risk;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// PSAR×EMA Grid MM NoSignal — стратегия без закрытия по обратному сигналу.
///
/// Ключевое отличие от GridMmRegimeStrategy:
/// - Вход: SAR×EMA кросс
/// - ВЫХОД: ТОЛЬКО когда грид вывёл позицию в прибыль (profit target)
/// - БЕЗ закрытия по обратному SAR×EMA сигналу
///
/// Оптимизация (SiM6, 2026-02-20 to 2026-04-21):
/// SAR(0.005/0.005/0.2), EMA=30
/// Grid: step=20, spread=50, max_levels=50
/// Close: RT>=30% + min_profit=28 пт/лот
/// MaxLots: 3 (динамический)
///
/// Результаты оптимизации (220 combos):
/// 111/200 прибыльных (50.5%)
/// Лучшая: +223,781₽ (3,754₽/день), WR=100%, Sharpe=9.26
/// </summary>
public class GridMmNoSignalStrategy : IStrategy
{
    public class Config
    {
        // SAR
        public double SarStart { get; set; } = 0.005;
        public double SarStep { get; set; } = 0.005;
        public double SarMax { get; set; } = 0.2;

        // EMA
        public int EmaPeriod { get; set; } = 30;

        // Grid
        public double GridStep { get; set; } = 20.0;
        public double GridSpread { get; set; } = 50.0;
        public int MaxGridLevels { get; set; } = 50;
        public double MinProfitPerLot { get; set; } = 28.0;
        public double ClosePct { get; set; } = 0.30;

        // Commission
        public double Commission { get; set; } = 0.90;

        // Dynamic lots
        public double LotStepProfit { get; set; } = 1000.0;
        public int MaxLots { get; set; } = 3;
    }

    public enum StrategyMode { Running, Paused, Stopped }

    public string Name => $"Grid MM NoSignal SAR({Params.SarStart}/{Params.SarStep}/{Params.SarMax}) EMA{Params.EmaPeriod}";
    public Config Params { get; }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    // Индикаторы
    private readonly ParabolicSAR _sar;
    private readonly EMA _ema;

    // Позиция
    private int _posDir;              // 0=flat, 1=long, -1=short
    private double _entryPrice;

    // Grid state — заполняется Launcher-ом через обратные вызовы
    private struct GridLevel
    {
        public double Price;          // цена лимитки
        public double TpPrice;        // TP = price + spread (в нашу сторону)
        public bool Filled;           // лимитка исполнена
        public string? BuyOrderId;    // ID лимитки на покупку
        public string? TpOrderId;     // ID лимитки на TP
    }
    private GridLevel[] _grid;
    private int _peakLots;
    private int _sessionRt;           // round trips
    private double _sessionRealized;
    private bool _entryOpen;          // entry лот ещё не закрыт TP

    // Предыдущие значения индикаторов
    private double _prevSar, _prevEma;
    private bool _prevValid;

    // Dynamic lots
    private int _currentLotLevel;
    private double _cumProfit;
    private double _portfolioValue; // устанавливается из Launcher

    // История
    public int TotalTrades { get; private set; }
    public double TotalPnL { get; private set; }
    public int PositionDirection => _posDir;
    public int CurrentLotLevel => _currentLotLevel;
    public double EntryPrice => _entryPrice;

    /// <summary>Количество открытых лотов (entry + grid fills)</summary>
    public int OpenLots
    {
        get
        {
            int lots = _entryOpen ? _currentLotLevel : 0;
            for (int j = 0; j < Params.MaxGridLevels; j++)
                if (_grid[j].Filled) lots += _currentLotLevel;
            return lots;
        }
    }

    /// <summary>Количество заполненных grid уровней</summary>
    public int FilledGridLevels
    {
        get
        {
            int c = 0;
            for (int j = 0; j < Params.MaxGridLevels; j++)
                if (_grid[j].Filled) c++;
            return c;
        }
    }

    /// <summary>Реализованный PnL сессии + нереализованный по текущей цене</summary>
    public double CalcGridPnL(double currentPrice)
    {
        if (_posDir == 0) return 0;
        double ur = 0;
        if (_entryOpen)
            ur += _posDir == 1 ? (currentPrice - _entryPrice) : (_entryPrice - currentPrice);
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            if (_grid[j].Filled)
                ur += _posDir == 1 ? (currentPrice - _grid[j].Price) : (_grid[j].Price - currentPrice);
        }
        int ol = OpenLots;
        return _sessionRealized + ur * _currentLotLevel - ol * Params.Commission;
    }

    // События для Launcher
    public class StrategyEvent
    {
        public enum EventType
        {
            EntryMarket,          // Вход маркетом 1 лот
            GridLimitOrders,      // Расставить grid лимитки
            CloseAllMarket,       // Закрыть всё маркетом + отменить все лимитки
            CancelAllLimits,      // Отменить все лимитки (перед close)
        }
        public EventType Type { get; set; }
        public int Direction { get; set; }   // 1=long, -1=short
        public double Price { get; set; }    // цена входа
        public int Volume { get; set; }      // кол-во лотов
        public string Reason { get; set; } = "";
        public double[] GridPrices { get; set; } = Array.Empty<double>();      // цены grid лимиток
        public double[] GridTpPrices { get; set; } = Array.Empty<double>();    // цены TP лимиток
    }

    private readonly List<StrategyEvent> _pendingEvents = new();
    public bool HasPendingEvents => _pendingEvents.Count > 0;
    public StrategyEvent? GetNextEvent()
    {
        if (_pendingEvents.Count == 0) return null;
        var e = _pendingEvents[0];
        _pendingEvents.RemoveAt(0);
        return e;
    }

    // Лог
    private readonly List<string> _log = new();
    public IReadOnlyList<string> Log => _log;

    public GridMmNoSignalStrategy(Config? config = null)
    {
        Params = config ?? new Config();
        _sar = new ParabolicSAR(Params.SarStart, Params.SarStep, Params.SarMax);
        _ema = new EMA(Params.EmaPeriod);
        _grid = new GridLevel[Params.MaxGridLevels];
        _currentLotLevel = 1;
    }

    /// <summary>
    /// Основной метод обработки свечи. Вызывается Launcher-ом.
    /// Генерирует события (вход/выход), которые Launcher исполняет через брокера.
    /// </summary>
    public Signal? OnCandle(Candle candle, string ticker)
    {
        double c = candle.Close;

        // Обновляем индикаторы
        double sar = _sar.Update(candle);
        double ema = _ema.Update(candle);

        // Пропускаем пока не прогреты
        if (double.IsNaN(sar) || double.IsNaN(ema) || !_prevValid)
        {
            _prevSar = sar; _prevEma = ema;
            _prevValid = !double.IsNaN(sar) && !double.IsNaN(ema);
            return null;
        }

        // SAR×EMA кроссы (только для входа!)
        bool longSignal = (_prevSar >= _prevEma && sar < ema);
        bool shortSignal = (_prevSar <= _prevEma && sar > ema);

        _prevSar = sar; _prevEma = ema;

        // Export for UI
        CurrentSar = sar;
        CurrentEma = ema;

        // Сохраняем в историю для графика
        if (!double.IsNaN(sar) && !double.IsNaN(ema))
        {
            _indicatorHistory.Add(new IndicatorPoint { Time = candle.Timestamp, Sar = sar, Ema = ema });
            if (_indicatorHistory.Count > 5000) _indicatorHistory.RemoveAt(0);
        }

        // Динамический sizing — обновляем MaxLots на основе волатильности
        if (_portfolioValue > 0 && candle.High > 0)
        {
            double volEstimate = RiskManager.EstimateVolFromRange(candle.High, candle.Low, candle.Close);
            int riskAdjLots = RiskManager.CalculateMaxLots(_portfolioValue, c, 12000.0, volEstimate);
            Params.MaxLots = Math.Max(1, Math.Min(riskAdjLots, Params.MaxLots));
        }

        // STOP MODE — закрываем если в позиции
        if (Mode == StrategyMode.Stopped && _posDir != 0)
        {
            EmitCloseAll("СТОП ТОРГИ");
            return null;
        }

        // В позиции — проверяем ТОЛЬКО profit target (НЕ закрываем по сигналу!)
        if (_posDir != 0)
        {
            // БЕЗ проверки обратного SAR×EMA сигнала!
            // Закрытие только когда грид вывёл позицию в прибыль

            // Profit target: RT >= 30% от peak + PnL/лот >= 28 пт
            if (_peakLots > 1 && _sessionRt >= (int)(_peakLots * Params.ClosePct) && OpenLots > 0)
            {
                double gridPnl = CalcGridPnL(c);
                if (gridPnl / OpenLots >= Params.MinProfitPerLot)
                {
                    EmitCloseAll($"Profit target ({_sessionRt} RT, {gridPnl / OpenLots:F0} пт/лот)");
                    return null;
                }
            }

            return null;
        }

        // Flat + Running — проверяем вход по SAR×EMA
        if (Mode == StrategyMode.Running && _posDir == 0)
        {
            if (longSignal)
            {
                EmitEntry(1, c);
            }
            else if (shortSignal)
            {
                EmitEntry(-1, c);
            }
        }

        return null;
    }

    private void EmitEntry(int dir, double price)
    {
        _posDir = dir;
        _entryPrice = price;
        _entryOpen = true;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = _currentLotLevel;

        // Считаем grid уровни
        double[] prices = new double[Params.MaxGridLevels];
        double[] tpPrices = new double[Params.MaxGridLevels];

        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            double levelPrice = dir == 1
                ? price - Params.GridStep * (j + 1)
                : price + Params.GridStep * (j + 1);
            double tpPrice = dir == 1
                ? levelPrice + Params.GridSpread
                : levelPrice - Params.GridSpread;

            prices[j] = levelPrice;
            tpPrices[j] = tpPrice;

            _grid[j] = new GridLevel
            {
                Price = levelPrice,
                TpPrice = tpPrice,
                Filled = false,
                BuyOrderId = null,
                TpOrderId = null
            };
        }

        // Событие: вход маркетом
        _pendingEvents.Add(new StrategyEvent
        {
            Type = StrategyEvent.EventType.EntryMarket,
            Direction = dir,
            Price = price,
            Volume = _currentLotLevel,
            Reason = dir == 1 ? "LONG: SAR<EMA (NoSignal)" : "SHORT: SAR>EMA (NoSignal)"
        });

        // Событие: расставить лимитки
        _pendingEvents.Add(new StrategyEvent
        {
            Type = StrategyEvent.EventType.GridLimitOrders,
            Direction = dir,
            Price = price,
            Volume = _currentLotLevel,
            GridPrices = prices,
            GridTpPrices = tpPrices,
            Reason = $"Grid: {Params.MaxGridLevels} уровней, step={Params.GridStep}, spread={Params.GridSpread}"
        });

        LogMsg($"ENTRY {dir} @ {price:F0}, grid {Params.MaxGridLevels} levels (NoSignal mode)");
    }

    private void EmitCloseAll(string reason)
    {
        _pendingEvents.Add(new StrategyEvent
        {
            Type = StrategyEvent.EventType.CloseAllMarket,
            Direction = _posDir,
            Price = 0,
            Volume = OpenLots,
            Reason = reason
        });

        // Закрываем позицию в стратегии
        double pnl = CalcGridPnL(_entryPrice); // approximate
        TotalTrades++;

        // Dynamic lots
        if (pnl < 0)
        {
            _currentLotLevel = Math.Max(1, _currentLotLevel - 1);
            _cumProfit = 0;
        }
        else
        {
            _cumProfit += pnl;
            if (_cumProfit >= Params.LotStepProfit && _currentLotLevel < Params.MaxLots)
            {
                _currentLotLevel++;
                _cumProfit = 0;
            }
        }

        LogMsg($"CLOSE: {reason} | approx PnL={pnl:F0}");

        _posDir = 0;
        _entryPrice = 0;
        _entryOpen = false;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = 0;

        // Очищаем grid
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            _grid[j] = new GridLevel();
        }
    }

    /// <summary>
    /// Вызывается Launcher-ом когда grid лимитка заполнилась.
    /// </summary>
    public void OnGridFill(int levelIndex)
    {
        if (levelIndex < 0 || levelIndex >= Params.MaxGridLevels) return;
        _grid[levelIndex].Filled = true;

        int ol = OpenLots;
        if (ol > _peakLots) _peakLots = ol;

        LogMsg($"Grid[{levelIndex}] filled @ {_grid[levelIndex].Price:F0}, open={ol} lots");
    }

    /// <summary>
    /// Вызывается Launcher-ом когда TP лимитка заполнилась (round trip).
    /// </summary>
    public void OnGridTp(int levelIndex)
    {
        if (levelIndex < 0 || levelIndex >= Params.MaxGridLevels) return;

        double profit = Params.GridSpread - 2 * Params.Commission;
        _sessionRealized += profit * _currentLotLevel;
        _sessionRt++;

        // Уровень свободен — можно переиспользовать
        _grid[levelIndex].Filled = false;
        _grid[levelIndex].BuyOrderId = null;
        _grid[levelIndex].TpOrderId = null;

        LogMsg($"Grid[{levelIndex}] TP +{profit * _currentLotLevel:F0} руб (RT #{_sessionRt})");
    }

    /// <summary>
    /// Вызывается Launcher-ом когда entry TP заполнился.
    /// </summary>
    public void OnEntryTp()
    {
        if (!_entryOpen) return;
        _entryOpen = false;

        double profit = Params.GridSpread - 2 * Params.Commission;
        _sessionRealized += profit * _currentLotLevel;
        _sessionRt++;

        LogMsg($"Entry TP +{profit * _currentLotLevel:F0} руб (RT #{_sessionRt})");
    }

    /// <summary>
    /// Вызывается Launcher-ом после реального close all.
    /// Передаёт реальный PnL для учёта.
    /// </summary>
    public void OnCloseAllComplete(double realizedPnL)
    {
        TotalPnL += realizedPnL;

        // Dynamic lots на основе реального PnL
        if (realizedPnL < 0)
        {
            _currentLotLevel = Math.Max(1, _currentLotLevel - 1);
            _cumProfit = 0;
            LogMsg($"[DYN] Loss ({realizedPnL:F0}), lots → {_currentLotLevel}");
        }
        else
        {
            _cumProfit += realizedPnL;
            if (_cumProfit >= Params.LotStepProfit && _currentLotLevel < Params.MaxLots)
            {
                _currentLotLevel++;
                _cumProfit = 0;
                LogMsg($"[DYN] +{Params.LotStepProfit:F0} руб, lots → {_currentLotLevel}");
            }
        }

        LogMsg($"Session PnL: {realizedPnL:F0}, total PnL: {TotalPnL:F0}, lots: {_currentLotLevel}");
    }

    /// <summary>
    /// Сохраняет ID ордера для grid уровня (для отслеживания).
    /// </summary>
    public void SetGridOrderIds(int levelIndex, string? buyOrderId, string? tpOrderId)
    {
        if (levelIndex >= 0 && levelIndex < Params.MaxGridLevels)
        {
            _grid[levelIndex].BuyOrderId = buyOrderId;
            _grid[levelIndex].TpOrderId = tpOrderId;
        }
    }

    public double CurrentSar { get; private set; }
    public double CurrentEma { get; private set; }
    public double CurrentRv => 0.0;
    public double CurrentHv => 0.0;
    public bool IsRegimeLowVol => true;
    public int TotalEntryLots => OpenLots;
    public double RvHvRatio => 0.0;

    // Legacy compatibility
    public class TradeRecord
    {
        public DateTime Time { get; set; }
        public string Ticker { get; set; } = "";
        public int Direction { get; set; }
        public double Price { get; set; }
        public int Lots { get; set; }
        public string Comment { get; set; } = "";
    }
    private readonly List<TradeRecord> _trades = new();
    public IReadOnlyList<TradeRecord> Trades => _trades;

    public class IndicatorPoint
    {
        public DateTime Time { get; set; }
        public double Sar { get; set; }
        public double Ema { get; set; }
    }
    private readonly List<IndicatorPoint> _indicatorHistory = new();
    public IReadOnlyList<IndicatorPoint> IndicatorHistory => _indicatorHistory;

    public void SetPortfolioValue(double value) => _portfolioValue = value;

    public void Reset()
    {
        _sar.Reset(); _ema.Reset();
        _posDir = 0; _entryPrice = 0; _entryOpen = false;
        _prevValid = false;
        _currentLotLevel = 1; _cumProfit = 0;
        TotalTrades = 0; TotalPnL = 0;
        _sessionRt = 0; _sessionRealized = 0; _peakLots = 0;
        _pendingEvents.Clear();
        _log.Clear();
        _grid = new GridLevel[Params.MaxGridLevels];
    }

    /// <summary>
    /// Восстанавливает позицию после разрыва связи
    /// </summary>
    public void RestorePosition(int direction, double entryPrice, int lots)
    {
        _posDir = direction;
        _entryPrice = entryPrice;
        _entryOpen = true;
        _currentLotLevel = lots;
        _peakLots = Math.Max(_peakLots, lots);
        _log.Add($"[RESTORE] Position restored: {direction} {lots}x @ {entryPrice:F0}");
    }

    /// <summary>
    /// Очищает позицию (когда нет реальной позиции)
    /// </summary>
    public void ClearPosition()
    {
        _posDir = 0;
        _entryPrice = 0;
        _entryOpen = false;
        _grid = new GridLevel[Params.MaxGridLevels];
        _log.Add("[CLEAR] Position cleared");
    }

    public string GetStatus()
    {
        string dir = _posDir switch { 1 => "LONG", -1 => "SHORT", _ => "FLAT" };
        return $"{Name} [{Mode}] {dir} entry={_entryPrice:F0} " +
               $"open={OpenLots} lots | RT={_sessionRt} " +
               $"| trades={TotalTrades} totalPnL={TotalPnL:F0}";
    }

    private void LogMsg(string msg)
    {
        _log.Add($"[{DateTime.UtcNow:HH:mm:ss}] {msg}");
        if (_log.Count > 1000) _log.RemoveAt(0);
    }
}
