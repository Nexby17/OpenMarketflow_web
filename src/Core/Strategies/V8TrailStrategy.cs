using System.Collections.Generic;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// V8 Trail Strategy — Inverted PSAR×EMA с trailing SL в % от entry.
/// PSAR flip UP + close > EMA → SHORT (fade)
/// PSAR flip DOWN + close < EMA → LONG (fade)
/// Exit: trailing SL (high - entry*slPct) для LONG, (low + entry*slPct) для SHORT
/// </summary>
public class V8TrailStrategy
{
    public class V8Params
    {
        public double SarStart { get; set; } = 0.02;
        public double SarStep { get; set; } = 0.02;
        public double SarMax { get; set; } = 0.2;
        public int EmaPeriod { get; set; } = 20;
        public double SlPct { get; set; } = 0.10; // trailing SL в % (0.10 = 0.10%)
        public double Commission { get; set; } = 0.90;
        public int MaxHoldMinutes { get; set; } = 240; // 4 часа
    }

    public V8Params Params { get; set; } = new();

    // PSAR state
    private double _sar = 0;
    private double _af = 0;
    private double _ep = 0;
    private int _sarTrend = 1; // 1=up, -1=down
    private int _prevTrend = 1;

    // EMA state
    private double _ema = 0;
    private bool _emaReady = false;
    private int _barCount = 0;

    // Position state
    private int _posDir = 0; // 0=none, 1=long, -1=short
    private double _entryPrice = 0;
    private double _highest = 0;
    private double _lowest = 0;
    private DateTime? _entryTime = null;

    // Stats
    private int _totalTrades = 0;
    private int _wins = 0;
    private double _realizedPnL = 0;

    // Public accessors
    public int PositionDirection => _posDir;
    public double EntryPrice => _entryPrice;
    public double CurrentSar => _sar;
    public double CurrentEma => _ema;
    public int SarTrend => _sarTrend;
    public int TotalTrades => _totalTrades;
    public int Wins => _wins;
    public double RealizedPnL => _realizedPnL;
    public int HoldMinutes => _entryTime.HasValue ? (int)(DateTime.UtcNow - _entryTime.Value).TotalMinutes : 0;
    public double TrailingSL => _posDir != 0 ? _entryPrice * Params.SlPct / 100.0 : 0;
    public double CurrentSLPrice => _posDir == 1 ? _highest - TrailingSL : _posDir == -1 ? _lowest + TrailingSL : 0;

    public void Init(double firstPrice)
    {
        _sar = firstPrice;
        _af = Params.SarStart;
        _ep = firstPrice;
        _ema = firstPrice;
        _prevTrend = 1;
    }

    /// <summary>
    /// Feed a bar. Returns signal: 0=none, 1=long, -1=short.
    /// </summary>
    public int OnBar(double close, double high, double low)
    {
        _barCount++;
        int signal = 0;

        // Update PSAR
        _prevTrend = _sarTrend;
        UpdatePSAR(high, low);

        // Update EMA
        if (!_emaReady)
        {
            _ema = close;
            if (_barCount >= 2) _emaReady = true;
        }
        else
        {
            double k = 2.0 / (Params.EmaPeriod + 1);
            _ema = close * k + _ema * (1 - k);
        }

        if (!_emaReady) return 0;

        // Detect signal: V8 inverted logic
        // PSAR flip UP + close > EMA → SHORT (fade the trend)
        // PSAR flip DOWN + close < EMA → LONG (fade the trend)
        if (_sarTrend != _prevTrend)
        {
            if (_sarTrend == 1 && close > _ema)
                signal = -1; // SHORT
            else if (_sarTrend == -1 && close < _ema)
                signal = 1;  // LONG
        }

        return signal;
    }

    /// <summary>
    /// Check if trailing SL hit. Returns (shouldClose, reason).
    /// </summary>
    public void UpdateTrailing(double high, double low)
    {
        if (_posDir == 0) return;
        if (_posDir == 1) _highest = Math.Max(_highest, high);
        else _lowest = Math.Min(_lowest, low);
    }

