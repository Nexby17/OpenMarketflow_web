using HedgeFund.Core.Models;
using HedgeFund.Core.Indicators;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Grid MM v8 — инвертированная логика V7.
/// 
/// V7: SAR > EMA → SHORT (mean-reversion)
/// V8: SAR > EMA → LONG (trend-following)
/// 
/// Остальное то же: STD выход + profit target + grid
/// </summary>
public class GridMmV8Strategy
{
    public class Config
    {
        public double SarStart { get; set; } = 0.009;
        public double SarStep { get; set; } = 0.01;
        public double SarMax { get; set; } = 0.2;
        public int EmaPeriod { get; set; } = 30;
        public double GridStep { get; set; } = 35.0;
        public double GridSpread { get; set; } = 35.0;
        public int MaxGridLevels { get; set; } = 70;
        public int MaxLots { get; set; } = 1;
        public int StdPeriod { get; set; } = 14;
        public double StdMult { get; set; } = 1.5;
        public bool GridHold { get; set; } = false;
        public double ClosePct { get; set; } = 0.15;
        public double MinProfitPerLot { get; set; } = 35.0;
        public double Commission { get; set; } = 0.90;
    }

    public enum Mode { Running, Paused, Stopped }
    public enum EventType { EntryMarket, PlaceNextGridLevel, GridLevelFilled, CloseAllMarket }
    
    public class Event
    {
        public EventType Type { get; set; }
        public int Direction { get; set; }
        public double Price { get; set; }
        public int Volume { get; set; }
        public int LevelIndex { get; set; }
        public double TpPrice { get; set; }
        public string Reason { get; set; } = "";
    }

    public Config Params { get; }
    public Mode CurrentMode { get; set; } = Mode.Paused;
    
    public int PositionDirection => _posDir;
    public double EntryPrice => _entryPrice;
    public int EntryLots => _entryLots;
    public int CurrentLevel => _currentLevel;
    public int FilledLevels => _filledLevels;
    public int TotalLots => EntryLots + FilledLevels;
    public int RoundTrips => _roundTrips;
    public double TotalPnL => _realizedPnL;
    public double CurrentSar { get { var v = _sar?.Value ?? 0; return double.IsNaN(v) ? 0 : v; } }
    public double CurrentEma { get { var v = _ema?.Value ?? 0; return double.IsNaN(v) ? 0 : v; } }
    public double CurrentStd { get { return double.IsNaN(_currentStd) ? 0 : _currentStd; } }
    public bool IsConnected { get; set; }
    
    private int _posDir = 0;
    private double _entryPrice = 0;
    private int _entryLots = 0;
    private int _currentLevel = 0;
    private int _filledLevels = 0;
    private int _roundTrips = 0;
    private double _realizedPnL = 0;
    private int _peakLots = 0;
    
    private ParabolicSAR? _sar;
    private EMA? _ema;
    private double _prevSar = 0;
    private double _prevEma = 0;
    private bool _warmedUp = false;
    
    private double _currentStd = double.NaN;
    private readonly List<double> _stdBuffer = new();
    private readonly List<Event> _pendingEvents = new();
    
    public GridMmV8Strategy(Config? config) { Params = config ?? new Config(); }
    
