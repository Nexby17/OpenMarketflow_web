using System.Reflection;

public class InspectIndicators
{
    public static void Run()
    {
        // Загружаем StockSharp.Algo
        var algoAssembly = AppDomain.CurrentDomain.GetAssemblies()
            .FirstOrDefault(a => a.GetName().Name == "StockSharp.Algo");

        if (algoAssembly == null)
        {
            Console.WriteLine("❌ StockSharp.Algo не загружен! Пробуем загрузить из NuGet cache...");
            
            var nugetPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".nuget", "packages", "stocksharp.algo", "5.0.222", "lib", "net6.0", "StockSharp.Algo.dll");
            
            if (File.Exists(nugetPath))
            {
                algoAssembly = Assembly.LoadFrom(nugetPath);
                Console.WriteLine($"✅ Загружен из: {nugetPath}");
            }
            else
            {
                Console.WriteLine($"❌ Не найден: {nugetPath}");
                return;
            }
        }

        Console.WriteLine($"=== StockSharp.Algo ===\n");

        // Ищем типы индикаторов
        var indicatorTypes = algoAssembly.GetTypes()
            .Where(t => t.Namespace?.Contains("Indicator") == true || t.Name.Contains("Indicator"))
            .OrderBy(t => t.Name)
            .ToList();

        Console.WriteLine($"📊 Индикаторов найдено: {indicatorTypes.Count}\n");

        // Показываем первые 20
        foreach (var type in indicatorTypes.Take(20))
        {
            Console.WriteLine($"   {type.Name}");
        }

        // Ищем базовый интерфейс/класс индикаторов
        var baseIndicatorType = algoAssembly.GetTypes()
            .FirstOrDefault(t => t.Name == "Indicator" || t.Name == "BaseIndicator");

        if (baseIndicatorType != null)
        {
            Console.WriteLine($"\n=== Базовый тип индикаторов: {baseIndicatorType.Name} ===\n");

            var methods = baseIndicatorType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .Where(m => !m.IsSpecialName)
                .OrderBy(m => m.Name)
                .ToList();

            Console.WriteLine($"🔧 Методы ({methods.Count}):");
            foreach (var method in methods)
            {
                var parameters = method.GetParameters();
                var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                Console.WriteLine($"   {method.ReturnType.Name} {method.Name}({paramStr})");
            }

            var props = baseIndicatorType.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                .OrderBy(p => p.Name)
                .ToList();

            Console.WriteLine($"\n📋 Свойства ({props.Count}):");
            foreach (var prop in props)
            {
                Console.WriteLine($"   {prop.PropertyType.Name} {prop.Name}");
            }
        }

        // Ищем конкретные индикаторы
        var targetIndicators = new[] { "ParabolicSar", "ExponentialMovingAverage", "AverageTrueRange", "SimpleMovingAverage" };

        Console.WriteLine($"\n=== Проверка конкретных индикаторов ===\n");
        foreach (var name in targetIndicators)
        {
            var type = algoAssembly.GetTypes()
                .FirstOrDefault(t => t.Name == name);

            if (type != null)
            {
                Console.WriteLine($"✅ {name} найден");
                Console.WriteLine($"   BaseType: {type.BaseType?.Name}");

                // Конструкторы
                var ctors = type.GetConstructors();
                Console.WriteLine($"   Конструкторы: {ctors.Length}");
                foreach (var ctor in ctors)
                {
                    var parameters = ctor.GetParameters();
                    var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                    Console.WriteLine($"      {name}({paramStr})");
                }
            }
            else
            {
                Console.WriteLine($"❌ {name} не найден");
            }
            Console.WriteLine();
        }
    }
}
