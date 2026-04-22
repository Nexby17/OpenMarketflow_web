using System.Reflection;
using StockSharp.Algo.Indicators;

Console.WriteLine("=== PSAR свойства ===");
var psar = new ParabolicSar();
var psarProps = psar.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
    .OrderBy(p => p.Name)
    .ToList();

foreach (var prop in psarProps)
{
    try
    {
        var value = prop.GetValue(psar);
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = {value}");
    }
    catch
    {
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = [error]");
    }
}

Console.WriteLine("\n=== MACD свойства ===");
var macd = new MACD();
var macdProps = macd.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
    .OrderBy(p => p.Name)
    .ToList();

foreach (var prop in macdProps)
{
    try
    {
        var value = prop.GetValue(macd);
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = {value}");
    }
    catch
    {
        Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = [error]");
    }
}
