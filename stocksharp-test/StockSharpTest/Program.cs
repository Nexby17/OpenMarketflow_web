using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

namespace StockSharpTest;

class Program
{
    static readonly HttpClient _http = new() { BaseAddress = new Uri("https://tradeapi.finam.ru") };

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
            var authBody = $"{{\"secret\":\"{accessToken}\"}}";
            var authResp = await _http.PostAsync("/v1/sessions",
                new StringContent(authBody, Encoding.UTF8, "application/json"));
            var authJson = await authResp.Content.ReadAsStringAsync();

            Console.WriteLine($"  HTTP {(int)authResp.StatusCode} {authResp.StatusCode}");
            Console.WriteLine($"  Ответ (первые 500): {authJson[..Math.Min(500, authJson.Length)]}");

            if (!authResp.IsSuccessStatusCode)
            {
                Console.WriteLine($"  ❌ Auth failed!");
                return;
            }

            using var doc = JsonDocument.Parse(authJson);
            var root = doc.RootElement;

            // Перебираем все свойства и ищем JWT (строку с 3 частями через точку)
            Console.WriteLine("  Свойства ответа:");
            foreach (var prop in root.EnumerateObject())
            {
                var valStr = prop.Value.ToString();
                Console.WriteLine($"    {prop.Name}: {prop.Value.ValueKind} (длина: {valStr.Length}) = {valStr[..Math.Min(100, valStr.Length)]}...");

                if (prop.Value.ValueKind == JsonValueKind.String)
                {
                    var val = prop.Value.GetString()!;
                    if (val.Split('.').Length == 3 && val.Length > 100)
                    {
                        jwt = val;
                        Console.WriteLine($"    → Это JWT! (длина: {jwt.Length})");
                    }
                }
            }

            if (string.IsNullOrEmpty(jwt))
            {
                Console.WriteLine("  ⚠️ JWT с 3 частями не найден. Ищем любой длинный токен...");
                foreach (var prop in root.EnumerateObject())
                {
                    if (prop.Value.ValueKind == JsonValueKind.String && prop.Value.GetString()!.Length > 50)
                    {
                        jwt = prop.Value.GetString();
                        Console.WriteLine($"  → Используем {prop.Name} как токен (длина: {jwt!.Length})");
                        break;
                    }
                }
            }

            if (string.IsNullOrEmpty(jwt))
            {
                Console.WriteLine("  ❌ Не удалось получить JWT!");
                return;
            }

