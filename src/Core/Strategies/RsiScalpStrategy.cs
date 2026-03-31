using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// RSI Scalp — быстрый RSI(7), вход при перепроданности/перекупленности.
/// Long при RSI &lt; 25, Short при RSI &gt; 75, выход при RSI ~50.
/// </summary>
public class RsiScalpStrategy : IStrategy
{
    private readonly RSI _rsi;
    private readonly double _longEntry;
    private readonly double _shortEntry;
    private readonly double _exitLevel;
    private readonly double _exitBand;

    private bool _inPosition;
    private SignalDirection _positionDir;

    public string Name => "RSI Scalp";

    public RsiScalpStrategy(
        int rsiPeriod = 7,
        double longEntry = 25,
        double shortEntry = 75,
        double exitLevel = 50,
        double exitBand = 5)
    {
        _rsi = new RSI(rsiPeriod);
        _longEntry = longEntry;
        _shortEntry = shortEntry;
        _exitLevel = exitLevel;
        _exitBand = exitBand;
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        double rsi = _rsi.Update(candle);
        if (double.IsNaN(rsi)) return null;

        // Выход
        if (_inPosition)
        {
            bool shouldExit = Math.Abs(rsi - _exitLevel) <= _exitBand;

            if (shouldExit)
            {
                var exitDir = _positionDir == SignalDirection.Buy ? SignalDirection.Sell : SignalDirection.Buy;
                _inPosition = false;
                return new Signal
                {
                    Timestamp = candle.Timestamp,
                    Ticker = ticker,
                    Direction = exitDir,
                    Source = SignalSource.Exit,
                    StrategyName = Name,
                    Price = candle.Close,
                    Comment = $"Выход RSI={rsi:F1} ≈ {_exitLevel}"
                };
            }
            return null;
        }

        // Вход Long
        if (rsi < _longEntry)
        {
            _inPosition = true;
            _positionDir = SignalDirection.Buy;
            return new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Buy,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"RSI={rsi:F1} < {_longEntry} — перепроданность"
            };
        }

        // Вход Short
        if (rsi > _shortEntry)
        {
            _inPosition = true;
            _positionDir = SignalDirection.Sell;
            return new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Sell,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"RSI={rsi:F1} > {_shortEntry} — перекупленность"
            };
        }

        return null;
    }

    public void Reset()
    {
        _rsi.Reset();
        _inPosition = false;
        _positionDir = SignalDirection.None;
    }
}
