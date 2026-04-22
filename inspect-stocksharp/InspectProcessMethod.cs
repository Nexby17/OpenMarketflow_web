using System.Reflection;

public class InspectProcessMethod
{
    public static void Run()
    {
        var algoAssembly = Assembly.LoadFrom(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".nuget", "packages", "stocksharp.algo", "5.0.222", "lib", "net6.0", "StockSharp.Algo.dll"));

        Console.WriteLine("=== Метод Process в BaseIndicator ===\n");

        var baseIndicator = algoAssembly.GetTypes()
            .FirstOrDefault(t => t.Name == "BaseIndicator");

        if (baseIndicator != null)
        {
            var processMethod = baseIndicator.GetMethods()
                .FirstOrDefault(m => m.Name == "Process");

            if (processMethod != null)
            {
                Console.WriteLine($"🔧 Process method:");
                Console.WriteLine($"   Return: {processMethod.ReturnType.FullName}");
                Console.WriteLine($"   Parameters:");
                foreach (var param in processMethod.GetParameters())
                {
                    Console.WriteLine($"      {param.ParameterType.FullName} {param.Name}");
                }

                // Ищем тип параметра
                var paramType = processMethod.GetParameters().FirstOrDefault()?.ParameterType;
                if (paramType != null)
                {
                    Console.WriteLine($"\n=== Тип параметра: {paramType.Name} ===\n");

                    // Его методы
                    var methods = paramType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
                        .Where(m => !m.IsSpecialName)
                        .OrderBy(m => m.Name)
                        .ToList();

                    Console.WriteLine($"🔧 Методы ({methods.Count}):");
                    foreach (var m in methods.Take(20))
                    {
                        var parameters = m.GetParameters();
                        var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                        Console.WriteLine($"   {m.ReturnType.Name} {m.Name}({paramStr})");
                    }

                    // Его свойства
                    var props = paramType.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                        .OrderBy(p => p.Name)
                        .ToList();

                    Console.WriteLine($"\n📋 Свойства ({props.Count}):");
                    foreach (var p in props.Take(20))
                    {
                        Console.WriteLine($"   {p.PropertyType.Name} {p.Name}");
                    }

                    // Ищем фабричные методы для создания значений
                    Console.WriteLine($"\n=== Поиск фабричных методов для создания значений ===\n");
                    var valueFactories = algoAssembly.GetTypes()
                        .Where(t => t.GetMethods().Any(m =>
                            m.ReturnType == paramType &&
                            m.IsStatic &&
                            (m.Name.Contains("Create") || m.Name.Contains("New"))))
                        .SelectMany(t => t.GetMethods(BindingFlags.Public | BindingFlags.Static)
                            .Where(m => m.ReturnType == paramType))
                        .Take(10)
                        .ToList();

                    if (valueFactories.Any())
                    {
                        Console.WriteLine($"🏭 Фабричные методы ({valueFactories.Count}):");
                        foreach (var m in valueFactories)
                        {
                            var parameters = m.GetParameters();
                            var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                            Console.WriteLine($"   {m.DeclaringType?.Name}.{m.Name}({paramStr})");
                        }
                    }
                    else
                    {
                        Console.WriteLine("❌ Фабричные методы не найдены");

                        // Пробуем найти конструкторы типа параметра
                        var ctors = paramType.GetConstructors();
                        if (ctors.Any())
                        {
                            Console.WriteLine($"\n🔨 Конструкторы ({ctors.Length}):");
                            foreach (var ctor in ctors)
                            {
                                var parameters = ctor.GetParameters();
                                var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                                Console.WriteLine($"   {paramType.Name}({paramStr})");
                            }
                        }
                    }
                }
            }
        }
    }
}
