using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// PSAR Grid MM + RV/HV Regime Filter.
/// 
/// Логика:
/// 1. RV < HV → LOW VOL → торгуем Grid MM
/// 2. RV >= HV → HIGH VOL → закрываем все позиции, не торгуем
/// 3. SAR×EMA кросс → вход 1 лот + расставляем сетку лимиток против позиции
/// 4. Каждые 35 пт против позиции → +1 лот (grid fill)
/// 5. Каждый уровень: TP = +35 пт от цены заполнения → round trip
/// 6. Закрытие всего: обратный SAR×EMA кросс ИЛИ 50%+ RT + PnL/лот ≥ 28 пт
/// 7. Dynamic lots 1→3: +1000 руб → +1 лот, убыток → -1 лот
/// </summary>
public class GridMmRegimeStrategy : IStrategy
{
    public class Config
    {
        // SAR
        public double SarStart { get; set; } = 0.005;
        public double SarStep { get; set; } = 0.01;
        public double SarMax { get; set; } = 0.2;
        
        // EMA
        public int EmaPeriod { get; set; } = 500;
        
        // Grid
        public double GridStep { get; set; } = 35.0;     // шаг сетки (пт)
        public double GridSpread { get; set; } = 35.0;   // TP на уровень (пт)
        public int MaxGridLevels { get; set; } = 50;
        public double MinProfitPerLot { get; set; } = 28.0; // пт для early close
        public double ClosePct { get; set; } = 0.50;     // 50%+ RT для early close
        
        // RV/HV Regime
        public int RvWindow { get; set; } = 288;
        public int HvWindow { get; set; } = 1440;
        
        // Commission
        public double Commission { get; set; } = 0.90;   // руб RT
        
        // Dynamic lots
        public double LotStepProfit { get; set; } = 1000.0;
        public int MaxLots { get; set; } = 3;
    }

    public enum StrategyMode { Running, Paused, Stopped }
    
    public string Name => "Grid MM Regime";
    public Config Params { get; }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    // Индикаторы
    private readonly ParabolicSAR _sar;
    private readonly EMA _ema;
    
    // RV/HV
    private readonly double[] _returns;
    private int _retIdx;
    private readonly double[] _rvValues;
    private int _rvIdx;
    private double _rvSum;
    private double _currentRv;
    private double _currentHv;
    private int _totalBars;
    
    // Позиция
    private int _posDir;              // 0=flat, 1=long, -1=short
    private double _entryPrice;       // initial entry price
    private bool _entryFilled;        // entry lot still open (not TP'd yet)
    
    // Grid levels
    private struct GridLevel
    {
        public double Price;      // цена лимитки
        public double TpPrice;    // TP = price ± spread
        public bool Filled;       // лимитка исполнена
        public int FillBar;       // бар на котором исполнилась
    }
    private GridLevel[] _grid;
    private int _peakLots;        // макс лотов в этой сессии
    private int _sessionRt;       // round trips в этой сессии
    private double _sessionRealized; // реализованный PnL сессии
    
    // Предыдущие
    private double _prevSar, _prevEma;
    private bool _prevValid;
    private double _prevClose;
    private DateTime _lastSignalTime;
    
    // Dynamic lots
    private int _currentLotLevel;
    private double _cumProfit;
    
    // Очередь сигналов (несколько сигналов на бар)
    private readonly Queue<Signal> _signalQueue = new();
    
    // Экспорт
    public double CurrentSar => _sar.Value;
    public double CurrentEma => _ema.Value;
    public double CurrentRv => _currentRv;
    public double CurrentHv => _currentHv;
    public bool IsRegimeLowVol => _currentHv > 0 && _currentRv < _currentHv;
    public int PositionDirection => _posDir;
    public int CurrentLotLevel => _currentLotLevel;
    public double CurrentGridPnL { get; private set; }
    public int OpenGridLevels { get; private set; }
    public double EntryPrice => _entryPrice;
    public int TotalEntryLots => OpenGridLevels;
    public int TotalTrades { get; private set; }
    public double TotalPnL { get; private set; }
    public double RvHvRatio => _currentHv > 0 ? _currentRv / _currentHv : 0;
    
    // История
    public class TradeRecord
    {
        public DateTime Time { get; set; }
        public string Ticker { get; set; }
        public int Direction { get; set; }
        public double Price { get; set; }
        public int Lots { get; set; }
        public string Comment { get; set; }
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
    private const int MAX_INDICATOR_HISTORY = 500;
    
    private readonly List<string> _log = new();
    public IReadOnlyList<string> Log => _log;

