using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>
/// Bollinger Bands. 
/// Calculate() возвращает среднюю линию (SMA).
/// Upper/Lower доступны через свойства.
/// </summary>
public class BollingerBands : IIndicator
{
    private readonly int _period;
    private readonly double _stdDevMultiplier;
    private readonly Queue<double> _buffer;

    public string Name => $"BB({_period},{_stdDevMultiplier})";

    public double Middle { get; private set; }
    public double Upper { get; private set; }
    public double Lower { get; private set; }
    public double BandWidth => Upper - Lower;

    public BollingerBands(int period = 20, double stdDevMultiplier = 2.0)
    {
        _period = period;
        _stdDevMultiplier = stdDevMultiplier;
        _buffer = new Queue<double>(period);
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
        if (_buffer.Count > _period) _buffer.Dequeue();

        if (_buffer.Count < _period)
        {
            Middle = Upper = Lower = double.NaN;
            return double.NaN;
        }

        double mean = _buffer.Average();
        double variance = _buffer.Select(x => (x - mean) * (x - mean)).Average();
        double stdDev = Math.Sqrt(variance);

        Middle = mean;
        Upper = mean + stdDev * _stdDevMultiplier;
        Lower = mean - stdDev * _stdDevMultiplier;

        return Middle;
    }

    public void Reset()
    {
        _buffer.Clear();
        Middle = Upper = Lower = double.NaN;
    }
}
