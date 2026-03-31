using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// VWAP Reversion — внутридневной VWAP, вход при отклонении > 1.5σ, выход < 0.3σ.
/// Расчёт VWAP и стандартного отклонения ведётся с начала дня.
/// </summary>
public class VwapReversionStrategy : IStrategy
{
    private readonly double _entryThreshold;
    private readonly double _exitThreshold;

    // Аккумуляторы VWAP (сбрасываются в начале дня)
    private double _cumulativeTPV; // ∑(TP * Volume)
    private double _cumulativeVolume; // ∑Volume
    private double _cumulativeTPV2; // ∑(TP² * Volume) для дисперсии
    private DateTime _currentDay;
    private int _barCount;

    private bool _inPosition;
    private SignalDirection _positionDir;

    public string Name => "VWAP Reversion";

    public VwapReversionStrategy(double entryThreshold = 1.5, double exitThreshold = 0.3)
    {
        _entryThreshold = entryThreshold;
        _exitThreshold = exitThreshold;
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        // Сброс в начале нового дня
        if (candle.Timestamp.Date != _currentDay)
        {
            _currentDay = candle.Timestamp.Date;
            _cumulativeTPV = 0;
            _cumulativeVolume = 0;
            _cumulativeTPV2 = 0;
            _barCount = 0;

            // Закрываем позицию при смене дня
            if (_inPosition)
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
                    Comment = "Закрытие позиции: новый торговый день"
                };
            }
        }

        double tp = (candle.High + candle.Low + candle.Close) / 3.0;
        double vol = candle.Volume;

        if (vol <= 0) return null;

        _cumulativeTPV += tp * vol;
        _cumulativeVolume += vol;
        _cumulativeTPV2 += tp * tp * vol;
        _barCount++;

        if (_barCount < 10 || _cumulativeVolume <= 0) return null;

        double vwap = _cumulativeTPV / _cumulativeVolume;
        // Взвешенная дисперсия: E[X²] - E[X]²
        double variance = (_cumulativeTPV2 / _cumulativeVolume) - (vwap * vwap);
        if (variance < 0) variance = 0;
        double stdDev = Math.Sqrt(variance);

        if (stdDev < 0.01) return null;

        double zScore = (candle.Close - vwap) / stdDev;

        // Выход
        if (_inPosition)
        {
            if (Math.Abs(zScore) < _exitThreshold)
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
                    Comment = $"Выход: Z={zScore:F2} < {_exitThreshold}, VWAP={vwap:F2}"
                };
            }
            return null;
        }

        // Вход Long: цена сильно ниже VWAP
        if (zScore < -_entryThreshold)
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
                Comment = $"Z={zScore:F2} < -{_entryThreshold}, VWAP={vwap:F2}, σ={stdDev:F2}"
            };
        }

        // Вход Short: цена сильно выше VWAP
        if (zScore > _entryThreshold)
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
                Comment = $"Z={zScore:F2} > {_entryThreshold}, VWAP={vwap:F2}, σ={stdDev:F2}"
            };
        }

        return null;
    }

    public void Reset()
    {
        _cumulativeTPV = 0;
        _cumulativeVolume = 0;
        _cumulativeTPV2 = 0;
        _currentDay = DateTime.MinValue;
        _barCount = 0;
        _inPosition = false;
        _positionDir = SignalDirection.None;
    }
}
