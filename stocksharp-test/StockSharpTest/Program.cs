using System;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

using StockSharp.Algo;
using StockSharp.Finam;
using StockSharp.Messages;
using StockSharp.BusinessEntities;

namespace StockSharpTest;

class Program
{
    static async Task Main(string[] args)
    {
        var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (string.IsNullOrEmpty(token))
        {
            Console.WriteLine("❌ FINAM_TOKEN не задан!");
            return;
        }
        Console.WriteLine($"✅ Токен получен (длина: {token.Length})");

        // Создаём коннектор
        var connector = new Connector();
        
        // Добавляем адаптер Finam
        connector.AddAdapter<FinamMessageAdapter>(a =>
        {
            a.Token = token.ToSecureString();
        });

        var connected = new ManualResetEventSlim(false);
        var securitiesReceived = new ManualResetEventSlim(false);
        var securitiesList = new List<Security>();

        // Обработчики событий
        connector.Connected += () =>
        {
            Console.WriteLine("✅ Подключён к Финам через StockSharp!");
            connected.Set();
        };

        connector.ConnectionError += error =>
        {
            Console.WriteLine($"❌ Ошибка подключения: {error}");
            connected.Set(); // чтобы не зависнуть
        };

        connector.Error += error =>
        {
            Console.WriteLine($"⚠️ Ошибка: {error.Message}");
        };

        connector.SecurityReceived += (sub, security) =>
        {
            securitiesList.Add(security);
            // Логируем первые 20 и потом каждый 100-й
            if (securitiesList.Count <= 20 || securitiesList.Count % 100 == 0)
            {
                Console.WriteLine($"  📊 [{securitiesList.Count}] {security.Id} | {security.Code} | {security.Name} | Тип: {security.Type}");
            }
        };

        connector.SubscriptionFailed += (sub, error, isSubscribe) =>
        {
            Console.WriteLine($"⚠️ Subscription failed: {error.Message} (subscribe={isSubscribe})");
        };

        connector.SubscriptionOnline += sub =>
        {
            Console.WriteLine($"📡 Subscription online: {sub.DataType}");
            if (sub.DataType == DataType.Securities)
            {
                securitiesReceived.Set();
            }
        };

        connector.SubscriptionStopped += sub =>
        {
            Console.WriteLine($"🛑 Subscription stopped: {sub.DataType}");
        };

        // Подключаемся
        Console.WriteLine("🔌 Подключаемся к Финам...");
        connector.Connect();

        if (!connected.Wait(TimeSpan.FromSeconds(30)))
        {
            Console.WriteLine("❌ Таймаут подключения (30 сек)");
            return;
        }

        // Ждём инструменты
        Console.WriteLine("\n📋 Загружаем инструменты...");
        
        // Подписываемся на получение инструментов
        connector.SubscribeSecurities(new Security());
        
        // Ждём загрузки
        securitiesReceived.Wait(TimeSpan.FromSeconds(60));
        await Task.Delay(5000); // доп. время на получение
        
        Console.WriteLine($"\n✅ Получено инструментов: {securitiesList.Count}");

        // Ищем интересующие инструменты
        var targets = new[] { "SBER", "GAZP", "Si", "SiM5", "SiH6" };
        Console.WriteLine("\n🔍 Поиск интересующих инструментов:");
        foreach (var code in targets)
        {
            var found = securitiesList.Where(s => 
                s.Code?.Contains(code, StringComparison.OrdinalIgnoreCase) == true)
                .Take(5).ToList();
            
            if (found.Any())
            {
                foreach (var s in found)
                {
                    Console.WriteLine($"  ✅ {s.Id} | {s.Code} | {s.Name} | Тип: {s.Type} | Board: {s.Board?.Code}");
                }
            }
            else
            {
                Console.WriteLine($"  ❌ {code} — не найден");
            }
        }

        // Пробуем получить свечи Si (если нашли)
        var si = securitiesList.FirstOrDefault(s => 
            s.Code?.StartsWith("Si", StringComparison.OrdinalIgnoreCase) == true && 
            s.Type == SecurityTypes.Future);
        
        if (si != null)
        {
            Console.WriteLine($"\n📈 Получаем дневные свечи для {si.Code} ({si.Id})...");
            
            var candlesReceived = new ManualResetEventSlim(false);
            var candleCount = 0;

            connector.CandleReceived += (sub, candle) =>
            {
                candleCount++;
                if (candleCount <= 10)
                {
                    Console.WriteLine($"  🕯️ {candle.OpenTime:yyyy-MM-dd} O:{candle.OpenPrice} H:{candle.HighPrice} L:{candle.LowPrice} C:{candle.ClosePrice} V:{candle.TotalVolume}");
                }
            };

            connector.SubscriptionOnline += sub =>
            {
                if (sub.DataType == DataType.CandleTimeFrame)
                    candlesReceived.Set();
            };

            var from = DateTime.UtcNow.AddDays(-30);
            var to = DateTime.UtcNow;

            connector.SubscribeCandles(si, DataType.CandleTimeFrame, from, to);
            
            candlesReceived.Wait(TimeSpan.FromSeconds(30));
            await Task.Delay(3000);
            
            Console.WriteLine($"  ✅ Получено свечей: {candleCount}");
        }
        else
        {
            Console.WriteLine("\n⚠️ Фьючерс Si не найден, пропускаем свечи");
        }

        // Отключаемся
        Console.WriteLine("\n🔌 Отключаемся...");
        connector.Disconnect();
        await Task.Delay(2000);

        Console.WriteLine("\n✅ Тест завершён!");
    }
}
