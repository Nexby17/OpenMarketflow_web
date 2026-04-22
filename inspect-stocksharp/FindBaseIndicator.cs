using System.Reflection;

var algoAssembly = Assembly.LoadFrom(Path.Combine(
    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
    ".nuget", "packages", "stocksharp.algo", "5.0.222", "lib", "net6.0", "StockSharp.Algo.dll"));

var baseIndicator = algoAssembly.GetTypes()
    .FirstOrDefault(t => t.Name == "BaseIndicator");

if (baseIndicator != null)
{
    Console.WriteLine($"BaseIndicator найден:");
    Console.WriteLine($"  FullName: {baseIndicator.FullName}");
    Console.WriteLine($"  Namespace: {baseIndicator.Namespace}");
    Console.WriteLine($"  IsPublic: {baseIndicator.IsPublic}");
    Console.WriteLine($"  IsAbstract: {baseIndicator.IsAbstract}");
}
else
{
    Console.WriteLine("BaseIndicator НЕ найден!");

    // Ищем все типы с "Indicator" в имени
    var allIndicators = algoAssembly.GetTypes()
        .Where(t => t.Name.Contains("Indicator"))
        .OrderBy(t => t.Name)
        .ToList();

    Console.WriteLine($"\nВсе типы с 'Indicator' в имени ({allIndicators.Count}):");
    foreach (var t in allIndicators.Take(30))
    {
        Console.WriteLine($"  {t.FullName}");
    }
}
