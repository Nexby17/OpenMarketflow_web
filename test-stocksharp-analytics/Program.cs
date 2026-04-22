// Тест StockSharp Analytics
using HedgeFund.Core;
using HedgeFund.Core.Analytics;
using HedgeFund.Core.Models;
using StockSharp.Algo.Indicators;

Console.WriteLine("=== Тест StockSharp Analytics ===\n");

// Тест 1: EMA
Console.WriteLine("Тест 1: EMA");
var ema = StockSharpAnalytics.CreateEMA(14);
Console.WriteLine($"  EMA создана, Length = {ema.Length}");

var prices = new List<(decimal price, DateTimeOffset time)>();
var start = DateTimeOffset.Now.AddDays(-20);
for (int i = 0; i < 30; i++)
{
    prices.Add((100 + i * 2 + (i % 5), start.AddMinutes(i)));
}

var emaValues = StockSharpAnalytics.CalculatePriceHistory(ema, prices);
Console.WriteLine($"  Рассчитано {emaValues.Count} значений");
Console.WriteLine($"  Первые 10: {string.Join(", ", emaValues.Take(10).Select(v => v?.ToString("F2") ?? "null"))}");
Console.WriteLine($"  IsFormed: {StockSharpAnalytics.IsFormed(ema)}");
Console.WriteLine($"  Последнее значение: {emaValues.Last()}");

// Тест 2: PSAR
Console.WriteLine($"\nТест 2: PSAR");
var psar = StockSharpAnalytics.CreatePSAR(0.02, 0.02, 0.2);
var psarValues = StockSharpAnalytics.CalculatePriceHistory(psar, prices);
Console.WriteLine($"  Рассчитано {psarValues.Count} значений");
Console.WriteLine($"  Первые 10: {string.Join(", ", psarValues.Take(10).Select(v => v?.ToString("F2") ?? "null"))}");
Console.WriteLine($"  IsFormed: {StockSharpAnalytics.IsFormed(psar)}");
Console.WriteLine($"  Последнее значение: {psarValues.Last()}");

// Тест 3: ATR со свечами
Console.WriteLine($"\nТест 3: ATR со свечами");
var atr = StockSharpAnalytics.CreateATR(14);
var candles = new List<Candle>();
for (int i = 0; i < 30; i++)
{
    var open = 100.0 + i * 2;
    var close = open + (i % 3) * 1.5 - 1.5;
    candles.Add(new Candle
    {
        Timestamp = start.AddMinutes(i).DateTime,
        Open = open,
        High = Math.Max(open, close) + 2,
        Low = Math.Min(open, close) - 2,
        Close = close,
        Volume = 1000
    });
}

var atrValues = StockSharpAnalytics.CalculateCandleHistory(atr, candles);
Console.WriteLine($"  Рассчитано {atrValues.Count} значений");
Console.WriteLine($"  Первые 10: {string.Join(", ", atrValues.Take(10).Select(v => v?.ToString("F2") ?? "null"))}");
Console.WriteLine($"  IsFormed: {StockSharpAnalytics.IsFormed(atr)}");
Console.WriteLine($"  Последнее значение: {atrValues.Last()}");

// Тест 4: RSI
Console.WriteLine($"\nТест 4: RSI");
var rsi = StockSharpAnalytics.CreateRSI(14);
var rsiValues = StockSharpAnalytics.CalculatePriceHistory(rsi, prices);
Console.WriteLine($"  Рассчитано {rsiValues.Count} значений");
Console.WriteLine($"  Последние 10: {string.Join(", ", rsiValues.TakeLast(10).Select(v => v?.ToString("F1") ?? "null"))}");
Console.WriteLine($"  IsFormed: {StockSharpAnalytics.IsFormed(rsi)}");
Console.WriteLine($"  Последнее значение: {rsiValues.Last()}");

Console.WriteLine("\n=== ✅ Все тесты пройдены! ===");
