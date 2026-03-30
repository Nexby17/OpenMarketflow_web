using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>Relative Strength Index</summary>
public class RSI : IIndicator
{
    private readonly int _period;
    private double _avgGain;
    private double _avgLoss;
    private double _prevClose;
    private int _count;

    public string Name => $"RSI({_period})";

    public RSI(int period = 14)
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
        
        if (_count == 1)
        {
            _prevClose = candle.Close;
            return double.NaN;
        }

        double change = candle.Close - _prevClose;
        double gain = change > 0 ? change : 0;
        double loss = change < 0 ? -change : 0;

        if (_count <= _period + 1)
        {
            _avgGain += gain / _period;
            _avgLoss += loss / _period;
        }
        else
        {
            _avgGain = (_avgGain * (_period - 1) + gain) / _period;
            _avgLoss = (_avgLoss * (_period - 1) + loss) / _period;
        }

        _prevClose = candle.Close;

        if (_count <= _period) return double.NaN;

        if (_avgLoss == 0) return 100;
        double rs = _avgGain / _avgLoss;
        return 100 - (100 / (1 + rs));
    }

    public void Reset()
    {
        _avgGain = 0;
        _avgLoss = 0;
        _prevClose = 0;
        _count = 0;
    }
}