    public GridMmRegimeStrategy(Config? config = null)
    {
        Params = config ?? new Config();
        _sar = new ParabolicSAR(Params.SarStart, Params.SarStep, Params.SarMax);
        _ema = new EMA(Params.EmaPeriod);
        _returns = new double[Params.RvWindow];
        _rvValues = new double[Params.HvWindow];
        _grid = new GridLevel[Params.MaxGridLevels];
        _currentLotLevel = 1;
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        // Если есть отложенные сигналы — выдаём сначала их
        if (_signalQueue.Count > 0)
            return _signalQueue.Dequeue();

        // Дедупликация: если свеча та же и мы flat — пропускаем
        if (candle.Timestamp == _lastSignalTime && _posDir == 0 && _signalQueue.Count == 0)
            return null;
        _lastSignalTime = candle.Timestamp;
        
        double c = candle.Close;
        double hi = candle.High;
        double lo = candle.Low;
        
        // === Обновляем SAR + EMA ===
        double sar = _sar.Update(candle);
        double ema = _ema.Update(candle);
        
        // === Обновляем RV/HV ===
        UpdateRvHv(c);
        
        // === Пропускаем пока индикаторы не прогреты ===
        if (double.IsNaN(sar) || double.IsNaN(ema) || !_prevValid)
        {
            _prevSar = sar; _prevEma = ema;
            _prevValid = !double.IsNaN(sar) && !double.IsNaN(ema);
            return null;
        }
        
        // === SAR×EMA кроссы ===
        bool longEntry = (_prevSar >= _prevEma && sar < ema);
        bool shortEntry = (_prevSar <= _prevEma && sar > ema);
        bool reverseLongExit = (_prevSar <= _prevEma && sar > ema);  // was long, now short signal
        bool reverseShortExit = (_prevSar >= _prevEma && sar < ema); // was short, now long signal
        
        _prevSar = sar; _prevEma = ema;
        
        // Сохраняем историю
        _indicatorHistory.Add(new IndicatorPoint { Time = candle.Timestamp, Sar = sar, Ema = ema });
        if (_indicatorHistory.Count > MAX_INDICATOR_HISTORY)
            _indicatorHistory.RemoveAt(0);
        
        bool lowVol = IsRegimeLowVol;
        
        // === STOP MODE ===
        if (Mode == StrategyMode.Stopped && _posDir != 0)
            return CloseAll(c, ticker, "СТОП ТОРГИ");
        
        // === HIGH VOL: закрыть если в позиции ===
        if (!lowVol && _posDir != 0)
            return CloseAll(c, ticker, "HIGH VOL (RV >= HV)");
        
        // === POSICIA OTKRYTA: grid logic ===
        if (_posDir != 0 && lowVol)
        {
            ProcessGrid(c, hi, lo, candle.Timestamp, ticker);
            
            // Проверяем close conditions
            bool reverseCross = (_posDir == 1 && reverseLongExit) || (_posDir == -1 && reverseShortExit);
            bool shouldClose = false;
            string closeReason = "";
            
            if (reverseCross)
            {
                shouldClose = true;
                closeReason = "SAR×EMA reverse cross";
            }
            else if (_peakLots > 1 && _sessionRt >= (int)(_peakLots * Params.ClosePct) && OpenGridLevels > 0)
            {
                if (CurrentGridPnL / OpenGridLevels >= Params.MinProfitPerLot)
                {
                    shouldClose = true;
                    closeReason = $"Profit target ({_sessionRt} RT, {CurrentGridPnL / OpenGridLevels:F0} пт/лот)";
                }
            }
            
            if (shouldClose)
                return CloseAll(c, ticker, closeReason);
            
            // Если есть сигналы из ProcessGrid — выдаём
            if (_signalQueue.Count > 0)
                return _signalQueue.Dequeue();
            
            return null;
        }
        
        // === ВХОД: flat + running + LOW VOL ===
        if (_posDir == 0 && Mode == StrategyMode.Running && lowVol)
        {
            if (longEntry)
            {
                OpenPosition(1, c, candle.Timestamp, ticker);
                return new Signal
                {
                    Timestamp = candle.Timestamp, Ticker = ticker,
                    Direction = SignalDirection.Buy, Source = SignalSource.Strategy,
                    Volume = _currentLotLevel, StrategyName = Name, Price = c,
                    Comment = "LONG: SAR<EMA"
                };
            }
            if (shortEntry)
            {
                OpenPosition(-1, c, candle.Timestamp, ticker);
                return new Signal
                {
                    Timestamp = candle.Timestamp, Ticker = ticker,
                    Direction = SignalDirection.Sell, Source = SignalSource.Strategy,
                    Volume = _currentLotLevel, StrategyName = Name, Price = c,
                    Comment = "SHORT: SAR>EMA"
                };
            }
        }
        
        return null;
    }

