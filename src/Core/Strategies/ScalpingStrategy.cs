using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Скальпинговая стратегия.
/// Вход по пересечению быстрой и медленной EMA + RSI фильтр.
/// 
/// Buy: fast EMA пересекает slow EMA снизу вверх + RSI < 70
/// Sell: fast EMA пересекает slow EMA сверху вниз + RSI > 30
/// </summary>
public class ScalpingStrategy : IStrategy
{
    private readonly EMA _fastEma;
    private readonly EMA _slowEma;
    private readonly RSI _rsi;
    private double _prevFast;
    private double _prevSlow;
    private int _count;

    public string Name => $"Scalping (EMA Cross + RSI)";

    public double OverboughtLevel { get; set; } = 70;
    public double OversoldLevel { get; set; } = 30;

    public ScalpingStrategy(int fastPeriod = 9, int slowPeriod = 21, int rsiPeriod = 14)
    {
        _fastEma = new EMA(fastPeriod);
        _slowEma = new EMA(slowPeriod);
        _rsi = new RSI(rsiPeriod);
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        _count++;
        double fast = _fastEma.Update(candle);
        double slow = _slowEma.Update(candle);
        double rsi = _rsi.Update(candle);

        // Недостаточно данных
        if (double.IsNaN(fast) || double.IsNaN(slow) || double.IsNaN(rsi) 
            || double.IsNaN(_prevFast) || double.IsNaN(_prevSlow))
        {
            _prevFast = fast;
            _prevSlow = slow;
            return null;
        }

        Signal? signal = null;

        // Золотой крест: fast пересекла slow снизу вверх
        bool goldenCross = _prevFast <= _prevSlow && fast > slow;
        // Мёртвый крест: fast пересекла slow сверху вниз
        bool deathCross = _prevFast >= _prevSlow && fast < slow;

        if (goldenCross && rsi < OverboughtLevel)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Buy,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Golden Cross. Fast: {fast:F2}, Slow: {slow:F2}, RSI: {rsi:F1}"
            };
        }
        else if (deathCross && rsi > OversoldLevel)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Sell,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Death Cross. Fast: {fast:F2}, Slow: {slow:F2}, RSI: {rsi:F1}"
            };
        }

        _prevFast = fast;
        _prevSlow = slow;
        return signal;
    }

    public void Reset()
    {
        _fastEma.Reset();
        _slowEma.Reset();
        _rsi.Reset();
        _prevFast = double.NaN;
        _prevSlow = double.NaN;
        _count = 0;
    }
}
