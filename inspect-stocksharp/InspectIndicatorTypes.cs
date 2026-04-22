using System.Reflection;

public class InspectIndicatorTypes
{
    public static void Run()
    {
        var algoAssembly = Assembly.LoadFrom(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".nuget", "packages", "stocksharp.algo", "5.0.222", "lib", "net6.0", "StockSharp.Algo.dll"));

        Console.WriteLine("=== Типы связанные с индикаторами ===\n");

        // Ищем все интерфейсы с "Indicator" в названии
        var indicatorInterfaces = algoAssembly.GetTypes()
            .Where(t => t.IsInterface && t.Name.Contains("Indicator"))
            .OrderBy(t => t.Name)
            .ToList();

        Console.WriteLine($"📋 Интерфейсы индикаторов ({indicatorInterfaces.Count}):");
        foreach (var iface in indicatorInterfaces)
        {
            Console.WriteLine($"   {iface.Name}");
            var methods = iface.GetMethods();
            if (methods.Any())
            {
                Console.WriteLine($"      Methods: {methods.Length}");
                foreach (var m in methods.Take(3))
                {
                    var parameters = m.GetParameters();
                    var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                    Console.WriteLine($"         {m.ReturnType.Name} {m.Name}({paramStr})");
                }
            }
        }

        // Ищем классы с "Value" в названии для IIndicatorValue
        Console.WriteLine($"\n=== Типы для значений индикаторов ===\n");
        var valueTypes = algoAssembly.GetTypes()
            .Where(t => t.Name.Contains("Value") && !t.IsInterface && !t.IsAbstract)
            .OrderBy(t => t.Name)
            .ToList();

        Console.WriteLine($"📋 Типы значений ({valueTypes.Count}):");
        foreach (var type in valueTypes.Take(20))
        {
            Console.WriteLine($"   {type.Name} - {type.Namespace}");
        }

        // Ищем конкретный класс для свечей
        Console.WriteLine($"\n=== Поиск классов для свечей ===\n");
        var candleValueTypes = algoAssembly.GetTypes()
            .Where(t => t.Name.Contains("Candle") && t.Name.Contains("Value"))
            .ToList();

        if (candleValueTypes.Any())
        {
            foreach (var t in candleValueTypes)
            {
                Console.WriteLine($"✅ {t.Name} - {t.Namespace}");
            }
        }
        else
        {
            Console.WriteLine("❌ Не найдены классы для свечей");

            // Ищем методы для создания значений из свечей
            var candleTypes = algoAssembly.GetTypes().Where(t => t.Name.Contains("Candle")).ToList();
            Console.WriteLine($"\n🕯️  Типы свечей ({candleTypes.Count}):");
            foreach (var t in candleTypes.Take(10))
            {
                Console.WriteLine($"   {t.Name} - {t.Namespace}");
            }
        }
    }
}
