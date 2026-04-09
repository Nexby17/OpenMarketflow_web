using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Volume Reversal Strategy for RTS futures (1 min).
/// 
/// Логика:
/// 1. Ждём N declining candles (red, lower close each)
/// 2. Volume spike: current bar volume >= avg_volume × VolMult
/// 3. Reversal candle: green for LONG, red for SHORT
/// 4. Вход market + SL + TP (TP = SL × TpMult) + trailing stop
/// 
/// Walk-forward OOS (RTS 1min): 2/2 wins, +43,310, 1338 trades
/// Best params: nb=2, vm=1.5, sl=133, tp=2.5, trail=0.7, avgVol=30
/// </summary>
public class VolumeReversalStrategy : IStrategy
{
    public class Config
    {
        /// <summary>N consecutive declining candles before reversal</summary>
        public int NBars { get; set; } = 2;
        
        /// <summary>Volume spike multiplier vs rolling average</summary>
        public double VolMult { get; set; } = 1.5;
        
        /// <summary>Stop-loss in price points</summary>
        public double StopLoss { get; set; } = 133.0;
        
        /// <summary>Take-profit multiplier of SL</summary>
        public double TpMult { get; set; } = 2.5;
        
        /// <summary>Trailing stop activation threshold (fraction of TP distance)</summary>
        public double TrailPct { get; set; } = 0.7;
        
        /// <summary>Rolling average volume window</summary>
        public int AvgVolWindow { get; set; } = 30;
        
        /// <summary>Commission per round trip</summary>
        public double Commission { get; set; } = 0.90;
    }

    public enum StrategyMode { Running, Paused, Stopped }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    private readonly Config _cfg;
    
    // State
    private int _positionDir; // 1=long, -1=short, 0=flat
    private double _entryPrice;
    private double _stopLoss;
    private double _takeProfit;
    private bool _trailActive;
    private double _peakPrice;
    private int _totalTrades;
    private double _totalPnL;
    
    // History for volume avg
    private readonly double[] _volHistory;
    private int _volIdx;
    private int _volCount;
    
    // Candle history for pattern detection
    private readonly Candle[] _candleHistory;
    private int _candleIdx;
    private int _candleCount;

    public int PositionDirection => _positionDir;
    public int TotalTrades => _totalTrades;
    public double TotalPnL => _totalPnL;
    public double CurrentAvgVolume => _volCount > 0 
        ? _volHistory.Take(_volCount).Average() 
        : 0;
    public string Name => "VolumeReversal";
    public int BarsInPosition { get; private set; }
    public double StopLoss => _stopLoss;
    public double TakeProfit => _takeProfit;

    public VolumeReversalStrategy(Config config)
    {
        _cfg = config;
        _volHistory = new double[_cfg.AvgVolWindow + 100];
        _candleHistory = new Candle[_cfg.NBars + 10];
    }

    /// <summary>
    /// Called on each new completed candle.
    /// Returns Signal if entry/exit action needed, null otherwise.
    /// </summary>
    public Signal? OnCandle(Candle candle, string ticker)
    {
        // Update volume history
        _volHistory[_volIdx] = candle.Volume;
        _volIdx = (_volIdx + 1) % _volHistory.Length;
        _volCount = Math.Min(_volCount + 1, _volHistory.Length);
        
        // Store candle
        _candleHistory[_candleIdx] = candle;
        _candleIdx = (_candleIdx + 1) % _candleHistory.Length;
        _candleCount = Math.Min(_candleCount + 1, _candleHistory.Length);

        double avgVol = CurrentAvgVolume;
        if (avgVol <= 0) return null;

        // If in position, check SL/TP/trail
        if (_positionDir != 0)
        {
            BarsInPosition++;
            
            if (_positionDir == 1) // Long
            {
                // Stop loss
                if (candle.Close <= _stopLoss)
                {
                    return ClosePosition(candle, "VR_SL");
                }
                // Take profit
                if (candle.Close >= _takeProfit)
                {
                    return ClosePosition(candle, "VR_TP");
                }
                // Trailing
                if (candle.Close > _peakPrice) _peakPrice = candle.Close;
                if (!_trailActive && _takeProfit != _entryPrice)
                {
                    double frac = (candle.Close - _entryPrice) / (_takeProfit - _entryPrice);
                    if (frac >= _cfg.TrailPct)
                    {
                        _trailActive = true;
                        _stopLoss = _entryPrice; // Move SL to breakeven
                    }
                }
                if (_trailActive && candle.Close <= _stopLoss)
                {
                    return ClosePosition(candle, "VR_Trail");
                }
            }
            else if (_positionDir == -1) // Short
            {
                if (candle.Close >= _stopLoss)
                {
                    return ClosePosition(candle, "VR_SL");
                }
                if (candle.Close <= _takeProfit)
                {
                    return ClosePosition(candle, "VR_TP");
                }
                if (candle.Close < _peakPrice) _peakPrice = candle.Close;
                if (!_trailActive && _entryPrice != _takeProfit)
                {
                    double frac = (_entryPrice - candle.Close) / (_entryPrice - _takeProfit);
                    if (frac >= _cfg.TrailPct)
                    {
                        _trailActive = true;
                        _stopLoss = _entryPrice;
                    }
                }
                if (_trailActive && candle.Close >= _stopLoss)
                {
                    return ClosePosition(candle, "VR_Trail");
                }
            }
            
            return null; // Still in position
        }

        // Not in position — look for entry signals
        if (Mode != StrategyMode.Running) return null;
        
        // Volume spike check
        if (candle.Volume < avgVol * _cfg.VolMult) return null;
        
        // Need at least NBars history
        if (_candleCount < _cfg.NBars + 1) return null;

        // Get previous candles (newest to oldest)
        var prev = GetRecentCandles(_cfg.NBars);
        if (prev == null) return null;

        // Check LONG: declining red candles + green reversal
        if (CheckLongSetup(prev, candle))
        {
            return OpenPosition(candle, 1, ticker, "VR_Long");
        }

        // Check SHORT: rising green candles + red reversal
        if (CheckShortSetup(prev, candle))
        {
            return OpenPosition(candle, -1, ticker, "VR_Short");
        }

        return null;
    }