    private void OpenPosition(int dir, double price, DateTime time, string ticker)
    {
        _posDir = dir;
        _entryPrice = price;
        _entryFilled = true;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = 1;
        
        // Расставляем grid уровни
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            _grid[j] = new GridLevel
            {
                Price = dir == 1 ? price - Params.GridStep * (j + 1) : price + Params.GridStep * (j + 1),
                TpPrice = dir == 1 ? price - Params.GridStep * (j + 1) + Params.GridSpread 
                                    : price + Params.GridStep * (j + 1) - Params.GridSpread,
                Filled = false, FillBar = 0
            };
        }
        
        LogMsg($"[{ticker}] OPEN {dir} @ {price:F0}, lots={_currentLotLevel}, RV={_currentRv:F4} HV={_currentHv:F4} ratio={RvHvRatio:F2}");
        _trades.Add(new TradeRecord { Time = time, Ticker = ticker, Direction = dir, Price = price, Lots = _currentLotLevel, Comment = "Entry" });
    }

    private void ProcessGrid(double c, double hi, double lo, DateTime time, string ticker)
    {
        int d = _posDir;
        int barNum = _totalBars;
        
        // === 1. Round trips: TP на заполненных уровнях ===
        if (_entryFilled)
        {
            bool tpHit = d == 1 ? hi >= _entryPrice + Params.GridSpread : lo <= _entryPrice - Params.GridSpread;
            if (tpHit)
            {
                _entryFilled = false;
                double profit = Params.GridSpread - 2 * Params.Commission;
                _sessionRealized += profit * _currentLotLevel;
                _sessionRt++;
                _trades.Add(new TradeRecord { Time = time, Ticker = ticker, Direction = -d, Price = d == 1 ? _entryPrice + Params.GridSpread : _entryPrice - Params.GridSpread, Lots = _currentLotLevel, Comment = "Entry TP" });
                _signalQueue.Enqueue(new Signal
                {
                    Timestamp = time, Ticker = ticker,
                    Direction = d == 1 ? SignalDirection.Sell : SignalDirection.Buy,
                    Source = SignalSource.TakeProfit, Volume = _currentLotLevel,
                    StrategyName = Name, Price = c,
                    Comment = "Entry TP"
                });
            }
        }
        
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            if (!_grid[j].Filled || _grid[j].FillBar == barNum)
                continue;
            
            bool tpHit = d == 1 ? hi >= _grid[j].TpPrice : lo <= _grid[j].TpPrice;
            if (tpHit)
            {
                _grid[j].Filled = false;
                double profit = Params.GridSpread - 2 * Params.Commission;
                _sessionRealized += profit * _currentLotLevel;
                _sessionRt++;
                _trades.Add(new TradeRecord { Time = time, Ticker = ticker, Direction = -d, Price = _grid[j].TpPrice, Lots = _currentLotLevel, Comment = $"Grid[{j}] TP" });
                _signalQueue.Enqueue(new Signal
                {
                    Timestamp = time, Ticker = ticker,
                    Direction = d == 1 ? SignalDirection.Sell : SignalDirection.Buy,
                    Source = SignalSource.TakeProfit, Volume = _currentLotLevel,
                    StrategyName = Name, Price = c,
                    Comment = $"Grid[{j}] TP +{profit:F0}руб"
                });
            }
        }
        
        // === 2. Новые fills: цена дошла до уровня ===
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            if (_grid[j].Filled) continue;
            
