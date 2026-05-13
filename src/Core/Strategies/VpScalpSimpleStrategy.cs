using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// VP Scalp Simple — Volume Profile mean-reversion с trailing SL и POC exit.
/// Без grid, без TP. 1 лот.
///
/// Логика:
/// 1. VP(40 баров, 30 бинов) → POC, VAL, VAH
/// 2. Цена < VAL → LONG, цена > VAH → SHORT
/// 3. Trailing SL = sl_pct% от текущей цены (динамический)
/// 4. Exit: trailing SL hit ИЛИ POC hit ИЛИ timeout (60 мин)
///
/// Инструменты: MIX (лучший), RTS
/// Таймфрейм: 5 мин
/// </summary>
public class VpScalpSimpleStrategy
{
    public class Config
    {
        public int VpLookback { get; set; } = 40;
        public int VpBins { get; set; } = 30;
        public double VaPercent { get; set; } = 0.70;
        public double SlPct { get; set; } = 0.20;        // SL = 0.20% от текущей цены
        public int MaxHoldMinutes { get; set; } = 60;
        public double Commission { get; set; } = 0.90;
    }

    public enum Mode { Running, Paused, Stopped }

    // VP state
    public double POC { get; private set; }
    public double VAH { get; private set; }
    public double VAL { get; private set; }

    // Position state
    public int PositionDir => _posDir;
    public double EntryPrice => _entryPrice;
    public double CurrentSL => _currentSL;
    public DateTime? EntryTime => _entryTime;
    public int HoldMinutes => _entryTime.HasValue ? (int)(DateTime.UtcNow - _entryTime.Value).TotalMinutes : 0;
    public double RealizedPnL => _realizedPnL;
    public int TotalTrades => _totalTrades;
    public int Wins => _wins;
    public Mode CurrentMode { get; set; } = Mode.Paused;

    private int _posDir = 0;
    private double _entryPrice = 0;
    private double _currentSL = 0;
    private DateTime? _entryTime = null;
    private double _realizedPnL = 0;
    private int _totalTrades = 0;
    private int _wins = 0;

    public Config Params { get; }

    public VpScalpSimpleStrategy(Config? config = null)
    {
        Params = config ?? new Config();
    }

    /// <summary>
    /// Update VP from price/volume arrays. Called before each signal check.
    /// </summary>
    public void UpdateVP(double[] closes, double[] volumes)
    {
        if (closes.Length < 2) return;

        double lo = closes.Min(), hi = closes.Max();
        if (lo == hi) { POC = lo; VAL = lo; VAH = hi; return; }

        int bins = Params.VpBins;
        double[] edges = new double[bins + 1];
        double step = (hi - lo) / bins;
        for (int i = 0; i <= bins; i++) edges[i] = lo + i * step;

        double[] volPerBin = new double[bins];
        for (int i = 0; i < closes.Length; i++)
        {
            double p = closes[i];
            double v = volumes[i];
            int idx = Math.Min((int)((p - lo) / (hi - lo) * bins), bins - 1);
            if (idx < 0) idx = 0;
            volPerBin[idx] += v;
        }

        // POC
        int pocIdx = 0;
        for (int i = 1; i < bins; i++)
            if (volPerBin[i] > volPerBin[pocIdx]) pocIdx = i;
        POC = (edges[pocIdx] + edges[pocIdx + 1]) / 2;

        // Value Area (70%)
        double totalVol = volPerBin.Sum();
        var sorted = volPerBin.Select((v, i) => (v, i)).OrderByDescending(x => x.v).ToArray();
        double cumVol = 0;
        var vaIndices = new HashSet<int>();
        foreach (var (v, i) in sorted)
        {
            cumVol += v;
            vaIndices.Add(i);
            if (cumVol >= Params.VaPercent * totalVol) break;
        }
        var sortedVa = vaIndices.OrderBy(x => x).ToArray();
        VAL = edges[sortedVa[0]];
        VAH = edges[sortedVa[^1] + 1];
    }

