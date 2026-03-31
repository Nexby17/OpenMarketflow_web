using System;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace StockSharpTest;

class Program
{
    static readonly HttpClient _http = new() { BaseAddress = new Uri("https://trade-api.finam.ru") };
    static readonly JsonSerializerOptions _json = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    static async Task Main(string[] args)
    {
        Console.WriteLine("=== Finam Trade API Test ===\n");

        var accessToken = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (string.IsNullOrEmpty(accessToken))
        {
            Console.WriteLine("❌ FINAM_TOKEN не задан!");
            return;
        }
        Console.WriteLine($"✅ Access token получен (длина: {accessToken.Length})");

        // ═══════════════════════════════════════════════
        // Шаг 1: Получить JWT через POST /v1/sessions
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 1: Авторизация (JWT) ---");
        string? jwt = null;
        try
        {
            var authBody = JsonSerializer.Serialize(new { secret = accessToken }, _json);
            var authResp = await _http.PostAsync("/v1/sessions",
                new StringContent(authBody, Encoding.UTF8, "application/json"));
            var authJson = await authResp.Content.ReadAsStringAsync();

            Console.WriteLine($"  HTTP {(int)authResp.StatusCode} {authResp.StatusCode}");

            if (!authResp.IsSuccessStatusCode)
            {
                Console.WriteLine($"  ❌ Auth failed: {authJson[..Math.Min(500, authJson.Length)]}");
                return;
            }

            using var doc = JsonDocument.Parse(authJson);
            jwt = doc.RootElement.TryGetProperty("token", out var t) ? t.GetString()
                : doc.RootElement.TryGetProperty("accessToken", out var at) ? at.GetString()
                : doc.RootElement.TryGetProperty("jwt", out var j) ? j.GetString()
                : null;

            if (string.IsNullOrEmpty(jwt))
            {
                // Может быть вложено в data или результат — выведем весь ответ
                Console.WriteLine($"  ⚠️ JWT не найден в ответе. Полный ответ:");
                Console.WriteLine($"  {authJson[..Math.Min(800, authJson.Length)]}");
                
                // Попробуем перебрать все свойства
                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    Console.WriteLine($"  Ключ: {prop.Name} = {prop.Value.ToString()[..Math.Min(100, prop.Value.ToString().Length)]}...");
                    if (prop.Value.ValueKind == JsonValueKind.String && prop.Value.GetString()!.Length > 50)
                    {
                        jwt = prop.Value.GetString();
                        Console.WriteLine($"  → Используем {prop.Name} как JWT (длина: {jwt!.Length})");
                        break;
                    }
                }
            }

            if (string.IsNullOrEmpty(jwt))
            {
                Console.WriteLine("  ❌ Не удалось получить JWT!");
                return;
            }

            Console.WriteLine($"  ✅ JWT получен (длина: {jwt.Length})");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ❌ Ошибка авторизации: {ex.Message}");
            return;
        }

        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", jwt);