            bool fillHit = d == 1 ? lo <= _grid[j].Price : hi >= _grid[j].Price;
            if (fillHit)
            {
                _grid[j].Filled = true;
                _grid[j].FillBar = barNum;
                _trades.Add(new TradeRecord { Time = time, Ticker = ticker, Direction = d, Price = _grid[j].Price, Lots = _currentLotLevel, Comment = $"Grid[{j}] fill" });
                _signalQueue.Enqueue(new Signal
                {
                    Timestamp = time, Ticker = ticker,
                    Direction = d == 1 ? SignalDirection.Buy : SignalDirection.Sell,
                    Source = SignalSource.Grid, Volume = _currentLotLevel,
                    StrategyName = Name, Price = c,
                    Comment = $"Grid[{j}] fill @ {_grid[j].Price:F0}"
                });
            }
        }
        
        // === 3. Считаем текущую позицию ===
        OpenGridLevels = _currentLotLevel * (1 + CountFilledLevels());
        if (OpenGridLevels > _peakLots) _peakLots = OpenGridLevels;
        
        // Unrealized PnL
        double ur = 0;
        if (_entryFilled)
            ur += d == 1 ? (c - _entryPrice) : (_entryPrice - c);
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            if (_grid[j].Filled)
                ur += d == 1 ? (c - _grid[j].Price) : (_grid[j].Price - c);
        }
        CurrentGridPnL = _sessionRealized + ur * _currentLotLevel - OpenGridLevels * Params.Commission;
    }

    private int CountFilledLevels()
    {
        int count = 0;
        for (int j = 0; j < Params.MaxGridLevels; j++)
            if (_grid[j].Filled) count++;
        return count;
    }

    private Signal CloseAll(double price, string ticker, string reason)
    {
        if (_posDir == 0) return null;
        
        int d = _posDir;
        double ur = 0;
        int totalLots = 0;
        
        if (_entryFilled)
        {
            ur += d == 1 ? (price - _entryPrice) : (_entryPrice - price);
            totalLots += _currentLotLevel;
        }
        for (int j = 0; j < Params.MaxGridLevels; j++)
        {
            if (_grid[j].Filled)
            {
                ur += d == 1 ? (price - _grid[j].Price) : (_grid[j].Price - price);
                totalLots += _currentLotLevel;
            }
        }
        
        double pnl = _sessionRealized + ur * _currentLotLevel - totalLots * Params.Commission;
        
        LogMsg($"[{ticker}] CLOSE: {reason} | PnL={pnl:F0} | lots={totalLots}/{_peakLots} | RT={_sessionRt}");
        TotalTrades++;
        TotalPnL += pnl;
        
        // Dynamic lots
        if (pnl < 0)
        {
            _currentLotLevel = Math.Max(1, _currentLotLevel - 1);
            _cumProfit = 0;
            LogMsg($"[DYN] Loss ({pnl:F0}), lots → {_currentLotLevel}");
        }
        else
        {
            _cumProfit += pnl;
            if (_cumProfit >= Params.LotStepProfit && _currentLotLevel < Params.MaxLots)
            {
                _currentLotLevel++;
                _cumProfit = 0;
                LogMsg($"[DYN] +1000 руб, lots → {_currentLotLevel}");
            }
        }
        
        _trades.Add(new TradeRecord { Time = DateTime.UtcNow, Ticker = ticker, Direction = -d, Price = price, Lots = totalLots, Comment = reason });
        
        var signal = new Signal
        {
            Timestamp = DateTime.UtcNow, Ticker = ticker,
            Direction = d == 1 ? SignalDirection.Sell : SignalDirection.Buy,
            Source = SignalSource.Exit, Volume = totalLots,
            StrategyName = Name, Price = price,
            Comment = reason
        };
        
        _posDir = 0; _entryPrice = 0; _entryFilled = false;
        _sessionRt = 0; _sessionRealized = 0; _peakLots = 0;
        CurrentGridPnL = 0; OpenGridLevels = 0;
        _signalQueue.Clear();
        return signal;
    }

    private void UpdateRvHv(double c)
    {
        _totalBars++;
        if (_totalBars < 2) { _prevClose = c; return; }
        
        double ret = Math.Log(c / _prevClose);
        _returns[_retIdx] = ret;
        _retIdx = (_retIdx + 1) % Params.RvWindow;
        
        if (_totalBars >= Params.RvWindow + 1)
        {
            double mean = 0;
            for (int i = 0; i < Params.RvWindow; i++) mean += _returns[i];
            mean /= Params.RvWindow;
            double variance = 0;
            for (int i = 0; i < Params.RvWindow; i++)
            {
                double d = _returns[i] - mean;
                variance += d * d;
            }
            variance /= Params.RvWindow;
            _currentRv = Math.Sqrt(variance) * Math.Sqrt(1440.0 * 365.0);
            
            if (_rvIdx < Params.HvWindow)
            {
                _rvValues[_rvIdx] = _currentRv;
                _rvSum += _currentRv;
                _rvIdx++;
            }
            else
            {
                int oldestIdx = _rvIdx % Params.HvWindow;
                _rvSum -= _rvValues[oldestIdx];
                _rvValues[oldestIdx] = _currentRv;
                _rvSum += _currentRv;
                _rvIdx++;
            }
            _currentHv = _rvSum / Math.Min(_rvIdx, Params.HvWindow);
        }
        _prevClose = c;
    }

    public void Reset()
    {
        _sar.Reset(); _ema.Reset();
        _posDir = 0; _entryPrice = 0; _entryFilled = false;
        _prevValid = false; _totalBars = 0;
        _retIdx = 0; _rvIdx = 0; _rvSum = 0;
        _currentRv = 0; _currentHv = 0;
        _currentLotLevel = 1; _cumProfit = 0;
        TotalTrades = 0; TotalPnL = 0;
        CurrentGridPnL = 0; OpenGridLevels = 0;
        _signalQueue.Clear();
        _log.Clear(); _trades.Clear(); _indicatorHistory.Clear();
    }

    private void LogMsg(string msg)
    {
        _log.Add($"[{DateTime.UtcNow:HH:mm:ss}] {msg}");
        if (_log.Count > 1000) _log.RemoveAt(0);
    }
}
