using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

/// <summary>Parabolic SAR</summary>
public class ParabolicSAR
{
    private readonly double _afStart;
    private readonly double _afStep;
    private readonly double _afMax;

    private double _sar;
    private double _ep;      // extreme point
    private double _af;
    private int _trend;       // 1=up, -1=down
    private int _count;
    private int _prevTrend;

    public string Name => $"PSAR({_afStart}/{_afStep}/{_afMax})";
    public double Value => _sar;
    public int Trend => _trend;
    public int PrevTrend => _prevTrend;

    /// <summary>SAR flip: тренд изменился на текущем баре</summary>
    public bool FlippedUp => _prevTrend == -1 && _trend == 1;
    public bool FlippedDown => _prevTrend == 1 && _trend == -1;

    public ParabolicSAR(double afStart, double afStep, double afMax)
    {
        _afStart = afStart;
        _afStep = afStep;
        _afMax = afMax;
        Reset();
    }

    public double Update(Candle candle)
    {
        _count++;
        _prevTrend = _trend;

        if (_count == 1)
        {
            _trend = 1;
            _sar = candle.Low;
            _ep = candle.High;
            _af = _afStart;
            return double.NaN;
        }

        if (_trend == 1)
        {
            double newSar = _sar + _af * (_ep - _sar);
            // SAR не выше Low предыдущего бара (упрощённо — текущего)
            newSar = Math.Min(newSar, candle.Low);

            if (candle.Low < newSar)
            {
                // Разворот вниз
                _trend = -1;
                _sar = _ep;
                _ep = candle.Low;
                _af = _afStart;
            }
            else
            {
                _sar = newSar;
                if (candle.High > _ep)
                {
                    _ep = candle.High;
                    _af = Math.Min(_af + _afStep, _afMax);
                }
            }
        }
        else
        {
            double newSar = _sar + _af * (_ep - _sar);
            newSar = Math.Max(newSar, candle.High);

            if (candle.High > newSar)
            {
                // Разворот вверх
                _trend = 1;
                _sar = _ep;
                _ep = candle.High;
                _af = _afStart;
            }
            else
            {
                _sar = newSar;
                if (candle.Low < _ep)
                {
                    _ep = candle.Low;
                    _af = Math.Min(_af + _afStep, _afMax);
                }
            }
        }

        return _count >= 2 ? _sar : double.NaN;
    }

    public void Reset()
    {
        _sar = double.NaN;
        _ep = 0;
        _af = _afStart;
        _trend = 0;
        _prevTrend = 0;
        _count = 0;
    }
}