    /// <summary>
    /// Check if there's an entry signal. Returns direction: 0=none, 1=LONG, -1=SHORT.
    /// </summary>
    public int CheckSignal(double closePrice)
    {
        if (CurrentMode != Mode.Running) return 0;
        if (_posDir != 0) return 0; // already in position
        if (VAL == 0 || VAH == 0) return 0;

        if (closePrice < VAL) return 1;  // LONG
        if (closePrice > VAH) return -1; // SHORT
        return 0;
    }

    /// <summary>
    /// Open position.
    /// </summary>
    public void OnEntry(int direction, double entryPrice)
    {
        _posDir = direction;
        _entryPrice = entryPrice;
        _entryTime = DateTime.UtcNow;

        // Initial SL = sl_pct% from entry
        double slDist = entryPrice * Params.SlPct / 100.0;
        _currentSL = direction == 1 ? entryPrice - slDist : entryPrice + slDist;
    }

    /// <summary>
    /// Update trailing SL. Returns new SL value.
    /// </summary>
    public double UpdateTrailingSL(double currentPrice)
    {
        if (_posDir == 0) return _currentSL;

        double slDist = currentPrice * Params.SlPct / 100.0;
        if (_posDir == 1)
        {
            double newSL = currentPrice - slDist;
            if (newSL > _currentSL) _currentSL = newSL;
        }
        else
        {
            double newSL = currentPrice + slDist;
            if (newSL < _currentSL) _currentSL = newSL;
        }
        return _currentSL;
    }

    /// <summary>
    /// Check exit conditions. Returns (shouldExit, reason) or (false, null).
    /// </summary>
    public (bool shouldExit, string? reason, double exitPrice) CheckExit(double high, double low, double close)
    {
        if (_posDir == 0) return (false, null, 0);

        // Trailing SL hit
        if (_posDir == 1 && low <= _currentSL)
            return (true, "SL", _currentSL);
        if (_posDir == -1 && high >= _currentSL)
            return (true, "SL", _currentSL);

        // POC hit
        if (_posDir == 1 && close >= POC)
            return (true, "POC", close);
        if (_posDir == -1 && close <= POC)
            return (true, "POC", close);

        // Timeout
        if (HoldMinutes >= Params.MaxHoldMinutes)
            return (true, $"Timeout {Params.MaxHoldMinutes}min", close);

        return (false, null, 0);
    }

    /// <summary>
    /// Close position with PnL calculation.
    /// </summary>
    public double OnExit(double exitPrice, string reason)
    {
        double pnl = (exitPrice - _entryPrice) * _posDir - Params.Commission * 2;
        _realizedPnL += pnl;
        _totalTrades++;
        if (pnl > 0) _wins++;

        _posDir = 0;
        _entryPrice = 0;
        _currentSL = 0;
        _entryTime = null;

        return pnl;
    }

    public void ClearPosition()
    {
        _posDir = 0;
        _entryPrice = 0;
        _currentSL = 0;
        _entryTime = null;
    }

    public Dictionary<string, object> GetStatus()
    {
        return new Dictionary<string, object>
        {
            ["mode"] = CurrentMode.ToString(),
            ["position"] = _posDir != 0 ? (_posDir == 1 ? "LONG" : "SHORT") : "FLAT",
            ["entryPrice"] = _entryPrice,
            ["sl"] = _currentSL,
            ["slPct"] = Params.SlPct,
            ["poc"] = POC,
            ["val"] = VAL,
            ["vah"] = VAH,
            ["holdMinutes"] = HoldMinutes,
            ["maxHold"] = Params.MaxHoldMinutes,
            ["pnl"] = _realizedPnL,
            ["trades"] = _totalTrades,
            ["wins"] = _wins,
            ["winRate"] = _totalTrades > 0 ? (double)_wins / _totalTrades * 100 : 0,
        };
    }
}
