using StockSharp.Algo.Indicators;

Console.WriteLine("Тест BaseIndicator");

var ema = new ExponentialMovingAverage { Length = 14 };
Console.WriteLine($"EMA создана: {ema}");

// Пробуем Process
var value = ema.CreateValue(DateTimeOffset.Now, new object[] { 100.0m });
ema.Process(value);

Console.WriteLine($"Значение EMA: {ema.CurrentValue}");
Console.WriteLine($"IsFormed: {ema.IsFormed}");
