// Инспекция StockSharp.Finam.dll - поиск public API
using System.Reflection;

var assemblyPath = args.Length > 0
    ? args[0]
    : Path.Combine(Environment.CurrentDirectory, "bin", "Debug", "net8.0", "StockSharp.Finam.dll");

if (!File.Exists(assemblyPath))
{
    // Пробуем найти в NuGet cache
    var nugetPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".nuget", "packages", "stocksharp.finam", "5.0.198", "lib", "net6.0", "StockSharp.Finam.dll");

    if (File.Exists(nugetPath))
    {
        assemblyPath = nugetPath;
    }
    else
    {
        Console.WriteLine($"❌ StockSharp.Finam.dll не найден!");
        Console.WriteLine($"   Пробуем: {assemblyPath}");
        Console.WriteLine($"   NuGet cache: {nugetPath}");
        return;
    }
}

Console.WriteLine($"=== Инспекция: {assemblyPath} ===\n");

var assembly = Assembly.LoadFrom(assemblyPath);
Console.WriteLine($"📦 Assembly: {assembly.FullName}");
Console.WriteLine($"📍 Location: {assembly.Location}\n");

// Находим все public типы
var types = assembly.GetTypes().OrderBy(t => t.Name).ToList();
Console.WriteLine($"📊 Всего типов: {types.Count}\n");

// Ищем FinamMessageAdapter
var adapterType = types.FirstOrDefault(t => t.Name.Contains("Adapter"));
if (adapterType != null)
{
    Console.WriteLine($"=== {adapterType.Name} ===");
    Console.WriteLine($"📝 Namespace: {adapterType.Namespace}");
    Console.WriteLine($"🏷️  IsPublic: {adapterType.IsPublic}");
    Console.WriteLine($"🏷️  IsAbstract: {adapterType.IsAbstract}");
    Console.WriteLine($"🏷️  BaseType: {adapterType.BaseType?.Name ?? "null"}\n");

    // Конструкторы
    var constructors = adapterType.GetConstructors();
    Console.WriteLine($"🔨 Конструкторы ({constructors.Length}):");
    foreach (var ctor in constructors)
    {
        var parameters = ctor.GetParameters();
        var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
        Console.WriteLine($"   {adapterType.Name}({paramStr})");
    }
    Console.WriteLine();

    // Свойства
    var properties = adapterType.GetProperties(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
    Console.WriteLine($"📋 Свойства ({properties.Length}):");
    foreach (var prop in properties.OrderBy(p => p.Name))
    {
        Console.WriteLine($"   {prop.PropertyType.Name} {prop.Name} {{ {(prop.GetMethod?.IsPublic == true ? "get;" : "")}{(prop.SetMethod?.IsPublic == true ? " set;" : "")} }}");
    }
    Console.WriteLine();

    // Методы
    var methods = adapterType.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly)
        .Where(m => !m.IsSpecialName).ToList();
    Console.WriteLine($"🔧 Методы ({methods.Count}):");
    foreach (var method in methods.OrderBy(m => m.Name))
    {
        var parameters = method.GetParameters();
        var paramStr = string.Join(", ", parameters.Select(p => $"{p.ParameterType.Name} {p.Name}"));
        Console.WriteLine($"   {method.ReturnType.Name} {method.Name}({paramStr})");
    }
}

// Ищем другие интересные типы
var messageAdapters = types.Where(t => t.Name.EndsWith("Adapter")).ToList();
if (messageAdapters.Count > 1)
{
    Console.WriteLine($"\n=== Другие адаптеры ({messageAdapters.Count - 1}) ===");
    foreach (var t in messageAdapters.Where(t => t != adapterType))
    {
        Console.WriteLine($"   {t.Name}");
    }
}

// Ищем классы с "Finam" в названии
var finamTypes = types.Where(t => t.Name.Contains("Finam")).ToList();
if (finamTypes.Count > 0)
{
    Console.WriteLine($"\n=== Finam типы ({finamTypes.Count}) ===");
    foreach (var t in finamTypes)
    {
        Console.WriteLine($"   {t.Name} - {t.Namespace}");
    }
}

Console.WriteLine("\n=== ✅ Инспекция завершена! ===");

// Запускаем инспекцию базовых классов
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectBase.Run();

// Инспекция ConnectMessage
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectConnect.Run();

// Инспекция всех типов
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectAll.Run();

// Инспекция индикаторов
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectIndicators.Run();

// Инспекция типов индикаторов
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectIndicatorTypes.Run();

// Инспекция метода Process
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectProcessMethod.Run();

// Инспекция создания IIndicatorValue
Console.WriteLine("\n" + new string('=', 50) + "\n");
InspectIndicatorValueCreate.Run();
