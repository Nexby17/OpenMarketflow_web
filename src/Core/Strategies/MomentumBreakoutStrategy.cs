using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Momentum Breakout — пробой канала high/low с подтверждением объёмом.
/// Лучшая стратегия с оптимизированными параметрами.
/// </summary>
public class MomentumBreakoutStrategy : IStrategy
{
    private readonly int _channelPeriod;
    private readonly double _volumeMult;
    private readonly int _exitSmaPeriod;
    private readonly double _takeProfit;
    private readonly double _stopLoss;

    private readonly SMA _exitSma;
    private readonly Queue<double> _highs;
    private readonly Queue<double> _lows;
    private readonly Queue<long> _volumes;
    private const int VolumeLookback = 20;

    private bool _inPosition;
    private SignalDirection _positionDir;
    private double _entryPrice;
    private int _count;

    public string Name => "Momentum Breakout";

    public MomentumBreakoutStrategy(
        int channelPeriod = 10,
        double volumeMult = 2.0,
        int exitSma = 5,
        double takeProfit = 200,
        double stopLoss = 0)
    {
        _channelPeriod = channelPeriod;
        _volumeMult = volumeMult;
        _exitSmaPeriod = exitSma;
        _takeProfit = takeProfit;
        _stopLoss = stopLoss;

        _exitSma = new SMA(exitSma);
        _highs = new Queue<double>();
        _lows = new Queue<double>();
        _volumes = new Queue<long>();
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        _count++;

        // Обновляем буферы
        _highs.Enqueue(candle.High);
        _lows.Enqueue(candle.Low);
        _volumes.Enqueue(candle.Volume);

        if (_highs.Count > _channelPeriod) _highs.Dequeue();
        if (_lows.Count > _channelPeriod) _lows.Dequeue();
        if (_volumes.Count > VolumeLookback) _volumes.Dequeue();

        double smaValue = _exitSma.Update(candle);

        // Недостаточно данных
        if (_count < _channelPeriod || _volumes.Count < VolumeLookback)
            return null;

        double channelHigh = _highs.Max();
        double channelLow = _lows.Min();
        double avgVolume = _volumes.Average();

        // Если в позиции — проверяем выход
        if (_inPosition)
        {
            bool exitSignal = false;
            string comment = "";

            // Выход по SMA
            if (!double.IsNaN(smaValue))
            {
                if (_positionDir == SignalDirection.Buy && candle.Close < smaValue)
                {
                    exitSignal = true;
                    comment = $"Выход Long: цена {candle.Close:F2} < SMA({_exitSmaPeriod}) {smaValue:F2}";
                }
                else if (_positionDir == SignalDirection.Sell && candle.Close > smaValue)
                {
                    exitSignal = true;
                    comment = $"Выход Short: цена {candle.Close:F2} > SMA({_exitSmaPeriod}) {smaValue:F2}";
                }
            }

            // Выход по TP
            if (!exitSignal && _takeProfit > 0)
            {
                double pnl = _positionDir == SignalDirection.Buy
                    ? candle.Close - _entryPrice
                    : _entryPrice - candle.Close;
                if (pnl >= _takeProfit)
                {
                    exitSignal = true;
                    comment = $"Take Profit: PnL={pnl:F2} >= {_takeProfit:F2}";
                }
            }

            // Выход по SL
            if (!exitSignal && _stopLoss > 0)
            {
                double pnl = _positionDir == SignalDirection.Buy
                    ? candle.Close - _entryPrice
                    : _entryPrice - candle.Close;
                if (pnl <= -_stopLoss)
                {
                    exitSignal = true;
                    comment = $"Stop Loss: PnL={pnl:F2} <= -{_stopLoss:F2}";
                }
            }

            if (exitSignal)
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
                    Comment = comment
                };
            }

            return null;
        }

        // Вход: пробой канала + объём
        bool volumeConfirm = candle.Volume > avgVolume * _volumeMult;

        if (candle.Close > channelHigh && volumeConfirm)
        {
            _inPosition = true;
            _positionDir = SignalDirection.Buy;
            _entryPrice = candle.Close;
            return new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Buy,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Пробой High {channelHigh:F2}, Vol={candle.Volume}/{avgVolume:F0}*{_volumeMult}"
            };
        }

        if (candle.Close < channelLow && volumeConfirm)
        {
            _inPosition = true;
            _positionDir = SignalDirection.Sell;
            _entryPrice = candle.Close;
            return new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Sell,
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Пробой Low {channelLow:F2}, Vol={candle.Volume}/{avgVolume:F0}*{_volumeMult}"
            };
        }

        return null;
    }

    public void Reset()
    {
        _exitSma.Reset();
        _highs.Clear();
        _lows.Clear();
        _volumes.Clear();
        _inPosition = false;
        _positionDir = SignalDirection.None;
        _entryPrice = 0;
        _count = 0;
    }
}
