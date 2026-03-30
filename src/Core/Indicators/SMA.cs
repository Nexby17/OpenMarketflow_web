using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>Simple Moving Average</summary>
public class SMA : IIndicator
{
    private readonly int _period;
    private readonly Queue<double> _buffer;
    private double _sum;

    public string Name => $"SMA({_period})";

    public SMA(int period)
    {
        _period = period;
        _buffer = new Queue<double>(period);
        _sum = 0;
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
        _buffer.Enqueue(candle.Close);
        _sum += candle.Close;

        if (_buffer.Count > _period)
            _sum -= _buffer.Dequeue();

        return _buffer.Count >= _period ? _sum / _period : double.NaN;
    }

    public void Reset()
    {
        _buffer.Clear();
        _sum = 0;
    }
}
