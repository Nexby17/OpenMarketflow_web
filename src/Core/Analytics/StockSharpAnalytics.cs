// StockSharp аналитика: индикаторы
using StockSharp.Algo.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Analytics;

/// <summary>
/// Обёртка над StockSharp индикаторами для простого использования.
/// Используем готовые PSAR, EMA, ATR и другие индикаторы из StockSharp.
/// </summary>
public class StockSharpAnalytics
{
    /// <summary>
    /// Создаёт PSAR индикатор
    /// </summary>
    public static ParabolicSar CreatePSAR(double start = 0.02, double step = 0.02, double max = 0.2)
    {
        return new ParabolicSar
        {
            Acceleration = (decimal)start,
            AccelerationStep = (decimal)step,
            AccelerationMax = (decimal)max
        };
    }

    /// <summary>
    /// Создаёт EMA индикатор
    /// </summary>
    public static ExponentialMovingAverage CreateEMA(int period)
    {
        return new ExponentialMovingAverage { Length = period };
    }

    /// <summary>
    /// Создаёт ATR индикатор
    /// </summary>
    public static AverageTrueRange CreateATR(int period)
    {
        return new AverageTrueRange { Length = period };
    }

    /// <summary>
    /// Создаёт SMA индикатор
    /// </summary>
    public static SimpleMovingAverage CreateSMA(int period)
    {
        return new SimpleMovingAverage { Length = period };
    }

    /// <summary>
    /// Создаёт RSI индикатор
    /// </summary>
    public static RelativeStrengthIndex CreateRSI(int period = 14)
    {
        return new RelativeStrengthIndex { Length = period };
    }

    /// <summary>
    /// Создаёт MACD индикатор
    /// </summary>
    public static MACD CreateMACD(int fast = 12, int slow = 26, int signal = 9)
    {
        var macd = new MACD();
        if (macd.ShortMa != null) macd.ShortMa.Length = fast;
        if (macd.LongMa != null) macd.LongMa.Length = slow;
        // SignalLine создаётся автоматически
        return macd;
    }

    /// <summary>
    /// Обновляет индикатор ценой. Возвращает значение индикатора или null если не сформирован.
    /// </summary>
    public static decimal? ProcessPrice(object indicator, decimal price, DateTimeOffset time)
    {
        var createValue = indicator.GetType().GetMethod("CreateValue",
            new[] { typeof(DateTimeOffset), typeof(object[]) });
        var process = indicator.GetType().GetMethod("Process", new[] { typeof(IIndicatorValue) });

        if (createValue == null || process == null)
            return null;

        var value = (IIndicatorValue)createValue.Invoke(indicator, new object[] { time, new object[] { price } })!;
        var result = (IIndicatorValue)process.Invoke(indicator, new object[] { value })!;

        // Получаем значение из свойства Value результата
        var valueProp = result.GetType().GetProperty("Value");
        return (decimal?)valueProp?.GetValue(result);
    }

    /// <summary>
    /// Обновляет индикатор свечой (для ATR и других OHLC индикаторов). Возвращает значение или null.
    /// </summary>
    public static decimal? ProcessCandle(object indicator, Candle candle)
    {
        var createValue = indicator.GetType().GetMethod("CreateValue",
            new[] { typeof(DateTimeOffset), typeof(object[]) });
        var process = indicator.GetType().GetMethod("Process", new[] { typeof(IIndicatorValue) });

        if (createValue == null || process == null)
            return null;

        var value = (IIndicatorValue)createValue.Invoke(indicator, new object[]
        {
            new DateTimeOffset(candle.Timestamp),
            new object[] { candle.Open, candle.High, candle.Low, candle.Close, (long)candle.Volume }
        })!;
        var result = (IIndicatorValue)process.Invoke(indicator, new object[] { value })!;

        var valueProp = result.GetType().GetProperty("Value");
        return (decimal?)valueProp?.GetValue(result);
    }

    /// <summary>
    /// Проверяет что индикатор сформирован (есть достаточно данных)
    /// </summary>
    public static bool IsFormed(object indicator)
    {
        var prop = indicator.GetType().GetProperty("IsFormed");
        return prop != null && (bool)prop.GetValue(indicator)!;
    }

    /// <summary>
    /// Сбрасывает индикатор
    /// </summary>
    public static void Reset(object indicator)
    {
        var method = indicator.GetType().GetMethod("Reset", Type.EmptyTypes);
        method?.Invoke(indicator, null);
    }

    /// <summary>
    /// Рассчитывает исторические значения индикатора для списка цен
    /// </summary>
    public static List<decimal?> CalculatePriceHistory(object indicator, List<(decimal price, DateTimeOffset time)> prices)
    {
        var results = new List<decimal?>();
        foreach (var (price, time) in prices)
        {
            results.Add(ProcessPrice(indicator, price, time));
        }
        return results;
    }

    /// <summary>
    /// Рассчитывает исторические значения индикатора для списка свечей
    /// </summary>
    public static List<decimal?> CalculateCandleHistory(object indicator, List<Candle> candles)
    {
        var results = new List<decimal?>();
        foreach (var candle in candles)
        {
            results.Add(ProcessCandle(indicator, candle));
        }
        return results;
    }
}
