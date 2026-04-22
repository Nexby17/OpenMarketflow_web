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

// Пробуем Process
var indicatorValue = ema.CreateValue(DateTimeOffset.Now, new object[] { 100.0m });
Console.WriteLine($"\nСоздано значение: {indicatorValue.GetType().Name}");

var result = ema.Process(indicatorValue);
Console.WriteLine($"Process вернул: {result?.GetType().Name}");

if (result != null)
{
    var resultProps = result.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .OrderBy(p => p.Name)
        .ToList();

    Console.WriteLine($"\nСвойства результата ({resultProps.Count}):");
    foreach (var prop in resultProps)
    {
        try
        {
            var propValue = prop.GetValue(result);
            Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = {propValue}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name} = [Error: {ex.Message}]");
        }
    }

    // Пробуем GetValue
    var methods = result.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance)
        .Where(m => m.Name == "GetValue")
        .ToList();

    Console.WriteLine($"\nМетоды GetValue ({methods.Count}):");
    foreach (var method in methods)
    {
        var parameters = method.GetParameters();
        var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
        Console.WriteLine($"  {method.ReturnType.Name} GetValue({paramStr})");
    }

    if (methods.Any())
    {
        try
        {
            // Пробуем вызвать GetValue без параметров
            var getValueNoParams = methods.FirstOrDefault(m => !m.GetParameters().Any());
            if (getValueNoParams != null)
            {
                var val = getValueNoParams.Invoke(result, null);
                Console.WriteLine($"\nGetValue() = {val}");
            }

            // Пробуем вызвать с null
            var getValueWithNull = methods.FirstOrDefault(m => m.GetParameters().Any());
            if (getValueWithNull != null)
            {
                var val = getValueWithNull.Invoke(result, new object[] { null });
                Console.WriteLine($"GetValue(null) = {val}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"\nОшибка вызова GetValue: {ex.Message}");
        }
    }
}

// Подаём ещё значений
Console.WriteLine($"\nПодаём ещё значений...");
for (int i = 1; i <= 20; i++)
{
    var price = 100.0m + (i % 5) * 2.0m;
    var val = ema.CreateValue(DateTimeOffset.Now.AddMinutes(i), new object[] { price });
    var res = ema.Process(val);
    Console.WriteLine($"Цена {price:F2}, IsFormed: {ema.IsFormed}, Результат: {res}");
}