            Console.WriteLine($"  ✅ JWT: {jwt[..Math.Min(50, jwt.Length)]}...");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ❌ Ошибка авторизации: {ex.Message}");
            return;
        }

        // Пробуем оба формата авторизации
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", jwt);

        // ═══════════════════════════════════════════════
        // Шаг 2: Время сервера (не требует auth)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 2: Время сервера ---");
        try
        {
            var resp = await _http.GetAsync("/v1/assets/clock");
            var json = await resp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)resp.StatusCode}: {json[..Math.Min(300, json.Length)]}");
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // ═══════════════════════════════════════════════
        // Шаг 3: Биржи
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 3: Доступные биржи ---");
        try
        {
            var resp = await _http.GetAsync("/v1/exchanges");
            var json = await resp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)resp.StatusCode}: {json[..Math.Min(1000, json.Length)]}");
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // ═══════════════════════════════════════════════
        // Шаг 4: Получить account_id через сессию
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 4: Получение аккаунтов ---");
        string? accountId = null;

        // Пробуем /v1/sessions/details
        try
        {
            var resp = await _http.PostAsync("/v1/sessions/details",
                new StringContent("{}", Encoding.UTF8, "application/json"));
            var json = await resp.Content.ReadAsStringAsync();
            Console.WriteLine($"  /v1/sessions/details HTTP {(int)resp.StatusCode}: {json[..Math.Min(500, json.Length)]}");

            if (resp.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(json);
                // Ищем account_id в ответе
                var flat = FlattenJson(doc.RootElement);
                foreach (var kv in flat.Where(x => x.Key.Contains("account", StringComparison.OrdinalIgnoreCase) ||
                                                    x.Key.Contains("clientId", StringComparison.OrdinalIgnoreCase)))
                {
                    Console.WriteLine($"    {kv.Key} = {kv.Value}");
                    if (accountId == null && !string.IsNullOrEmpty(kv.Value))
                        accountId = kv.Value;
                }
            }
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // Если не нашли — попробуем другие пути
        if (accountId == null)
        {
            // Пробуем без Bearer — просто токен в заголовке
            Console.WriteLine("  Пробуем auth с прямым токеном...");
            var tmpHttp = new HttpClient { BaseAddress = new Uri("https://tradeapi.finam.ru") };
            tmpHttp.DefaultRequestHeaders.Add("Authorization", jwt);
            try
            {
                var resp = await tmpHttp.PostAsync("/v1/sessions/details",
                    new StringContent("{}", Encoding.UTF8, "application/json"));
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Direct auth HTTP {(int)resp.StatusCode}: {json[..Math.Min(300, json.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }
        }

        Console.WriteLine($"\n  Account ID: {accountId ?? "НЕ НАЙДЕН"}");

        // ═══════════════════════════════════════════════
        // Шаг 5: Инструменты (через /v1/assets/all с пагинацией)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 5: Список инструментов ---");

        // Попробуем разные endpoint'ы
        var assetUrls = new[]
        {
            "/v1/assets/all?limit=10",
            "/v1/assets?limit=10",
            "/v1/assets/all",
        };
        foreach (var url in assetUrls)
        {
            try
            {
                var resp = await _http.GetAsync(url);
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  {url}: HTTP {(int)resp.StatusCode} (size: {json.Length})");
                if (resp.IsSuccessStatusCode && json.Length < 5000)
                {
                    Console.WriteLine($"    {json[..Math.Min(1000, json.Length)]}");
                }
                else if (resp.IsSuccessStatusCode)
                {
                    Console.WriteLine($"    Первые 1000 символов: {json[..Math.Min(1000, json.Length)]}");
                }
                else
                {
                    Console.WriteLine($"    {json[..Math.Min(300, json.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  {url}: ⚠️ {ex.Message}"); }
        }

        // Конкретные символы — попробуем с разным форматом
        Console.WriteLine("\n  Поиск конкретных символов:");
        var symbolVariants = new[]
        {
            "SBER@TQBR", "MOEX:SBER", "SBER.TQBR", "TQBR:SBER",
            "GAZP@TQBR", "SiM5@FORTS", "Si-6.25@FORTS",
            "SBER", "FUT:SiM5",
        };
        foreach (var sym in symbolVariants)
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/assets/{Uri.EscapeDataString(sym)}");
                var json = await resp.Content.ReadAsStringAsync();
                if (resp.IsSuccessStatusCode)
                {
                    Console.WriteLine($"  ✅ {sym}: {json[..Math.Min(400, json.Length)]}");
                }
                else
                {
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(150, json.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 6: Свечи (если нашли account)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 6: Дневные свечи ---");
        var candleSyms = new[] { "SBER@TQBR", "SBER", "MOEX:SBER", "GAZP" };
        foreach (var sym in candleSyms)
        {
            try
            {
                var startTime = DateTime.UtcNow.AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ssZ");
                var endTime = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
                var url = $"/v1/instruments/{Uri.EscapeDataString(sym)}/bars?timeframe=TIME_FRAME_D&interval.startTime={startTime}&interval.endTime={endTime}";
                var resp = await _http.GetAsync(url);
                var json = await resp.Content.ReadAsStringAsync();

                if (resp.IsSuccessStatusCode)
                {
                    Console.WriteLine($"  ✅ {sym} (size: {json.Length}):");
                    Console.WriteLine($"    {json[..Math.Min(500, json.Length)]}");
                    break;
                }
                else
                {
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(200, json.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 7: Стакан
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 7: Стакан ---");
        foreach (var sym in new[] { "SBER@TQBR", "SBER", "MOEX:SBER" })
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{Uri.EscapeDataString(sym)}/orderbook");
                var json = await resp.Content.ReadAsStringAsync();
                if (resp.IsSuccessStatusCode)
                {
                    Console.WriteLine($"  ✅ {sym} стакан (size: {json.Length}):");
                    Console.WriteLine($"    {json[..Math.Min(800, json.Length)]}");
                    break;
                }
                else
                {
                    Console.WriteLine($"  ❌ {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(200, json.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 8: Котировки & Сделки
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 8: Котировки и сделки ---");
        foreach (var sym in new[] { "SBER@TQBR", "SBER" })
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{Uri.EscapeDataString(sym)}/quotes/latest");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Quotes {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }

            try
            {
                var resp = await _http.GetAsync($"/v1/instruments/{Uri.EscapeDataString(sym)}/trades/latest");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Trades {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 9: Торговые параметры и расписание
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 9: Параметры и расписание ---");
        foreach (var sym in new[] { "SBER@TQBR", "SBER" })
        {
            try
            {
                var resp = await _http.GetAsync($"/v1/assets/{Uri.EscapeDataString(sym)}/params");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Params {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }

            try
            {
                var resp = await _http.GetAsync($"/v1/assets/{Uri.EscapeDataString(sym)}/schedule");
                var json = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Schedule {sym}: HTTP {(int)resp.StatusCode} - {json[..Math.Min(400, json.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 10: Usage / Rate limits
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 10: Usage ---");
        try
        {
            var resp = await _http.GetAsync("/v1/usage");
            var json = await resp.Content.ReadAsStringAsync();
            Console.WriteLine($"  HTTP {(int)resp.StatusCode}: {json[..Math.Min(500, json.Length)]}");
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        Console.WriteLine("\n=== ✅ Тест завершён! ===");
    }

    static List<KeyValuePair<string, string>> FlattenJson(JsonElement el, string prefix = "")
    {
        var result = new List<KeyValuePair<string, string>>();
        switch (el.ValueKind)
        {
            case JsonValueKind.Object:
                foreach (var prop in el.EnumerateObject())
                    result.AddRange(FlattenJson(prop.Value, prefix + prop.Name + "."));
                break;
            case JsonValueKind.Array:
                int i = 0;
                foreach (var item in el.EnumerateArray())
                    result.AddRange(FlattenJson(item, prefix + $"[{i++}]."));
                break;
            default:
                result.Add(new(prefix.TrimEnd('.'), el.ToString()));
                break;
        }
        return result;
    }
}
