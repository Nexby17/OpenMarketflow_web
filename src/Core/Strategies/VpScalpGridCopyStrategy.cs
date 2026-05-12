using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// VP Scalp Grid — Volume Profile mean-reversion с многоуровневым TP grid.
/// 
/// Логика:
/// 1. Цена < VAL → LONG, цена > VAH → SHORT
/// 2. TP Grid: N лимиток по направлению к POC (LONG: SELL выше entry)
/// 3. Exit: POC hit ИЛИ timeout ИЛИ session end (19:00 МСК)
/// 4. RV adaptation: step/spread масштабируются по RV rank
/// </summary>
public class VpScalpGridCopyStrategy
{
    public class Config
    {
        public int MaxLevels { get; set; } = 100;
        public int StepBase { get; set; } = 15;
        public int SpreadBase { get; set; } = 50;
        public int MaxHoldMinutes { get; set; } = 60;
        public int VpLookback { get; set; } = 60;
        public int VpBinSize { get; set; } = 50;
        public double VaPercent { get; set; } = 0.70;
        public bool RvAdaptation { get; set; } = true;
        public double Commission { get; set; } = 0.90;
        public double MinProfitPerLot { get; set; } = 28;
    }

    public enum Mode { Running, Paused, Stopped }

    // Current VP indicators
    public double POC { get; private set; }
    public double VAH { get; private set; }
    public double VAL { get; private set; }

    // Position state
    public int PositionDirection => _posDir;
    public double EntryPrice => _entryPrice;
    public int FilledLevels => _filledLevels;
    public int TotalLots => 1 + _filledLevels;
    public int RoundTrips => _roundTrips;
    public double RealizedPnL => _realizedPnL;
    public DateTime? EntryTime => _entryTime;
    public int HoldMinutes => _entryTime.HasValue ? (int)(DateTime.UtcNow - _entryTime.Value).TotalMinutes : 0;
    public Mode CurrentMode { get; set; } = Mode.Paused;

    private int _posDir = 0;
    private double _entryPrice = 0;
    private int _filledLevels = 0;
    private int _roundTrips = 0;
    private double _realizedPnL = 0;
    private DateTime? _entryTime = null;

    // VP calculation buffers
    private readonly List<(double price, double volume)> _priceBuffer = new();

    // RV calculation
    private readonly List<double> _retBuffer = new();
    private double _prevClose = 0;
    private double _rvRank = 0.5;
    private double _smoothedRvRank = 0.5;
    // Start level from StepBase param (e.g. stepBase=35 → level=4)
    private int _prevRvLevel = -1; // -1 = not initialized, will be set from StepBase on first call

    public Config Params { get; }

    public VpScalpGridCopyStrategy(Config? config = null)
    {
        Params = config ?? new Config();
    }

    /// <summary>
    /// Feed a bar for VP calculation and RV rank. Returns signal: 0=none, 1=LONG, -1=SHORT
    /// </summary>
    public int OnBar(double close, double volume)
    {
        if (CurrentMode != Mode.Running) return 0;

        // Update price buffer for VP
        _priceBuffer.Add((close, volume));
        if (_priceBuffer.Count > Params.VpLookback)
            _priceBuffer.RemoveAt(0);

        // Update RV
        if (_prevClose > 0)
        {
            double ret = (close - _prevClose) / _prevClose;
            _retBuffer.Add(ret);
            if (_retBuffer.Count > 5000) _retBuffer.RemoveAt(0);
            if (_retBuffer.Count >= 100)
            {
                // Simple rank: current RV percentile in window
                var rvWindow = _retBuffer.Skip(Math.Max(0, _retBuffer.Count - 5000)).ToList();
                var vols = new List<double>();
                for (int i = 0; i < rvWindow.Count; i++)
                {
                    var w = rvWindow.Skip(Math.Max(0, i - 60)).Take(60).ToList();
                    if (w.Count >= 10)
                    {
                        double avg = w.Average();
                        double ss = w.Sum(x => x * x);
                        vols.Add(Math.Sqrt(ss / w.Count - avg * avg) * Math.Sqrt(252 * 1440));
                    }
                }
                if (vols.Count > 10)
                {
                    double currentRv = vols.Last();
                    // Rolling percentile: last 1000 RV values for rank (3.5 days on 5-min)
                    var recentVols = vols.Skip(Math.Max(0, vols.Count - 1000)).ToList();
                    double rawRank = recentVols.Count(v => v <= currentRv) / (double)recentVols.Count;
                    
                    // EMA smoothing: α=0.05 (slow adaptation)
                    _smoothedRvRank = 0.95 * _smoothedRvRank + 0.05 * rawRank;
                    _rvRank = Math.Clamp(_smoothedRvRank, 0, 1);
                }
            }
        }
        _prevClose = close;

        // Recalculate VP
        if (_priceBuffer.Count >= 20)
            CalculateVP();

        // Check signal only if no position
        if (_posDir != 0) return 0;
        if (double.IsNaN(VAL) || double.IsNaN(VAH) || double.IsNaN(POC)) return 0;

        if (close < VAL) return 1;   // LONG
        if (close > VAH) return -1;  // SHORT

        return 0;
    }

    /// <summary>
    /// Check exit conditions. Returns true if should close.
    /// </summary>
    public (bool shouldClose, string reason) CheckExit(double currentPrice, int currentHourUtc)
    {
        if (_posDir == 0) return (false, "");

        // Timeout
        if (_entryTime.HasValue && HoldMinutes >= Params.MaxHoldMinutes)
            return (true, $"Timeout ({HoldMinutes} min >= {Params.MaxHoldMinutes})");

        // POC hit
        if (_posDir == 1 && currentPrice >= POC)
            return (true, $"POC hit LONG: {currentPrice:F0} >= {POC:F0}");
        if (_posDir == -1 && currentPrice <= POC)
            return (true, $"POC hit SHORT: {currentPrice:F0} <= {POC:F0}");

        return (false, "");
    }

