using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Fade Impulse — торговля против сильных импульсов с trailing SL.
///
/// Логика:
/// 1. Импульс: volume ≥ volMult × SMA(20) И |body| ≥ bodyMult × ATR(20)
/// 2. Направление импульса: body > 0 = UP, body < 0 = DOWN
/// 3. Ждём pullback: pullbackBars свечей после импульса
/// 4. Вход 1 лот ПРОТИВ импульса (UP → SHORT, DOWN → LONG)
/// 5. SL = slPts пунктов от entry
/// 6. Trailing SL 1:1 — цена пошла на N пт → SL двигается на N пт
/// 7. TP = tpPts (почти не достигается, всё решает trailing)
/// 8. Exit: SL hit (trailing или стартовый) ИЛИ TP hit ИЛИ maxHoldMinutes
///
/// Инструменты: MIX (лучший), RTS, SI
/// Таймфрейм: 5 мин
/// </summary>
public class FadeImpulseStrategy
{
    public class Config
    {
        public double VolMult { get; set; } = 1.5;      // Volume ≥ volMult × SMA(20)
        public double BodyMult { get; set; } = 0.5;      // |body| ≥ bodyMult × ATR(20)
        public int AtrPeriod { get; set; } = 20;          // ATR period
        public int VolPeriod { get; set; } = 20;          // Volume SMA period
        public int PullbackBars { get; set; } = 2;        // Wait N bars after impulse
        public double PullbackPct { get; set; } = 0.3;    // Or wait for 30% retrace
        public double SlPts { get; set; } = 30;           // Initial SL in points
        public double TpPts { get; set; } = 500;          // TP in points
        public int MaxHoldMinutes { get; set; } = 60;     // Force exit after N minutes
        public double Commission { get; set; } = 0.90;    // Commission per side
    }

    public enum Mode { Running, Paused, Stopped }

    public enum EventType { SignalImpulse, SignalEntry, ExitSL, ExitTP, ExitTimeout }

    public class Event
    {
        public EventType Type { get; set; }
        public int Direction { get; set; }   // 1=LONG, -1=SHORT
        public double Price { get; set; }
        public double? PnL { get; set; }
        public string? Reason { get; set; }
    }

    // State
    public Mode CurrentMode { get; set; } = Mode.Paused;
    public int PositionDir => _posDir;
    public double EntryPrice => _entryPrice;
    public double CurrentSL => _currentSL;
    public double CurrentTP => _currentTP;
    public DateTime? EntryTime => _entryTime;
    public int HoldMinutes => _entryTime.HasValue ? (int)(DateTime.UtcNow - _entryTime.Value).TotalMinutes : 0;
    public double RealizedPnL => _realizedPnL;
    public int TotalTrades => _totalTrades;
    public int Wins => _wins;

    // Indicators state
    public double CurrentATR => _atr;
    public double CurrentVolSMA => _volSma;
    public double ImpulseDirection => _impulseDir; // 1=up, -1=down, 0=none
    public int BarsSinceImpulse => _barsSinceImpulse;
    public double ImpulseHigh => _impulseHigh;
    public double ImpulseLow => _impulseLow;

    // Config
    public Config Params { get; }

    private int _posDir = 0;
    private double _entryPrice = 0;
    private double _currentSL = 0;
    private double _currentTP = 0;
    private DateTime? _entryTime = null;
    private double _realizedPnL = 0;
    private int _totalTrades = 0;
    private int _wins = 0;

    // Impulse detection state
    private int _impulseDir = 0;       // 1=up impulse, -1=down impulse
    private int _barsSinceImpulse = 0;
    private double _impulseHigh = 0;
    private double _impulseLow = 0;
    private double _impulseBody = 0;

    // Indicator buffers
    private double _atr = 0;
    private double _volSma = 0;
    private readonly List<double> _trHistory = new();
    private readonly List<double> _volHistory = new();

    public FadeImpulseStrategy(Config? config = null)
    {
        Params = config ?? new Config();
    }

