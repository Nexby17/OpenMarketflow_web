using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Стратегия прорыва (Breakout).
/// Вход при пробое Bollinger Bands + подтверждение объёмом и ATR.
/// 
/// Buy: цена пробивает верхнюю BB + объём выше среднего + ATR растёт
/// Sell: цена пробивает нижнюю BB + объём выше среднего + ATR растёт
/// </summary>
public class BreakoutStrategy : IStrategy
{
    private readonly BollingerBands _bb;
    private readonly ATR _atr;
    private readonly SMA _volumeSma;
    private readonly int _volumePeriod;
    private double _prevAtr;
    private int _count;

    public string Name => "Breakout (BB + ATR + Volume)";

    public BreakoutStrategy(int bbPeriod = 20, double bbStdDev = 2.0, int atrPeriod = 14, int volumePeriod = 20)
    {
        _bb = new BollingerBands(bbPeriod, bbStdDev);
        _atr = new ATR(atrPeriod);
        _volumePeriod = volumePeriod;
        _volumeSma = new SMA(volumePeriod);
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        _count++;
        _bb.Update(candle);
        double atr = _atr.Update(candle);
        
        // SMA объёма — используем фейковую свечу
        double avgVolume = _volumeSma.Update(new Candle { Close = candle.Volume });

        // Недостаточно данных
        if (double.IsNaN(_bb.Upper) || double.IsNaN(atr) || double.IsNaN(avgVolume))
        {
            _prevAtr = atr;
            return null;
        }

        bool volumeConfirm = candle.Volume > avgVolume * 1.2; // Объём на 20% выше среднего
        bool atrGrowing = !double.IsNaN(_prevAtr) && atr > _prevAtr; // Волатильность растёт

        Signal? signal = null;

        // Пробой верхней BB — сигнал Buy
        if (candle.Close > _bb.Upper && volumeConfirm && atrGrowing)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Buy,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Пробой верхней BB ({_bb.Upper:F2}), ATR: {atr:F2}, Vol: {candle.Volume}/{avgVolume:F0}"
            };
        }
        // Пробой нижней BB — сигнал Sell
        else if (candle.Close < _bb.Lower && volumeConfirm && atrGrowing)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Sell,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Пробой нижней BB ({_bb.Lower:F2}), ATR: {atr:F2}, Vol: {candle.Volume}/{avgVolume:F0}"
            };
        }

        _prevAtr = atr;
        return signal;
    }

    public void Reset()
    {
        _bb.Reset();
        _atr.Reset();
        _volumeSma.Reset();
        _prevAtr = 0;
        _count = 0;
    }
}
