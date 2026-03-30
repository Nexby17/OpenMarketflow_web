using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>Average True Range — для расчёта волатильности и стопов</summary>
public class ATR : IIndicator
{
    private readonly int _period;
    private double _prevClose;
    private double _atr;
    private int _count;

    public string Name => $"ATR({_period})";

    public ATR(int period = 14)
    {
        _period = period;
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

        double tr;
        if (_count == 1)
        {
            tr = candle.High - candle.Low;
            _atr = tr;
        }
        else
        {
            tr = Math.Max(candle.High - candle.Low,
                 Math.Max(Math.Abs(candle.High - _prevClose),
                          Math.Abs(candle.Low - _prevClose)));

            if (_count <= _period)
                _atr = ((_atr * (_count - 1)) + tr) / _count;
            else
                _atr = (_atr * (_period - 1) + tr) / _period;
        }

        _prevClose = candle.Close;
        return _count >= _period ? _atr : double.NaN;
    }

    public void Reset()
    {
        _prevClose = 0;
        _atr = 0;
        _count = 0;
    }
}