    public void OnCandle(Candle candle)
    {
        if (CurrentMode != Mode.Running) return;
        
        double sar = _sar?.Update(candle) ?? 0;
        double ema = _ema?.Update(candle) ?? 0;
        
        _stdBuffer.Add(candle.Close);
        if (_stdBuffer.Count > Params.StdPeriod) _stdBuffer.RemoveAt(0);
        _currentStd = _stdBuffer.Count >= Params.StdPeriod ? CalcStd() : double.NaN;
        
        if (double.IsNaN(sar) || double.IsNaN(ema)) return;
        _warmedUp = true;
        
        double ct = 5;
        bool prevSarAbove = _prevSar - _prevEma >= ct;
        bool prevSarBelow = _prevEma - _prevSar >= ct;
        bool nowSarAbove = sar - ema >= ct;
        bool nowSarBelow = ema - sar >= ct;
        
        // ИНВЕРТИРОВАННАЯ логика: SAR > EMA → LONG, SAR < EMA → SHORT
        bool longSignal = _warmedUp && prevSarBelow && nowSarAbove;   // SAR пересёк EMA вверх → LONG
        bool shortSignal = _warmedUp && prevSarAbove && nowSarBelow;  // SAR пересёк EMA вниз → SHORT
        
        if (longSignal || shortSignal)
            Console.WriteLine($"[V8-SIGNAL] {(longSignal ? "LONG" : "SHORT")} cross: prevSAR={_prevSar:F0} prevEMA={_prevEma:F0} → SAR={sar:F0} EMA={ema:F0} price={candle.Close:F0}");
        
        _prevSar = sar;
        _prevEma = ema;
        
        // === НЕТ ПОЗИЦИИ ===
        if (_posDir == 0)
        {
            if (longSignal) EmitEntry(1, candle.Close, "SAR↑EMA → LONG");
            else if (shortSignal) EmitEntry(-1, candle.Close, "SAR↓EMA → SHORT");
            return;
        }
        
        // === ЕСТЬ ПОЗИЦИЯ ===
        
        // 1. STD exit
        if (!double.IsNaN(_currentStd) && _currentStd > 0)
        {
            double deviation = Math.Abs(candle.Close - sar);
            double threshold = Params.StdMult * _currentStd;
            if (deviation > threshold)
            {
                double sessionPnL = CalcUnrealizedPnL(candle.Close);
                if (!Params.GridHold || sessionPnL >= 0)
                {
                    EmitCloseAll($"STD: |{candle.Close:F0}-{sar:F0}|={deviation:F0}>{threshold:F0} PnL={sessionPnL:F0}");
                    return;
                }
            }
        }
        
        // 2. Profit target
        if (_filledLevels > 0 && _roundTrips >= (int)(_peakLots * Params.ClosePct))
        {
            double unrealizedPnl = CalcUnrealizedPnL(candle.Close);
            double pnlPerLot = TotalLots > 0 ? unrealizedPnl / TotalLots : 0;
            if (pnlPerLot >= Params.MinProfitPerLot)
            {
                EmitCloseAll($"Profit target: {_roundTrips} RT, {pnlPerLot:F0} пт/лот");
                return;
            }
        }
    }
    
    public (double gridPrice, double tpPrice) GetNextLevelPrices()
    {
        if (_posDir == 0 || _currentLevel <= 0) return (0, 0);
        if (_currentLevel > Params.MaxGridLevels) return (0, 0);
        int level = _currentLevel;
        if (_posDir == 1) return (_entryPrice - level * Params.GridStep, _entryPrice - level * Params.GridStep + Params.GridSpread);
        else return (_entryPrice + level * Params.GridStep, _entryPrice + level * Params.GridStep - Params.GridSpread);
    }
    
    public List<(int level, double gridPrice, double tpPrice)> GetMissedLevels(double currentPrice)
    {
        var result = new List<(int, double, double)>();
        if (_posDir == 0) return result;
        if (_posDir == 1)
        {
            int missed = (int)((_entryPrice - currentPrice) / Params.GridStep);
            for (int i = 1; i <= missed && i <= Params.MaxGridLevels; i++)
                result.Add((i, _entryPrice - i * Params.GridStep, _entryPrice - i * Params.GridStep + Params.GridSpread));
        }
        else
        {
            int missed = (int)((currentPrice - _entryPrice) / Params.GridStep);
            for (int i = 1; i <= missed && i <= Params.MaxGridLevels; i++)
                result.Add((i, _entryPrice + i * Params.GridStep, _entryPrice + i * Params.GridStep - Params.GridSpread));
        }
        return result;
    }
    