    /// <summary>
    /// Get adapted step and spread based on RV rank.
    /// RV adaptation: 13 levels from 15/15 to 75/75, step 5.
    /// k = rvRank → level = floor(k × 13) → step = 15 + level × 5
    /// </summary>
    public (int step, int spread) GetAdaptedParams()
    {
        if (!Params.RvAdaptation)
            return (Params.StepBase, Params.SpreadBase);

        // Initialize level from StepBase on first call
        if (_prevRvLevel < 0)
            _prevRvLevel = Math.Clamp((Params.StepBase - 15) / 5, 0, 12);

        int rawLevel = (int)Math.Floor(_rvRank * 13);
        if (rawLevel > 12) rawLevel = 12;
        if (rawLevel < 0) rawLevel = 0;
        
        // Hysteresis: only change level if diff >= 2
        // Clamp: max change 2 levels per tick
        int diff = rawLevel - _prevRvLevel;
        int level = _prevRvLevel;
        if (Math.Abs(diff) >= 2)
            level = _prevRvLevel + Math.Sign(diff) * Math.Min(Math.Abs(diff), 2);
        
        _prevRvLevel = level;
        int step = 15 + level * 5;
        return (step, step);
    }

    /// <summary>Current RV rank (0..1) for display</summary>
    public double RvRank => _rvRank;

    /// <summary>Current RV-adapted level (0..12) for display</summary>
    public int RvLevel => (int)Math.Floor(Math.Clamp(_rvRank, 0, 1) * 13);

    /// <summary>
    /// Get all grid level prices for current position
    /// </summary>
    public List<(int level, double price)> GetGridLevels()
    {
        var result = new List<(int, double)>();
        if (_posDir == 0) return result;

        var (step, _) = GetAdaptedParams();

        for (int j = 1; j <= Params.MaxLevels; j++)
        {
            double price = _posDir == 1
                ? _entryPrice + step * j   // LONG: grid ABOVE entry (SELL limits)
                : _entryPrice - step * j;  // SHORT: grid BELOW entry (BUY limits)
            result.Add((j, price));
        }
        return result;
    }

    /// <summary>
    /// Calculate unrealized PnL at given price
    /// </summary>
    public double CalcUnrealizedPnL(double currentPrice)
    {
        if (_posDir == 0) return 0;
        double main = (currentPrice - _entryPrice) * _posDir * TotalLots;
        return main + _realizedPnL - TotalLots * Params.Commission;
    }

    // State management
    public void OnEntry(int direction, double price)
    {
        _posDir = direction;
        _entryPrice = price;
        _filledLevels = 0;
        _realizedPnL = 0;
        _entryTime = DateTime.UtcNow;
    }

    public void OnGridFill(int level, double fillPrice)
    {
        _filledLevels++;
    }

    public void OnGridTpDone(double pnl)
    {
        _roundTrips++;
        _realizedPnL += pnl;
        if (_filledLevels > 0) _filledLevels--;
    }

    public void ClearPosition()
    {
        _posDir = 0;
        _entryPrice = 0;
        _filledLevels = 0;
        _roundTrips = 0;
        _realizedPnL = 0;
        _entryTime = null;
    }

    public void RestorePosition(int direction, double entryPrice, int filledLevels, int roundTrips, double realizedPnL, DateTime? entryTime)
    {
        _posDir = direction;
        _entryPrice = entryPrice;
        _filledLevels = filledLevels;
        _roundTrips = roundTrips;
        _realizedPnL = realizedPnL;
        _entryTime = entryTime;
    }

    private void CalculateVP()
    {
        if (_priceBuffer.Count < 20) return;

        var prices = _priceBuffer.Select(p => p.price).ToList();
        var vols = _priceBuffer.Select(p => p.volume).ToList();

        double minP = prices.Min();
        double maxP = prices.Max();
        if (maxP - minP < Params.VpBinSize) return;

        int minBin = (int)(minP / Params.VpBinSize) * Params.VpBinSize;
        int maxBin = (int)(maxP / Params.VpBinSize) * Params.VpBinSize + Params.VpBinSize;

        var bins = new Dictionary<int, double>();
        for (int b = minBin; b < maxBin; b += Params.VpBinSize)
            bins[b] = 0;

        for (int i = 0; i < prices.Count; i++)
        {
            int b = (int)(prices[i] / Params.VpBinSize) * Params.VpBinSize;
            if (bins.ContainsKey(b))
                bins[b] += vols[i];
        }

        if (bins.Values.Sum() == 0) return;

        // POC = bin with max volume
        int pocBin = bins.Aggregate((a, b) => a.Value > b.Value ? a : b).Key;
        POC = pocBin + Params.VpBinSize / 2.0;

        // Value Area
        double totalVol = bins.Values.Sum();
        double targetVol = totalVol * Params.VaPercent;
        var sorted = bins.OrderByDescending(b => b.Value).ToList();
        double cumVol = 0;
        var vaBins = new List<int>();
        foreach (var b in sorted)
        {
            vaBins.Add(b.Key);
            cumVol += b.Value;
            if (cumVol >= targetVol) break;
        }

        VAH = vaBins.Max() + Params.VpBinSize;
        VAL = vaBins.Min();
    }
}
