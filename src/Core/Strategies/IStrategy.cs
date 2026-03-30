using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

public interface IStrategy
{
    string Name { get; }
    
    /// <summary>Обработать новую свечу и вернуть сигнал (или null)</summary>
    Signal? OnCandle(Candle candle, string ticker);
    
    /// <summary>Сбросить состояние стратегии</summary>
    void Reset();
}
