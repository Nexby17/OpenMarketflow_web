using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Bollinger Bounce — вход при касании границ BB, выход к средней линии.
/// Long при Close ≤ Lower BB, Short при Close ≥ Upper BB.
/// </summary>
public class BollingerBounceStrategy : IStrategy
{
    private readonly BollingerBands _bb;
    private bool _inPosition;
    private SignalDirection _positionDir;

    public string Name => "Bollinger Bounce";

    public BollingerBounceStrategy(int period = 15, double stdDev = 2.0)
    {
        _bb = new BollingerBands(period, stdDev);
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        _bb.Update(candle);

        if (double.IsNaN(_bb.Upper)) return null;

        // Выход к средней
        if (_inPosition)
        {
            bool shouldExit = false;
            string comment = "";

            if (_positionDir == SignalDirection.Buy && candle.Close >= _bb.Middle)
            {
                shouldExit = true;
                comment = $"Выход Long: цена {candle.Close:F2} >= Middle {_bb.Middle:F2}";
            }
            else if (_positionDir == SignalDirection.Sell && candle.Close <= _bb.Middle)
            {
                shouldExit = true;
                comment = $"Выход Short: цена {candle.Close:F2} <= Middle {_bb.Middle:F2}";
            }

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
                    Comment = comment
                };
            }
            return null;
        }

        // Вход Long — касание нижней BB
        if (candle.Close <= _bb.Lower)
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
                Comment = $"Касание Lower BB={_bb.Lower:F2}"
            };
        }

        // Вход Short — касание верхней BB
        if (candle.Close >= _bb.Upper)
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
                Comment = $"Касание Upper BB={_bb.Upper:F2}"
            };
        }

        return null;
    }

    public void Reset()
    {
        _bb.Reset();
        _inPosition = false;
        _positionDir = SignalDirection.None;
    }
}
