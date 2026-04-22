using StockSharp.Algo.Indicators;

Console.WriteLine("Поиск MACD...");

var types = typeof(ParabolicSar).Assembly.GetTypes()
    .Where(t => t.Name.Contains("Macd", StringComparison.OrdinalIgnoreCase) ||
                t.Name.Contains("MACD", StringComparison.OrdinalIgnoreCase))
    .ToList();

Console.WriteLine($"Найдено: {types.Count}");
foreach (var t in types)
{
    Console.WriteLine($"  {t.FullName}");
}
