using System.Reflection;

public class InspectBase
{
    public static void Run()
    {
        var assembly = Assembly.LoadFrom("bin/Debug/net8.0/StockSharp.Finam.dll");

        // Находим FinamMessageAdapter
        var finamType = assembly.GetTypes().FirstOrDefault(t => t.Name == "FinamMessageAdapter");
        if (finamType == null)
        {
            Console.WriteLine("❌ FinamMessageAdapter не найден!");
            return;
        }

        Console.WriteLine("=== Базовая иерархия ===\n");
        var current = finamType;
        while (current != null)
        {
            Console.WriteLine($"{current.Name}");
            Console.WriteLine($"  Properties: {current.GetProperties(BindingFlags.Public | BindingFlags.Instance).Length}");
            Console.WriteLine($"  Methods: {current.GetMethods(BindingFlags.Public | BindingFlags.Instance).Length}");
            current = current.BaseType;
        }

        Console.WriteLine("\n=== Свойства FinamMessageAdapter (включая наследованные) ===\n");
        var allProps = finamType.GetProperties(BindingFlags.Public | BindingFlags.Instance);
        foreach (var prop in allProps.OrderBy(p => p.Name))
        {
            var declaring = prop.DeclaringType?.Name ?? "";
            var inherited = declaring != "FinamMessageAdapter" ? $" [{declaring}]" : "";
            Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name}{inherited}");
        }

        Console.WriteLine("\n=== Поиск свойств с 'Token', 'Key', 'Secret', 'Password' ===\n");
        var tokenProps = allProps.Where(p =>
            p.Name.Contains("Token", StringComparison.OrdinalIgnoreCase) ||
            p.Name.Contains("Key", StringComparison.OrdinalIgnoreCase) ||
            p.Name.Contains("Secret", StringComparison.OrdinalIgnoreCase) ||
            p.Name.Contains("Password", StringComparison.OrdinalIgnoreCase) ||
            p.Name.Contains("Auth", StringComparison.OrdinalIgnoreCase)
        );

        foreach (var prop in tokenProps)
        {
            Console.WriteLine($"  {prop.PropertyType.Name} {prop.Name} [declaring: {prop.DeclaringType?.Name}]");
        }

        Console.WriteLine("\n=== Поиск методов с 'Connect', 'Auth', 'Login' ===\n");
        var allMethods = finamType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
        var authMethods = allMethods.Where(m =>
            m.Name.Contains("Connect", StringComparison.OrdinalIgnoreCase) ||
            m.Name.Contains("Auth", StringComparison.OrdinalIgnoreCase) ||
            m.Name.Contains("Login", StringComparison.OrdinalIgnoreCase)
        );

        foreach (var method in authMethods)
        {
            var parameters = method.GetParameters();
            var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
            Console.WriteLine($"  {method.ReturnType.Name} {method.Name}({paramStr}) [declaring: {method.DeclaringType?.Name}]");
        }

        Console.WriteLine("\n=== SettingsStorage - что это? ===\n");
        var settingsType = allProps.FirstOrDefault(p => p.PropertyType.Name.Contains("Settings"));
        if (settingsType != null)
        {
            Console.WriteLine($"  {settingsType.PropertyType.FullName} {settingsType.Name}");
        }

        // Проверяем есть ли у базового класса свойства для аутентификации
        var baseType = finamType.BaseType;
        if (baseType != null)
        {
            Console.WriteLine($"\n=== Свойства базового класса {baseType.Name} ===\n");
            var baseProps = baseType.GetProperties(BindingFlags.Public | BindingFlags.Instance);
            var authProps = baseProps.Where(p =>
                p.Name.Contains("Token", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Key", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Secret", StringComparison.OrdinalIgnoreCase) ||
                p.Name.Contains("Password", StringComparison.OrdinalIgnoreCase)
            );

            if (authProps.Any())
            {
                foreach (var prop in authProps)
                {
                    Console.WriteLine($"  {prop.PropertyType.Name} {prop.Name}");
                }
            }
            else
            {
                Console.WriteLine("  Свойств для аутентификации не найдено");
            }
        }
    }
}