    /// <summary>
    /// Process a new candle. Returns events (signals, fills, exits).
    /// </summary>
    public List<Event> OnCandle(Candle candle)
    {
        var events = new List<Event>();

        UpdateIndicators(candle);
        if (_atr <= 0 || _volSma <= 0) return events;

        // If in position — check exits first
        if (_posDir != 0)
        {
            var exitEvent = CheckExit(candle);
            if (exitEvent != null)
            {
                events.Add(exitEvent);
                return events; // Don't look for new signals while exiting
            }

            // Update trailing SL
            UpdateTrailingSL(candle.Close);
            return events;
        }

        // No position — look for impulse signals
        if (CurrentMode != Mode.Running) return events;

        // Check for new impulse
        double body = candle.Close - candle.Open;
        double absBodyPct = Math.Abs(body) / _atr;

        bool volSpike = candle.Volume >= Params.VolMult * _volSma;
        bool strongCandle = absBodyPct >= Params.BodyMult;

        // New impulse detected
        if (volSpike && strongCandle && _impulseDir == 0)
        {
            _impulseDir = body > 0 ? 1 : -1; // UP impulse or DOWN impulse
            _barsSinceImpulse = 0;
            _impulseHigh = candle.High;
            _impulseLow = candle.Low;
            _impulseBody = body;

            events.Add(new Event
            {
                Type = EventType.SignalImpulse,
                Direction = _impulseDir,
                Price = candle.Close,
                Reason = $"{(_impulseDir == 1 ? "UP" : "DOWN")} impulse: vol={candle.Volume:F0} (>{Params.VolMult:F1}×{_volSma:F0}), body={absBodyPct:F2}×ATR"
            });
        }

        // Tracking pullback after impulse
        if (_impulseDir != 0)
        {
            _barsSinceImpulse++;
            _impulseHigh = Math.Max(_impulseHigh, candle.High);
            _impulseLow = Math.Min(_impulseLow, candle.Low);

            // Calculate retrace
            double retracePct = 0;
            double range = _impulseHigh - _impulseLow;
            if (range > 0)
            {
                if (_impulseDir == 1) // UP impulse — retrace is price dropping from high
                    retracePct = (_impulseHigh - candle.Close) / range;
                else // DOWN impulse — retrace is price rising from low
                    retracePct = (candle.Close - _impulseLow) / range;
            }

            // Pullback confirmed
            if (_barsSinceImpulse >= Params.PullbackBars || retracePct >= Params.PullbackPct)
            {
                // Entry AGAINST impulse
                int entryDir = -_impulseDir;
                _posDir = entryDir;
                _entryPrice = candle.Close;
                _entryTime = DateTime.UtcNow;

                // Set initial SL and TP
                if (entryDir == 1) // LONG
                {
                    _currentSL = _entryPrice - Params.SlPts;
                    _currentTP = _entryPrice + Params.TpPts;
                }
                else // SHORT
                {
                    _currentSL = _entryPrice + Params.SlPts;
                    _currentTP = _entryPrice - Params.TpPts;
                }

                events.Add(new Event
                {
                    Type = EventType.SignalEntry,
                    Direction = entryDir,
                    Price = candle.Close,
                    Reason = $"Fade {(_impulseDir == 1 ? "UP" : "DOWN")} impulse after {_barsSinceImpulse} bars ({retracePct:P0} retrace)"
                });

                // Reset impulse state
                _impulseDir = 0;
                _barsSinceImpulse = 0;
            }

            // Impulse expired (too many bars without pullback)
            if (_barsSinceImpulse > Params.PullbackBars * 3)
            {
                _impulseDir = 0;
                _barsSinceImpulse = 0;
            }
        }

        return events;
    }

