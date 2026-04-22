using System.Reflection;

public class InspectAll
{
    public static void Run()
    {
        var assembly = Assembly.LoadFrom("bin/Debug/net8.0/StockSharp.Finam.dll");
        var types = assembly.GetTypes().OrderBy(t => t.Name).ToList();

        Console.WriteLine($"=== Все типы в StockSharp.Finam ({types.Count}) ===\n");

        foreach (var type in types)
        {
            Console.WriteLine($"📦 {type.Name} - {type.Namespace}");
            Console.WriteLine($"   IsPublic: {type.IsPublic}, IsAbstract: {type.IsAbstract}, BaseType: {type.BaseType?.Name ?? "null"}");

            // Проверяем на наличие свойств Token/Password/Key/Secret
            var props = type.GetProperties(BindingFlags.Public | BindingFlags.Instance);
            var authProps = props.Where(p =>
                p.Name.Contains("Token", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Password", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Key", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Secret", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Auth", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Login", StringComparison.OrdinalIgnoreCase)
            ).ToList();

            if (authProps.Any())
            {
                Console.WriteLine($"   🔑 Свойства аутентификации:");
                foreach (var prop in authProps)
                {
                    Console.WriteLine($"      {prop.PropertyType.Name} {prop.Name}");
                }
            }

            // Конструкторы
            var ctors = type.GetConstructors();
            if (ctors.Length > 0)
            {
                Console.WriteLine($"   🔨 Конструкторы: {ctors.Length}");
                foreach (var ctor in ctors)
                {
                    var parameters = ctor.GetParameters();
                    var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
                    Console.WriteLine($"      {type.Name}({paramStr})");
                }
            }

            Console.WriteLine();
        }
    }
}
