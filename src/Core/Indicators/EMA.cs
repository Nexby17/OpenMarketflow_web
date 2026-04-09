using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>Exponential Moving Average</summary>
public class EMA : IIndicator
{
    private readonly int _period;
    private readonly double _multiplier;
    private double _value;
    public double Value => _value;
    private int _count;

    public string Name => $"EMA({_period})";

    public EMA(int period)
    {
        _period = period;
        _multiplier = 2.0 / (_period + 1);
        _value = double.NaN;
        _count = 0;
    }

    public double[] Calculate(Candle[] candles)
    {
        Reset();
        var result = new double[candles.Length];
        for (int i = 0; i < candles.Length; i++)
            result[i] = Update(candles[i]);
        return result;
    }

    public double Update(Candle candle)
    {
        _count++;
        if (_count == 1)
        {
            _value = candle.Close;
        }
        else
        {
            _value = (candle.Close - _value) * _multiplier + _value;
        }
        return _count >= _period ? _value : double.NaN;
    }

    public void Reset()
    {
        _value = double.NaN;
        _count = 0;
    }
}
