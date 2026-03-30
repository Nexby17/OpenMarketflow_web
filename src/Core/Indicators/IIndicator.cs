using HedgeFund.Core.Models;

namespace HedgeFund.Core.Indicators;

public interface IIndicator
{
    string Name { get; }
    
    /// <summary>Рассчитать значение индикатора на массиве свечей</summary>
    double[] Calculate(Candle[] candles);
    
    /// <summary>Добавить одну свечу (для онлайн-расчёта)</summary>
    double Update(Candle candle);
    
    /// <summary>Сбросить состояние</summary>
    void Reset();
}
