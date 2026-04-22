using System.Reflection;
using StockSharp.Algo.Indicators;

var ema = new ExponentialMovingAverage { Length = 14 };

Console.WriteLine($"=== {ema.GetType().Name} ===");
Console.WriteLine($"BaseType: {ema.GetType().BaseType?.Name}");

var props = ema.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
    .OrderBy(p => p.Name)
    .ToList();

Console.WriteLine($"\nСвойства ({props.Count}):");
foreach (var prop in props)
{
    try
    {
        var value = prop.GetValue(ema);
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = {value}");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = [Error: {ex.Message}]");
    }
}

// Проверяем методы
var methods = ema.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance)
    .Where(m => !m.IsSpecialName)
    .OrderBy(m => m.Name)
    .ToList();

Console.WriteLine($"\nМетоды ({methods.Count}):");
foreach (var method in methods.Take(20))
{
    var parameters = method.GetParameters();
    var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
    Console.WriteLine($"  {method.ReturnType.Name,-30} {method.Name}({paramStr})");
}