    private Event? CheckExit(Candle candle)
    {
        // SL hit
        if (_posDir == 1 && candle.Low <= _currentSL)
        {
            return ClosePosition(_currentSL, EventType.ExitSL, "SL hit (trailing)");
        }
        if (_posDir == -1 && candle.High >= _currentSL)
        {
            return ClosePosition(_currentSL, EventType.ExitSL, "SL hit (trailing)");
        }

        // TP hit
        if (_posDir == 1 && candle.High >= _currentTP)
        {
            return ClosePosition(_currentTP, EventType.ExitTP, "TP hit");
        }
        if (_posDir == -1 && candle.Low <= _currentTP)
        {
            return ClosePosition(_currentTP, EventType.ExitTP, "TP hit");
        }

        // Timeout
        if (HoldMinutes >= Params.MaxHoldMinutes)
        {
            return ClosePosition(candle.Close, EventType.ExitTimeout, $"Timeout {Params.MaxHoldMinutes}min");
        }

        return null;
    }

    private Event ClosePosition(double exitPrice, EventType type, string reason)
    {
        double pnl = (exitPrice - _entryPrice) * _posDir - Params.Commission * 2;
        _realizedPnL += pnl;
        _totalTrades++;
        if (pnl > 0) _wins++;

        var evt = new Event
        {
            Type = type,
            Direction = _posDir,
            Price = exitPrice,
            PnL = pnl,
            Reason = reason
        };

        _posDir = 0;
        _entryPrice = 0;
        _currentSL = 0;
        _currentTP = 0;
        _entryTime = null;

        return evt;
    }

    /// <summary>
    /// Trailing SL 1:1 — price moves N pts → SL moves N pts.
    /// </summary>
    private void UpdateTrailingSL(double currentPrice)
    {
        if (_posDir == 0) return;

        if (_posDir == 1) // LONG
        {
            double newSL = currentPrice - Params.SlPts;
            if (newSL > _currentSL)
                _currentSL = newSL;
        }
        else // SHORT
        {
            double newSL = currentPrice + Params.SlPts;
            if (newSL < _currentSL)
                _currentSL = newSL;
        }
    }

    private void UpdateIndicators(Candle candle)
    {
        // True Range
        double tr = candle.High - candle.Low;

        // ATR
        _trHistory.Add(tr);
        if (_trHistory.Count > Params.AtrPeriod)
            _trHistory.RemoveAt(0);
        _atr = _trHistory.Count >= Params.AtrPeriod ? _trHistory.Average() : 0;

        // Volume SMA
        _volHistory.Add((double)candle.Volume);
        if (_volHistory.Count > Params.VolPeriod)
            _volHistory.RemoveAt(0);
        _volSma = _volHistory.Count >= Params.VolPeriod ? _volHistory.Average() : 0;
    }

    /// <summary>
    /// Force close position (used by launcher on stop/exit).
    /// </summary>
    public void ClearPosition()
    {
        _posDir = 0;
        _entryPrice = 0;
        _currentSL = 0;
        _currentTP = 0;
        _entryTime = null;
        _impulseDir = 0;
        _barsSinceImpulse = 0;
    }

    /// <summary>
    /// Status for API.
    /// </summary>
    public Dictionary<string, object> GetStatus()
    {
        return new Dictionary<string, object>
        {
            ["mode"] = CurrentMode.ToString(),
            ["position"] = _posDir != 0 ? (_posDir == 1 ? "LONG" : "SHORT") : "FLAT",
            ["entryPrice"] = _entryPrice,
            ["sl"] = _currentSL,
            ["tp"] = _currentTP,
            ["trailingSL"] = _currentSL,
            ["holdMinutes"] = HoldMinutes,
            ["pnl"] = _realizedPnL,
            ["trades"] = _totalTrades,
            ["wins"] = _wins,
            ["winRate"] = _totalTrades > 0 ? (double)_wins / _totalTrades * 100 : 0,
            ["atr"] = _atr,
            ["volSma"] = _volSma,
            ["impulseDir"] = _impulseDir != 0 ? (_impulseDir == 1 ? "UP" : "DOWN") : "none",
            ["barsSinceImpulse"] = _barsSinceImpulse,
        };
    }
}