    public (bool shouldClose, string reason) CheckSL(double high, double low)
    {
        if (_posDir == 0) return (false, "");

        // Timeout
        if (_entryTime.HasValue && HoldMinutes >= Params.MaxHoldMinutes)
            return (true, $"Timeout ({HoldMinutes} min)");

        double sl = _entryPrice * Params.SlPct / 100.0;

        if (_posDir == 1)
        {
            _highest = Math.Max(_highest, high);
            if (low <= _highest - sl)
                return (true, $"SL LONG: low={low:F0} <= {_highest - sl:F0} (trail={_highest:F0}-{sl:F0})");
        }
        else
        {
            _lowest = Math.Min(_lowest, low);
            if (high >= _lowest + sl)
                return (true, $"SL SHORT: high={high:F0} >= {_lowest + sl:F0} (trail={_lowest:F0}+{sl:F0})");
        }

        return (false, "");
    }

    public void OpenPosition(int dir, double price)
    {
        _posDir = dir;
        _entryPrice = price;
        _highest = price;
        _lowest = price;
        _entryTime = DateTime.UtcNow;
    }

    public void ClosePosition(double exitPrice, double stepPrice = 1.0)
    {
        double pnl;
        if (_posDir == 1)
            pnl = (exitPrice - _entryPrice) * stepPrice - Params.Commission * 2;
        else
            pnl = (_entryPrice - exitPrice) * stepPrice - Params.Commission * 2;

        _realizedPnL += pnl;
        _totalTrades++;
        if (pnl > 0) _wins++;

        _posDir = 0;
        _entryPrice = 0;
        _highest = 0;
        _lowest = 0;
        _entryTime = null;
    }

    public void ClearPosition()
    {
        _posDir = 0;
        _entryPrice = 0;
        _highest = 0;
        _lowest = 0;
        _entryTime = null;
    }

    public void RestorePosition(int dir, double entryPrice)
    {
        _posDir = dir;
        _entryPrice = entryPrice;
        _highest = entryPrice;
        _lowest = entryPrice;
        _entryTime = DateTime.UtcNow;
    }

    private void UpdatePSAR(double high, double low)
    {
        if (_sarTrend == 1)
        {
            _sar = _sar + _af * (_ep - _sar);
            _sar = Math.Min(_sar, Math.Min(high, high)); // simplified

            if (low < _sar)
            {
                _sarTrend = -1;
                _sar = _ep;
                _af = Params.SarStart;
                _ep = low;
            }
            else
            {
                if (high > _ep)
                {
                    _ep = high;
                    _af = Math.Min(_af + Params.SarStep, Params.SarMax);
                }
            }
        }
        else
        {
            _sar = _sar + _af * (_ep - _sar);
            _sar = Math.Max(_sar, Math.Max(low, low)); // simplified

            if (high > _sar)
            {
                _sarTrend = 1;
                _sar = _ep;
                _af = Params.SarStart;
                _ep = high;
            }
            else
            {
                if (low < _ep)
                {
                    _ep = low;
                    _af = Math.Min(_af + Params.SarStep, Params.SarMax);
                }
            }
        }
    }

    public Dictionary<string, object> GetStatus()
    {
        return new Dictionary<string, object>
        {
            ["mode"] = _posDir != 0 ? "Running" : "Waiting",
            ["posDir"] = _posDir,
            ["entryPrice"] = _entryPrice,
            ["sar"] = Math.Round(_sar, 2),
            ["ema"] = Math.Round(_ema, 2),
            ["sarTrend"] = _sarTrend,
            ["trailingSL"] = Math.Round(TrailingSL, 2),
            ["slPrice"] = Math.Round(CurrentSLPrice, 2),
            ["highest"] = Math.Round(_highest, 2),
            ["lowest"] = Math.Round(_lowest, 2),
            ["totalTrades"] = _totalTrades,
            ["wins"] = _wins,
            ["realizedPnL"] = Math.Round(_realizedPnL, 1),
            ["holdMinutes"] = HoldMinutes,
            ["params"] = new Dictionary<string, object>
            {
                ["sarStart"] = Params.SarStart,
                ["sarStep"] = Params.SarStep,
                ["sarMax"] = Params.SarMax,
                ["emaPeriod"] = Params.EmaPeriod,
                ["slPct"] = Params.SlPct,
                ["maxHoldMinutes"] = Params.MaxHoldMinutes
            }
        };
    }
}