    public void OnEntryFilled(int direction, double entryPrice, int lots)
    {
        _posDir = direction; _entryPrice = entryPrice; _entryLots = lots;
        _currentLevel = 1; _filledLevels = 0; _roundTrips = 0; _realizedPnL = 0; _peakLots = lots;
    }
    
    public void OnGridLevelFilled(int levelIndex, double fillPrice)
    {
        _filledLevels++; _peakLots = Math.Max(_peakLots, TotalLots); _currentLevel = levelIndex + 1;
        if (_currentLevel > Params.MaxGridLevels) _currentLevel = 0;
    }
    
    public void OnRoundTrip(double pnl) { _roundTrips++; _realizedPnL += pnl; }
    
    public void OnTpFilled() { if (_filledLevels > 0) _filledLevels--; }
    
    public void RestorePosition(int direction, double entryPrice, int totalLots, int filledLevels, int roundTrips, double realizedPnL)
    {
        _posDir = direction; _entryPrice = entryPrice; _entryLots = totalLots - filledLevels;
        _filledLevels = filledLevels; _roundTrips = roundTrips; _realizedPnL = realizedPnL;
        _peakLots = totalLots; _currentLevel = filledLevels + 1;
        if (_currentLevel > Params.MaxGridLevels) _currentLevel = 0;
    }
    
    public void ClearPosition()
    {
        _posDir = 0; _entryPrice = 0; _entryLots = 0; _currentLevel = 0;
        _filledLevels = 0; _roundTrips = 0; _realizedPnL = 0; _peakLots = 0;
    }
    
    public void InitIndicators() { _sar = new ParabolicSAR(Params.SarStart, Params.SarStep, Params.SarMax); _ema = new EMA(Params.EmaPeriod); _warmedUp = false; }
    
    public void ForceEntry(double currentPrice)
    {
        if (_posDir != 0) return;
        double diff = CurrentSar - CurrentEma;
        // Инвертировано: SAR > EMA → LONG
        int dir = diff >= -5 ? 1 : -1;
        EmitEntry(dir, currentPrice, $"Force V8: SAR={CurrentSar:F0} EMA={CurrentEma:F0} diff={diff:F0} → {(dir == 1 ? "LONG" : "SHORT")}");
    }
    
    public bool HasPendingEvents => _pendingEvents.Count > 0;
    public Event? GetNextEvent() { if (_pendingEvents.Count == 0) return null; var e = _pendingEvents[0]; _pendingEvents.RemoveAt(0); return e; }
    
    public string GetStatus()
    {
        string dir = _posDir == 1 ? "LONG" : _posDir == -1 ? "SHORT" : "FLAT";
        return $"V8 Mode={CurrentMode} | {dir} entry={_entryPrice:F0} lots={TotalLots} " +
               $"filled={_filledLevels} RT={_roundTrips} PnL={_realizedPnL:F0} " +
               $"next={_currentLevel} | SAR={CurrentSar:F0} EMA={CurrentEma:F0} STD={CurrentStd:F0}";
    }
    
    private double CalcStd() { double s=0,sq=0; foreach(var x in _stdBuffer){s+=x;sq+=x*x;} int n=_stdBuffer.Count; return Math.Sqrt(sq/n-Math.Pow(s/n,2)); }
    private double CalcUnrealizedPnL(double cp) { if(_posDir==0)return 0; double u=(cp-_entryPrice)*_posDir*_entryLots; u-=TotalLots*Params.Commission; return u+_realizedPnL; }
    
    private void EmitEntry(int dir, double price, string reason)
    {
        _pendingEvents.Add(new Event { Type=EventType.EntryMarket, Direction=dir, Price=price, Volume=Params.MaxLots, LevelIndex=0, Reason=reason });
    }
    
    private void EmitCloseAll(string reason)
    {
        _pendingEvents.Add(new Event { Type=EventType.CloseAllMarket, Direction=_posDir, Price=0, Volume=TotalLots, Reason=reason });
    }
}
