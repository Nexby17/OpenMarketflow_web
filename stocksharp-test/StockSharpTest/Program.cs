using System;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;

using StockSharp.Algo;
using StockSharp.Finam;
using StockSharp.Messages;

namespace StockSharpTest;

class Program
{
    static async Task Main(string[] args)
    {
        var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        
        // Шаг 1: Изучаем API через reflection
        Console.WriteLine("=== FinamMessageAdapter API ===");
        var adapterType = typeof(FinamMessageAdapter);
        
        Console.WriteLine("\n📋 Properties:");
        foreach (var prop in adapterType.GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .OrderBy(p => p.Name))
        {
            Console.WriteLine($"  {prop.PropertyType.Name} {prop.Name} {{ {(prop.CanRead ? "get; " : "")}{(prop.CanWrite ? "set; " : "")}}}");
        }

        Console.WriteLine("\n📋 Constructors:");
        foreach (var ctor in adapterType.GetConstructors())
        {
            var parms = string.Join(", ", ctor.GetParameters().Select(p => $"{p.ParameterType.Name} {p.Name}"));
            Console.WriteLine($"  FinamMessageAdapter({parms})");
        }

        // Шаг 2: Изучаем Connector API
        Console.WriteLine("\n=== Connector subscription methods ===");
        var connType = typeof(Connector);
        foreach (var method in connType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
            .Where(m => m.Name.Contains("Subscribe") || m.Name.Contains("Lookup"))
            .OrderBy(m => m.Name))
        {
            var parms = string.Join(", ", method.GetParameters().Select(p => $"{p.ParameterType.Name} {p.Name}"));
            Console.WriteLine($"  {method.ReturnType.Name} {method.Name}({parms})");
        }

        Console.WriteLine("\n=== Connector events (Subscription*) ===");
        foreach (var evt in connType.GetEvents(BindingFlags.Public | BindingFlags.Instance)
            .Where(e => e.Name.Contains("Subscription") || e.Name.Contains("Security") || e.Name.Contains("Candle") || e.Name.Contains("Connect"))
            .OrderBy(e => e.Name))
        {
            Console.WriteLine($"  event {evt.EventHandlerType?.Name} {evt.Name}");
        }

        if (string.IsNullOrEmpty(token))
        {
            Console.WriteLine("\n⚠️ FINAM_TOKEN не задан — пропускаем подключение");
            Console.WriteLine("✅ API exploration завершён!");
            return;
        }

        Console.WriteLine($"\n✅ Токен получен (длина: {token.Length})");

        // Шаг 3: Пробуем подключиться
        Console.WriteLine("\n=== Попытка подключения ===");
        
        var connector = new Connector();
        
        // Пробуем найти правильное свойство для токена
        connector.AddAdapter<FinamMessageAdapter>(a =>
        {
            // Ищем свойство для токена
            var tokenProp = adapterType.GetProperties()
                .FirstOrDefault(p => p.Name.Contains("Token", StringComparison.OrdinalIgnoreCase) 
                    || p.Name.Contains("Key", StringComparison.OrdinalIgnoreCase)
                    || p.Name.Contains("Secret", StringComparison.OrdinalIgnoreCase));
            
            if (tokenProp != null)
            {
                Console.WriteLine($"  Найдено свойство для токена: {tokenProp.Name} ({tokenProp.PropertyType.Name})");
                try
                {
                    if (tokenProp.PropertyType == typeof(System.Security.SecureString))
                    {
                        var ss = new System.Security.SecureString();
                        foreach (var c in token) ss.AppendChar(c);
                        tokenProp.SetValue(a, ss);
                    }
                    else if (tokenProp.PropertyType == typeof(string))
                    {
                        tokenProp.SetValue(a, token);
                    }
                    else
                    {
                        Console.WriteLine($"  ⚠️ Неизвестный тип: {tokenProp.PropertyType.FullName}");
                        // Попробуем через ToString конверсию
                        Console.WriteLine($"  Пробуем установить как строку...");
                    }
                    Console.WriteLine($"  ✅ Токен установлен через {tokenProp.Name}");
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"  ❌ Ошибка установки: {ex.Message}");
                }
            }
            else
            {
                Console.WriteLine("  ❌ Свойство для токена не найдено!");
                Console.WriteLine("  Все свойства с set:");
                foreach (var p in adapterType.GetProperties().Where(p => p.CanWrite))
                {
                    Console.WriteLine($"    {p.PropertyType.Name} {p.Name}");
                }
            }
        });

        var connected = new ManualResetEventSlim(false);

        connector.Connected += () =>
        {
            Console.WriteLine("✅ Подключён к Финам через StockSharp!");
            connected.Set();
        };

        connector.ConnectionError += error =>
        {
            Console.WriteLine($"❌ Ошибка подключения: {error}");
            connected.Set();
        };

        connector.Error += error =>
        {
            Console.WriteLine($"⚠️ Ошибка: {error.Message}");
        };

        var securitiesList = new List<StockSharp.BusinessEntities.Security>();
        connector.SecurityReceived += (sub, security) =>
        {
            securitiesList.Add(security);
            if (securitiesList.Count <= 10 || securitiesList.Count % 200 == 0)
            {
                Console.WriteLine($"  📊 [{securitiesList.Count}] {security.Id} | {security.Code} | {security.Name} | Тип: {security.Type}");
            }
        };

        connector.SubscriptionOnline += sub =>
        {
            Console.WriteLine($"📡 Subscription online: {sub.DataType}");
        };

        connector.SubscriptionFailed += (sub, error, isSubscribe) =>
        {
            Console.WriteLine($"⚠️ Subscription failed: {error.Message} (subscribe={isSubscribe})");
        };

        Console.WriteLine("🔌 Подключаемся...");
        connector.Connect();

        if (!connected.Wait(TimeSpan.FromSeconds(30)))
        {
            Console.WriteLine("❌ Таймаут подключения");
        }

        // Ждём инструменты
        await Task.Delay(10000);
        Console.WriteLine($"\n📋 Получено инструментов: {securitiesList.Count}");
        
        // Ищем Si
        var targets = new[] { "SBER", "GAZP", "Si" };
        foreach (var code in targets)
        {
            var found = securitiesList.Where(s => 
                s.Code?.Contains(code, StringComparison.OrdinalIgnoreCase) == true)
                .Take(3).ToList();
            
            if (found.Any())
            {
                foreach (var s in found)
                    Console.WriteLine($"  ✅ {s.Id} | {s.Code} | {s.Name} | Тип: {s.Type}");
            }
            else
            {
                Console.WriteLine($"  ❌ {code} — не найден");
            }
        }

        Console.WriteLine("\n🔌 Отключаемся...");
        connector.Disconnect();
        await Task.Delay(2000);
        Console.WriteLine("✅ Тест завершён!");
    }
}
