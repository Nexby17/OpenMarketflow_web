using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Williams VStop Pure — чистый flip без грида.
/// 
/// Логика:
/// 1. VStop trend flip up → Buy 1 лот
/// 2. VStop trend flip down → Sell 1 лот (close + reverse)
/// 3. Всегда в позиции. Нет стопов, нет TP, нет грида.
///
/// Лучшие параметры (30 сек SI, 56 дней):
///   Period=12, Mult=2.0: ppd=2,217, Sharpe=2.81, DD=-655
///   Period=6,  Mult=1.5: ppd=2,952, Sharpe=3.19, DD=-587
///
/// ⚠️ Только 56 дней данных! На 1 мин — убыточен. Для бумажной торговли.
/// </summary>
public class VStopPureStrategy : IStrategy
{
    public class Config
    {
        public int Period { get; set; } = 12;
        public double Mult { get; set; } = 2.0;
        public double Commission { get; set; } = 0.60;
        public int BaseLots { get; set; } = 1;
    }

    public enum StrategyMode { Running, Paused, Stopped }

    public string Name => $"VStop Pure {Params.Period}/{Params.Mult:F1}";
    public Config Params { get; }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    // VStop state
    private readonly double[] _atr;
    private double _vstop;
    private int _trend;       // 1=up, -1=down
    private double _maxPrice, _minPrice;
    private double _af;       // ATR EMA factor
    private int _barCount;
    private readonly double[] _trBuf;  // true range buffer for initial ATR
    private double _atrVal;
    private bool _atrReady;

    // Position
    private int _posDir;      // 0=flat, 1=long, -1=short
    private double _entryPrice;

    // Stats
    public int TotalTrades { get; private set; }
    public double TotalPnL { get; private set; }
    public int PositionDirection => _posDir;
    public double CurrentPnL { get; private set; }

    private readonly List<string> _log = new();
    public IReadOnlyList<string> Log => _log;

    public VStopPureStrategy(Config? config = null)
    {
        Params = config ?? new Config();
        _trBuf = new double[Params.Period + 1];
        _atr = new double[1]; // placeholder
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        _barCount++;
        double c = candle.Close, h = candle.High, l = candle.Low;

        // True Range
        double tr = _barCount == 1 ? h - l : Math.Max(h - l, Math.Max(Math.Abs(h - _prevClose), Math.Abs(l - _prevClose)));
        _prevClose = c;

        // ATR calculation
        if (!_atrReady)
        {
            if (_barCount <= Params.Period)
            {
                _trBuf[_barCount] = tr;
                if (_barCount == Params.Period)
                {
                    double sum = 0;
                    for (int i = 1; i <= Params.Period; i++) sum += _trBuf[i];
                    _atrVal = sum / Params.Period;
                    _atrReady = true;

                    // Init VStop
                    _trend = 1;
                    _maxPrice = h;
                    _minPrice = l;
                    _vstop = _maxPrice - Params.Mult * _atrVal;
                }
                return null;
            }
        }
        else
        {
            double k = 2.0 / (Params.Period + 1);
            _atrVal = tr * k + _atrVal * (1 - k);
        }

        if (!_atrReady) return null;

        // VStop update
        int prevTrend = _trend;
        double stopDist = Params.Mult * _atrVal;

        if (_trend == 1)
        {
            if (h > _maxPrice) _maxPrice = h;
            double vs = _maxPrice - stopDist;
            if (vs < _vstop) vs = _vstop;

            if (c < vs)
            {
                _trend = -1;
                _minPrice = l;
                _vstop = _minPrice + stopDist;
            }
            else
            {
                _vstop = vs;
            }
        }
        else
        {
            if (l < _minPrice) _minPrice = l;
            double vs = _minPrice + stopDist;
            if (vs > _vstop) vs = _vstop;

            if (c > vs)
            {
                _trend = 1;
                _maxPrice = h;
                _vstop = _maxPrice - stopDist;
            }
            else
            {
                _vstop = vs;
            }
        }

        // No flip = no action
        if (_trend == prevTrend)
        {
            // Update current PnL
            if (_posDir == 1) CurrentPnL = c - _entryPrice;
            else if (_posDir == -1) CurrentPnL = _entryPrice - c;
            return null;
        }

        // === FLIP ===
        if (Mode == StrategyMode.Stopped && _posDir != 0)
        {
            return CloseTrade(c, ticker, "СТОП ТОРГИ");
        }

        if (Mode != StrategyMode.Running) return null;

        // Close existing + open new
        Signal? closeSignal = null;
        if (_posDir != 0)
        {
            double pnl = _posDir == 1 ? c - _entryPrice - Params.Commission : _entryPrice - c - Params.Commission;
            TotalPnL += pnl;
            TotalTrades++;
            LogMsg($"[{ticker}] CLOSE {(_posDir == 1 ? "LONG" : "SHORT")} @ {c:F0} PnL={pnl:F1}");
        }

        // Open new direction
        int newDir = _trend == 1 ? 1 : -1;
        _posDir = newDir;
        _entryPrice = c;
        CurrentPnL = 0;

        string dir = newDir == 1 ? "LONG" : "SHORT";
        LogMsg($"[{ticker}] {dir} @ {c:F0} (VStop flip)");

        return new Signal
        {
            Timestamp = candle.Timestamp,
            Ticker = ticker,
            Direction = newDir == 1 ? SignalDirection.Buy : SignalDirection.Sell,
            Source = SignalSource.Strategy,
            Volume = (_posDir != 0 ? 2 : 1) * Params.BaseLots, // close + open = 2x if was in position
            StrategyName = Name,
            Price = c,
            Comment = $"VStop flip → {dir}"
        };
    }

    private double _prevClose;

    private Signal CloseTrade(double price, string ticker, string reason)
    {
        double pnl = _posDir == 1 ? price - _entryPrice - Params.Commission : _entryPrice - price - Params.Commission;
        TotalPnL += pnl;
        TotalTrades++;
        LogMsg($"[{ticker}] {reason}: PnL={pnl:F1}");

        var signal = new Signal
        {
            Timestamp = DateTime.UtcNow, Ticker = ticker,
            Direction = _posDir == 1 ? SignalDirection.Sell : SignalDirection.Buy,
            Source = SignalSource.Exit, Volume = Params.BaseLots,
            StrategyName = Name, Price = price, Comment = reason
        };

        _posDir = 0; CurrentPnL = 0;
        return signal;
    }

    public void Reset()
    {
        _barCount = 0; _atrReady = false; _atrVal = 0;
        _trend = 0; _vstop = 0; _maxPrice = 0; _minPrice = 0;
        _posDir = 0; _entryPrice = 0; _prevClose = 0;
        CurrentPnL = 0;
        _log.Clear();
    }

    public string GetStatus()
    {
        var dir = _posDir switch { 1 => "LONG", -1 => "SHORT", _ => "FLAT" };
        return $"{Name} [{Mode}] {dir} entry={_entryPrice:F0} pnl={CurrentPnL:F0} " +
               $"vstop={_vstop:F0} trend={_trend} | trades={TotalTrades} totalPnL={TotalPnL:F0}";
    }

    private void LogMsg(string msg)
    {
        _log.Add($"[{DateTime.UtcNow:HH:mm:ss}] {msg}");
        if (_log.Count > 2000) _log.RemoveAt(0);
    }
}
