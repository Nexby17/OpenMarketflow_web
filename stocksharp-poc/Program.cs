// PoC тест: подключение StockSharp.Finam к реальному брокеру
using HedgeFund.Brokers.StockSharp;

Console.WriteLine("=== StockSharp Finam PoC ===\n");

// Читаем токен из .env или переменной окружения
var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
if (string.IsNullOrEmpty(token))
{
    // Пробуем прочитать из файла .env в родительской директории
    var envPath = Path.Combine("..", ".env");
    if (File.Exists(envPath))
    {
        foreach (var line in File.ReadAllLines(envPath))
        {
            if (line.StartsWith("FINAM_TOKEN="))
            {
                token = line.Substring("FINAM_TOKEN=".Length);
                break;
            }
        }
    }
}

if (string.IsNullOrEmpty(token))
{
    Console.WriteLine("❌ FINAM_TOKEN не найден!");
    Console.WriteLine("   Установите переменную окружения или создайте .env файл с FINAM_TOKEN");
    return;
}

Console.WriteLine($"✅ Token loaded (length: {token.Length})\n");

// Создаём PoC
using var poc = new StockSharpPoC(token);

// Тест 1: Подключение
Console.WriteLine("═══════════════════════════════════════");
Console.WriteLine("Test 1: Connection");
Console.WriteLine("═══════════════════════════════════════");

var connected = await poc.ConnectAsync();
if (!connected)
{
    Console.WriteLine("\n❌ Connection failed. Aborting.");
    return;
}

// Небольшая пауза для полной инициализации
await Task.Delay(1000);

// Тест 2: Получение списка инструментов
Console.WriteLine("\n═══════════════════════════════════════");
Console.WriteLine("Test 2: Get Instruments");
Console.WriteLine("═══════════════════════════════════════");

await poc.TestGetInstrumentsAsync();

// Тест 3: Подписка на свечи
Console.WriteLine("\n═══════════════════════════════════════");
Console.WriteLine("Test 3: Subscribe to Candles");
Console.WriteLine("═══════════════════════════════════════");

await poc.TestCandlesAsync();

// Отключение
Console.WriteLine("\n═══════════════════════════════════════");
Console.WriteLine("Cleanup");
Console.WriteLine("═══════════════════════════════════════");

await poc.DisconnectAsync();
await Task.Delay(1000);

Console.WriteLine("\n=== ✅ PoC completed! ===");