        // ═══════════════════════════════════════════════
        // Шаг 2: Информация о сессии
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 2: Информация о сессии ---");
        try
        {
            var sessResp = await _http.PostAsync("/v1/sessions/details",
                new StringContent("{}", Encoding.UTF8, "application/json"));
            var sessJson = await sessResp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)sessResp.StatusCode}: {sessJson[..Math.Min(500, sessJson.Length)]}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ⚠️ {ex.Message}");
        }

        // ═══════════════════════════════════════════════
        // Шаг 3: Время сервера
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 3: Время сервера ---");
        try
        {
            var clockResp = await _http.GetAsync("/v1/assets/clock");
            var clockJson = await clockResp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)clockResp.StatusCode}: {clockJson[..Math.Min(300, clockJson.Length)]}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ⚠️ {ex.Message}");
        }

        // ═══════════════════════════════════════════════
        // Шаг 4: Список бирж
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 4: Доступные биржи ---");
        try
        {
            var exchResp = await _http.GetAsync("/v1/exchanges");
            var exchJson = await exchResp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)exchResp.StatusCode}: {exchJson[..Math.Min(500, exchJson.Length)]}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ⚠️ {ex.Message}");
        }

        // ═══════════════════════════════════════════════
        // Шаг 5: Поиск инструментов (SBER, GAZP, Si)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 5: Поиск инструментов ---");
        var symbols = new[] { "SBER", "GAZP", "SiM5", "Si-6.25" };
        foreach (var sym in symbols)
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/assets/{sym}");
                var json = await resp.Content.ReadAsStringAsync();
                if (resp.IsSuccessStatusCode)
                    Console.WriteLine($"  ✅ {sym}: {json[..Math.Min(400, json.Length)]}");
                else
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(200, json.Length)]}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  ❌ {sym}: {ex.Message}");
            }
        }

        // Попробуем получить полный список
        Console.WriteLine("\n  Получаем полный список инструментов...");
        try
        {
            var allResp = await _http.GetAsync("/v1/assets");
            var allJson = await allResp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)allResp.StatusCode}, размер ответа: {allJson.Length} символов");

            if (allResp.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(allJson);
                var root = doc.RootElement;

                // Ищем массив инструментов
                JsonElement assets = default;
                if (root.TryGetProperty("assets", out assets) || root.TryGetProperty("instruments", out assets) || root.TryGetProperty("data", out assets))
                {
                    Console.WriteLine($"  📊 Всего инструментов: {assets.GetArrayLength()}");

                    // Ищем SBER, GAZP, Si
                    int shown = 0;
                    foreach (var item in assets.EnumerateArray())
                    {
                        var itemStr = item.ToString();
                        var code = item.TryGetProperty("symbol", out var s) ? s.GetString()
                                 : item.TryGetProperty("code", out var c) ? c.GetString()
                                 : item.TryGetProperty("ticker", out var tc) ? tc.GetString()
                                 : "";

                        if (code != null && (code.Contains("SBER") || code.Contains("GAZP") || code.Contains("Si")))
                        {
                            Console.WriteLine($"  🔍 {itemStr[..Math.Min(300, itemStr.Length)]}");
                            shown++;
                            if (shown >= 10) break;
                        }
                    }
                    if (shown == 0)
                    {
                        Console.WriteLine("  ⚠️ SBER/GAZP/Si не найдены. Первые 5 инструментов:");
                        int i = 0;
                        foreach (var item in assets.EnumerateArray())
                        {
                            Console.WriteLine($"  [{i}] {item.ToString()[..Math.Min(300, item.ToString().Length)]}");
                            if (++i >= 5) break;
                        }
                    }
                }
                else
                {
                    // Выведем структуру ответа
                    Console.WriteLine("  Структура ответа:");
                    foreach (var prop in root.EnumerateObject())
                    {
                        Console.WriteLine($"  - {prop.Name}: {prop.Value.ValueKind} ({prop.Value.ToString()[..Math.Min(100, prop.Value.ToString().Length)]})");
                    }
                }
            }
            else
            {
                Console.WriteLine($"  ❌ {allJson[..Math.Min(300, allJson.Length)]}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ❌ {ex.Message}");
        }

        // ═══════════════════════════════════════════════
        // Шаг 6: Свечи Si (дневные, последний месяц)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 6: Дневные свечи ---");
        var candleSymbols = new[] { "SBER", "GAZP", "SiM5", "Si-6.25" };
        foreach (var sym in candleSymbols)
        {
            try
            {
                var startTime = DateTime.UtcNow.AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ssZ");
                var endTime = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
                var url = $"/v1/instruments/{sym}/bars?timeframe=TIME_FRAME_D&interval.startTime={startTime}&interval.endTime={endTime}";
                var resp = await _http.GetAsync(url);
                var json = await resp.Content.ReadAsStringAsync();

                if (resp.IsSuccessStatusCode && json.Length > 10)
                {
                    Console.WriteLine($"  ✅ {sym} дневные свечи (ответ: {json.Length} символов):");

                    using var doc = JsonDocument.Parse(json);
                    var root = doc.RootElement;

                    JsonElement bars = default;
                    if (root.TryGetProperty("bars", out bars) || root.TryGetProperty("candles", out bars) || root.TryGetProperty("data", out bars))
                    {
                        Console.WriteLine($"     Количество свечей: {bars.GetArrayLength()}");
                        int i = 0;
                        foreach (var bar in bars.EnumerateArray())
                        {
                            Console.WriteLine($"     {bar}");
                            if (++i >= 5) { Console.WriteLine("     ..."); break; }
                        }
                    }
                    else
                    {
                        Console.WriteLine($"     {json[..Math.Min(500, json.Length)]}");
                    }
                    break; // Нашли рабочий символ
                }
                else
                {
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(200, json.Length)]}");
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  ❌ {sym}: {ex.Message}");
            }
        }

        // ═══════════════════════════════════════════════
        // Шаг 7: Стакан (orderbook)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 7: Стакан (orderbook) ---");
        var obSymbols = new[] { "SBER", "GAZP", "SiM5", "Si-6.25" };
        foreach (var sym in obSymbols)
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{sym}/orderbook");
                var json = await resp.Content.ReadAsStringAsync();

                if (resp.IsSuccessStatusCode && json.Length > 10)
                {
                    Console.WriteLine($"  ✅ {sym} стакан (ответ: {json.Length} символов):");
                    Console.WriteLine($"     {json[..Math.Min(800, json.Length)]}");
                    break;
                }
                else
                {
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(200, json.Length)]}");
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  ❌ {sym}: {ex.Message}");
            }
        }

        // ═══════════════════════════════════════════════
        // Шаг 8: Последние котировки
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 8: Последние котировки ---");
        foreach (var sym in new[] { "SBER", "GAZP" })
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{sym}/quotes/latest");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  ❌ {sym}: {ex.Message}");
            }
        }

        // ═══════════════════════════════════════════════
        // Шаг 9: Последние сделки
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 9: Последние сделки ---");
        foreach (var sym in new[] { "SBER", "GAZP" })
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{sym}/trades/latest");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"  ❌ {sym}: {ex.Message}");
            }
        }

        Console.WriteLine("\n=== ✅ Тест завершён! ===");
    }
}
