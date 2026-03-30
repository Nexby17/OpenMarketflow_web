using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Стратегия арбитража акция/фьючерс (сбор спреда).
/// Мониторит разницу между ценой акции и фьючерса.
/// 
/// Buy спред (buy акцию + sell фьючерс): когда спред расширился выше нормы
/// Sell спред (sell акцию + buy фьючерс): когда спред сузился ниже нормы
/// 
/// Для полноценной работы нужны два потока данных: акция и фьючерс.
/// Пока реализована упрощённая версия на одном инструменте с BB на спреде.
/// </summary>
public class SpreadStrategy : IStrategy
{
    private readonly BollingerBands _spreadBB;
    private readonly SMA _spreadSma;
    
    // Для расчёта спреда нужна цена второго инструмента
    private double _lastSpotPrice;
    private double _lastFuturesPrice;
    
    public string Name => "Spread Arbitrage (Spot/Futures)";
    
    /// <summary>Порог отклонения спреда для входа (в сигмах)</summary>
    public double EntryThreshold { get; set; } = 2.0;
    
    public SpreadStrategy(int period = 20, double stdDev = 2.0)
    {
        _spreadBB = new BollingerBands(period, stdDev);
        _spreadSma = new SMA(period);
    }

    /// <summary>Обновить цену спот-инструмента</summary>
    public void UpdateSpotPrice(double price) => _lastSpotPrice = price;
    
    /// <summary>Обновить цену фьючерса</summary>
    public void UpdateFuturesPrice(double price) => _lastFuturesPrice = price;
    
    /// <summary>Текущий спред (фьючерс - спот)</summary>
    public double CurrentSpread => _lastFuturesPrice - _lastSpotPrice;

    public Signal? OnCandle(Candle candle, string ticker)
    {
        if (_lastSpotPrice <= 0 || _lastFuturesPrice <= 0)
            return null;

        double spread = CurrentSpread;
        
        // Используем фейковую свечу со спредом в качестве Close
        var spreadCandle = new Candle 
        { 
            Timestamp = candle.Timestamp,
            Close = spread,
            Open = spread,
            High = spread,
            Low = spread
        };
        
        _spreadBB.Update(spreadCandle);
        double avgSpread = _spreadSma.Update(spreadCandle);

        if (double.IsNaN(_spreadBB.Upper) || double.IsNaN(avgSpread))
            return null;

        Signal? signal = null;

        // Спред расширился выше верхней BB → продаём спред (sell фьючерс, buy акцию)
        if (spread > _spreadBB.Upper)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Sell, // Sell futures
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Спред расширен: {spread:F2} > BB.Upper: {_spreadBB.Upper:F2}, среднее: {avgSpread:F2}"
            };
        }
        // Спред сузился ниже нижней BB → покупаем спред (buy фьючерс, sell акцию)
        else if (spread < _spreadBB.Lower)
        {
            signal = new Signal
            {
                Timestamp = candle.Timestamp,
                Ticker = ticker,
                Direction = SignalDirection.Buy, // Buy futures
                Source = SignalSource.Strategy,
                StrategyName = Name,
                Price = candle.Close,
                Comment = $"Спред сужен: {spread:F2} < BB.Lower: {_spreadBB.Lower:F2}, среднее: {avgSpread:F2}"
            };
        }

        return signal;
    }

    public void Reset()
    {
        _spreadBB.Reset();
        _spreadSma.Reset();
        _lastSpotPrice = 0;
        _lastFuturesPrice = 0;
    }
}