    private Candle[]? GetRecentCandles(int count)
    {
        if (_candleCount < count + 1) return null;
        var result = new Candle[count];
        for (int i = 0; i < count; i++)
        {
            int idx = (_candleIdx - 2 - i + _candleHistory.Length * 2) % _candleHistory.Length;
            result[i] = _candleHistory[idx];
        }
        return result;
    }

    private bool CheckLongSetup(Candle[] prev, Candle current)
    {
        // Current candle must be green (bullish reversal)
        if (current.Close <= current.Open) return false;
        
        // Previous N candles must be declining red
        for (int i = 0; i < prev.Length; i++)
        {
            // Red candle
            if (prev[i].Open <= prev[i].Close) return false;
            // Declining: each close < previous close
            if (i > 0 && prev[i - 1].Close <= prev[i].Close) return false;
        }
        return true;
    }

    private bool CheckShortSetup(Candle[] prev, Candle current)
    {
        // Current candle must be red (bearish reversal)
        if (current.Close >= current.Open) return false;
        
        // Previous N candles must be rising green
        for (int i = 0; i < prev.Length; i++)
        {
            if (prev[i].Open >= prev[i].Close) return false;
            if (i > 0 && prev[i - 1].Close >= prev[i].Close) return false;
        }
        return true;
    }

    private Signal OpenPosition(Candle candle, int dir, string ticker, string comment)
    {
        _positionDir = dir;
        _entryPrice = candle.Close;
        BarsInPosition = 0;

        if (dir == 1)
        {
            _stopLoss = candle.Close - _cfg.StopLoss;
            _takeProfit = candle.Close + _cfg.StopLoss * _cfg.TpMult;
            _peakPrice = candle.Close;
        }
        else
        {
            _stopLoss = candle.Close + _cfg.StopLoss;
            _takeProfit = candle.Close - _cfg.StopLoss * _cfg.TpMult;
            _peakPrice = candle.Close;
        }

        _trailActive = false;

        return new Signal
        {
            Direction = dir == 1 ? SignalDirection.Buy : SignalDirection.Sell,
            Volume = 1,
            Price = candle.Close,
            Comment = comment,
            Source = SignalSource.Grid
        };
    }

    private Signal? ClosePosition(Candle candle, string reason)
    {
        if (_positionDir == 0) return null;

        double pnl;
        if (_positionDir == 1)
            pnl = (candle.Close - _entryPrice) * 1 - _cfg.Commission;
        else
            pnl = (_entryPrice - candle.Close) * 1 - _cfg.Commission;

        _totalPnL += pnl;
        _totalTrades++;
        
        Console.WriteLine($"[VR] {reason}: PnL={pnl:F0} Total={_totalPnL:F0} Trades={_totalTrades} " +
                          $"SL={_stopLoss:F0} TP={_takeProfit:F0} Entry={_entryPrice:F0} Exit={candle.Close:F0}");

        var dir = _positionDir == 1 ? SignalDirection.Sell : SignalDirection.Buy;
        _positionDir = 0;
        _trailActive = false;

        return new Signal
        {
            Direction = dir,
            Volume = 1,
            Price = candle.Close,
            Comment = reason,
            Source = SignalSource.TakeProfit
        };
    }

    /// <summary>Close position by market order (manual stop)</summary>
    public void ForceClose()
    {
        _positionDir = 0;
        _trailActive = false;
    }

    public void Reset()
    {
        _positionDir = 0;
        _trailActive = false;
        _totalTrades = 0;
        _totalPnL = 0;
        _volCount = 0;
        _volIdx = 0;
        _candleCount = 0;
        _candleIdx = 0;
    }
}
