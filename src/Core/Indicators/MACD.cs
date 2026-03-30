using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>
/// MACD (Moving Average Convergence Divergence)
/// Возвращает значение MACD-линии (fast EMA - slow EMA)
/// </summary>
public class MACD : IIndicator
{
    private readonly EMA _fastEma;
    private readonly EMA _slowEma;
    private readonly EMA _signalEma;
    private int _count;

    public string Name => $"MACD({_fastEma.Name},{_slowEma.Name})";

    // Доступ к компонентам для стратегий
    public double MACDLine { get; private set; }
    public double SignalLine { get; private set; }
    public double Histogram { get; private set; }

    public MACD(int fastPeriod = 12, int slowPeriod = 26, int signalPeriod = 9)
    {
        _fastEma = new EMA(fastPeriod);
        _slowEma = new EMA(slowPeriod);
        _signalEma = new EMA(signalPeriod);
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
        double fast = _fastEma.Update(candle);
        double slow = _slowEma.Update(candle);

        if (double.IsNaN(fast) || double.IsNaN(slow))
        {
            MACDLine = double.NaN;
            SignalLine = double.NaN;
            Histogram = double.NaN;
            return double.NaN;
        }

        MACDLine = fast - slow;

        // Используем фейковую свечу для расчёта сигнальной линии
        var fakeCandle = new Candle { Close = MACDLine };
        SignalLine = _signalEma.Update(fakeCandle);
        
        Histogram = double.IsNaN(SignalLine) ? double.NaN : MACDLine - SignalLine;

        return MACDLine;
    }

    public void Reset()
    {
        _fastEma.Reset();
        _slowEma.Reset();
        _signalEma.Reset();
        _count = 0;
        MACDLine = double.NaN;
        SignalLine = double.NaN;
        Histogram = double.NaN;
    }
}
