using System.Reflection;

public class InspectIndicatorValueCreate
{
    public static void Run()
    {
        var algoAssembly = Assembly.LoadFrom(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".nuget", "packages", "stocksharp.algo", "5.0.222", "lib", "net6.0", "StockSharp.Algo.dll"));

        Console.WriteLine("=== Поиск способов создания IIndicatorValue ===\n");

        // 1. Ищем классы которые реализуют IIndicatorValue
        var iIndicatorValue = algoAssembly.GetTypes()
            .FirstOrDefault(t => t.IsInterface && t.Name == "IIndicatorValue");

        if (iIndicatorValue == null)
        {
            Console.WriteLine("❌ IIndicatorValue не найден как интерфейс");
            // Пробуем найти как тип
            iIndicatorValue = algoAssembly.GetTypes()
                .FirstOrDefault(t => t.Name == "IIndicatorValue");
        }

        if (iIndicatorValue != null)
        {
            var implementations = algoAssembly.GetTypes()
                .Where(t => !t.IsInterface && !t.IsAbstract && iIndicatorValue.IsAssignableFrom(t))
                .OrderBy(t => t.Name)
                .ToList();

            Console.WriteLine($"📋 Реализации IIndicatorValue ({implementations.Count}):");
            foreach (var impl in implementations.Take(20))
            {
                Console.WriteLine($"   {impl.Name} - {impl.Namespace}");

                // Конструкторы
                var ctors = impl.GetConstructors(BindingFlags.Public | BindingFlags.Instance);
                if (ctors.Any())
                {
                    Console.WriteLine($"      Конструкторы: {ctors.Length}");
                    foreach (var ctor in ctors.Take(3))
                    {
                        var parameters = ctor.GetParameters();
                        var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                        Console.WriteLine($"         {impl.Name}({paramStr})");
                    }
                }
            }
        }

        // 2. Ищем в IndicatorValue
        Console.WriteLine($"\n=== Тип IndicatorValue ===\n");
        var indicatorValueType = algoAssembly.GetTypes()
            .FirstOrDefault(t => t.Name == "IndicatorValue");

        if (indicatorValueType != null)
        {
            Console.WriteLine($"✅ {indicatorValueType.FullName}");

            // Конструкторы
            var ctors = indicatorValueType.GetConstructors(BindingFlags.Public | BindingFlags.Instance);
            Console.WriteLine($"🔨 Конструкторы ({ctors.Length}):");
            foreach (var ctor in ctors)
            {
                var parameters = ctor.GetParameters();
                var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                Console.WriteLine($"   IndicatorValue({paramStr})");
            }

            // Статические методы
            var staticMethods = indicatorValueType.GetMethods(BindingFlags.Public | BindingFlags.Static)
                .Where(m => !m.IsSpecialName)
                .OrderBy(m => m.Name)
                .ToList();

            if (staticMethods.Any())
            {
                Console.WriteLine($"\n🔧 Статические методы ({staticMethods.Count}):");
                foreach (var m in staticMethods)
                {
                    var parameters = m.GetParameters();
                    var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                    Console.WriteLine($"   {m.ReturnType.Name} {m.Name}({paramStr})");
                }
            }
        }

        // 3. Ищем методы в IndicatorContainer или BaseIndicator для создания значений
        Console.WriteLine($"\n=== Поиск методов CreateValue ===\n");
        var allTypes = algoAssembly.GetTypes();
        var createValueMethods = allTypes
            .SelectMany(t => t.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static)
                .Where(m =>
                    (m.Name == "CreateValue" || m.Name == "NewValue") &&
                    m.ReturnType.Name.Contains("Indicator")))
            .Take(20)
            .ToList();

        if (createValueMethods.Any())
        {
            Console.WriteLine($"🏭 Методы создания значений ({createValueMethods.Count}):");
            foreach (var m in createValueMethods)
            {
                var parameters = m.GetParameters();
                var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                var declaring = m.DeclaringType?.Name ?? "";
                var isStatic = m.IsStatic ? "static " : "";
                Console.WriteLine($"   {isStatic}{m.ReturnType.Name} {declaring}.{m.Name}({paramStr})");
            }
        }
        else
        {
            Console.WriteLine("❌ Методы CreateValue не найдены");
        }

        // 4. Ищем DecimalIndicatorValue
        Console.WriteLine($"\n=== Поиск DecimalIndicatorValue ===\n");
        var decimalIndicatorValue = algoAssembly.GetTypes()
            .FirstOrDefault(t => t.Name == "DecimalIndicatorValue");

        if (decimalIndicatorValue != null)
        {
            Console.WriteLine($"✅ {decimalIndicatorValue.FullName}");

            var ctors = decimalIndicatorValue.GetConstructors(BindingFlags.Public | BindingFlags.Instance);
            Console.WriteLine($"🔨 Конструкторы ({ctors.Length}):");
            foreach (var ctor in ctors)
            {
                var parameters = ctor.GetParameters();
                var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                Console.WriteLine($"   DecimalIndicatorValue({paramStr})");
            }
        }
    }
}
