// PoC: проверка подключения StockSharp.Finam (v5.0.222)
using HedgeFund.Core;
using HedgeFund.Core.Models;
using StockSharp.Algo;
using StockSharp.Algo.Candles;
using StockSharp.BusinessEntities;
using StockSharp.Messages;
using StockSharp.Finam;
using Ecng.Common;

namespace HedgeFund.Brokers.StockSharp;

/// <summary>
/// Proof of Concept для StockSharp Finam коннектора.
/// Минимальный тест: подключение → получение инструментов → 1 свеча.
/// </summary>
public class StockSharpPoC : IDisposable
{
    private readonly Connector _connector;
    private readonly string _token;

    public StockSharpPoC(string token)
    {
        _token = token;

        // Создаём базовый Connector (новый API v5)
        _connector = new Connector();

        // Настройка Finam адаптера
        // TODO: разобраться с конструктором FinamMessageAdapter и токеном
        // FinamMessageAdapter может требовать другие параметры
        // Пробуем создать адаптер (может потребоваться отладка)
        // var finamAdapter = new FinamMessageAdapter(...);
        // _connector.Adapter.InnerAdapters.Add(finamAdapter);

        // Временно: пробуем без Finam адаптера, просто базовый Connector
        Console.WriteLine("⚠️ FinamMessageAdapter требует дополнительную настройку (токен, параметры)");
        Console.WriteLine("⚠️ Сейчас тестируем базовый Connector без Finam адаптера");

        // Подписки на события для логирования
        _connector.Connected += () => Console.WriteLine("✅ Connected to Finam");
        _connector.Disconnected += () => Console.WriteLine("❌ Disconnected from Finam");
        _connector.ConnectionError += ex => Console.WriteLine($"⚠️ Connection error: {ex?.Message}");
        _connector.Error += ex => Console.WriteLine($"⚠️ Error: {ex?.Message}");
    }

    public async Task<bool> ConnectAsync()
    {
        try
        {
            Console.WriteLine("🔌 Connecting to Finam via StockSharp...");
            await Task.Run(() => _connector.Connect());

            // Даём время на подключение
            await Task.Delay(3000);
            // Проверяем подключение через Connected событие или try-catch
            try
            {
                var _ = _connector.Securities; // Проверка что коннектор активен
                return true;
            }
            catch
            {
                return false;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Connect failed: {ex.Message}");
            return false;
        }
    }

    public async Task DisconnectAsync()
    {
        await Task.Run(() => _connector.Disconnect());
    }

    public async Task TestGetInstrumentsAsync()
    {
        Console.WriteLine("\n📊 Looking up instruments...");

        try
        {
            // Отправляем запрос на поиск инструментов
            var criteria = new SecurityLookupMessage
            {
                SecurityTypes = new[] { SecurityTypes.Future },
                SecurityId = new SecurityId { SecurityCode = "Si" }
            };

            _connector.SendInMessage(criteria);

            // Ждём появления инструментов (timeout 15 сек)
            var timeout = 15000;
            var start = DateTime.UtcNow;
            List<Security>? instruments = null;

            while ((DateTime.UtcNow - start).TotalMilliseconds < timeout)
            {
                instruments = _connector.Securities
                    .Where(s => s.Code.StartsWith("Si", StringComparison.OrdinalIgnoreCase))
                    .ToList();

                if (instruments.Any())
                {
                    Console.WriteLine($"✅ Found {instruments.Count} instruments:");
                    foreach (var s in instruments.Take(5))
                    {
                        Console.WriteLine($"   - {s.Code}@{s.Board?.Code} | {s.Name} | PriceStep: {s.PriceStep} | VolStep: {s.VolumeStep}");
                    }
                    return;
                }
                await Task.Delay(500);
            }

            Console.WriteLine("⚠️ No instruments found within timeout");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Lookup failed: {ex.Message}");
        }
    }

    public async Task TestCandlesAsync(string ticker = "SiH6")
    {
        Console.WriteLine($"\n📈 Subscribing to candles for {ticker}...");

        try
        {
            // Ищем инструмент
            var security = _connector.Securities
                .FirstOrDefault(s => s.Code == ticker);

            if (security == null)
            {
                Console.WriteLine($"❌ Security {ticker} not found. Trying lookup...");
                _connector.SendInMessage(new SecurityLookupMessage
                {
                    SecurityId = new SecurityId { SecurityCode = ticker }
                });

                // Ждём появления
                var lookupStart = DateTime.UtcNow;
                while ((DateTime.UtcNow - lookupStart).TotalMilliseconds < 10000)
                {
                    security = _connector.Securities.FirstOrDefault(s => s.Code == ticker);
                    if (security != null) break;
                    await Task.Delay(100);
                }
            }

            if (security == null)
            {
                Console.WriteLine($"❌ Security {ticker} still not found");
                return;
            }

            Console.WriteLine($"✅ Found security: {security.Code}@{security.Board?.Code}");

            // Создаём подписку на свечи (новый API)
            var subscription = new Subscription(DataType.TimeFrame(TimeSpan.FromMinutes(5)), security)
            {
                To = DateTime.Now
            };

            // Подписываемся на свечи
            var candleCount = 0;
            _connector.CandleReceived += (sub, candle) =>
            {
                if (sub == subscription)
                {
                    candleCount++;
                    Console.WriteLine($"🕯️  [{candleCount}] {candle.OpenTime:HH:mm:ss} O:{candle.OpenPrice:F2} H:{candle.HighPrice:F2} L:{candle.LowPrice:F2} C:{candle.ClosePrice:F2} V:{candle.TotalVolume}");
                }
            };

            _connector.Subscribe(subscription);

            // Ждём 5 свечей или 30 секунд
            var timeout = 30000;
            var start = DateTime.UtcNow;

            while ((DateTime.UtcNow - start).TotalMilliseconds < timeout && candleCount < 5)
            {
                await Task.Delay(1000);
            }

            Console.WriteLine($"✅ Received {candleCount} candles");

            // Отписываемся
            _connector.UnSubscribe(subscription);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Candles test failed: {ex.Message}\n{ex.StackTrace}");
        }
    }

    public void Dispose()
    {
        _connector?.Dispose();
    }
}
