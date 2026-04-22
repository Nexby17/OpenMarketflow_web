using System.Reflection;

public class InspectConnect
{
    public static void Run()
    {
        var assembly = Assembly.LoadFrom("bin/Debug/net8.0/StockSharp.Finam.dll");

        // Ищем ConnectMessage
        var connectType = assembly.GetTypes()
            .FirstOrDefault(t => t.Name.Contains("Connect") && t.Name.EndsWith("Message"));

        if (connectType == null)
        {
            // Пробуем искать в StockSharp.Messages (если загружен)
            var messagesAssembly = AppDomain.CurrentDomain.GetAssemblies()
                .FirstOrDefault(a => a.GetName().Name == "StockSharp.Messages");

            if (messagesAssembly != null)
            {
                connectType = messagesAssembly.GetTypes()
                    .FirstOrDefault(t => t.Name == "ConnectMessage");
            }
        }

        if (connectType == null)
        {
            Console.WriteLine("❌ ConnectMessage не найден!");
            return;
        }

        Console.WriteLine($"=== {connectType.FullName} ===\n");

        // Свойства
        var props = connectType.GetProperties(BindingFlags.Public | BindingFlags.Instance);
        Console.WriteLine($"📋 Свойства ({props.Length}):");
        foreach (var prop in props.OrderBy(p => p.Name))
        {
            Console.WriteLine($"  {prop.PropertyType.Name,-30} {prop.Name}");
        }

        // Конструкторы
        var ctors = connectType.GetConstructors();
        Console.WriteLine($"\n🔨 Конструкторы ({ctors.Length}):");
        foreach (var ctor in ctors)
        {
            var parameters = ctor.GetParameters();
            var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
            Console.WriteLine($"  {connectType.Name}({paramStr})");
        }

        // Поля
        var fields = connectType.GetFields(BindingFlags.Public | BindingFlags.Instance);
        if (fields.Any())
        {
            Console.WriteLine($"\n📦 Поля ({fields.Length}):");
            foreach (var field in fields.OrderBy(f => f.Name))
            {
                Console.WriteLine($"  {field.FieldType.Name,-30} {field.Name}");
            }
        }
    }
}
