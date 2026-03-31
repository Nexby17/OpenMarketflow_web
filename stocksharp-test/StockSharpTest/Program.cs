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
        // Шаг 1: Получить JWT
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 1: Авторизация (JWT) ---");
        string? jwt = null;
        try
        {
            using var authHttp = new HttpClient();
            var authBody = $"{{\"secret\":\"{accessToken}\"}}";
            var authResp = await authHttp.PostAsync("https://tradeapi.finam.ru/v1/sessions",
                new StringContent(authBody, Encoding.UTF8, "application/json"));
            var authJson = await authResp.Content.ReadAsStringAsync();

            Console.WriteLine($"  HTTP {(int)authResp.StatusCode} {authResp.StatusCode}");

            if (!authResp.IsSuccessStatusCode)
            {
                Console.WriteLine($"  ❌ Auth failed: {authJson[..Math.Min(300, authJson.Length)]}");
                return;
            }

            using var doc = JsonDocument.Parse(authJson);
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                if (prop.Value.ValueKind == JsonValueKind.String)
                {
                    var val = prop.Value.GetString()!;
                    Console.WriteLine($"  Property '{prop.Name}': length={val.Length}, parts={val.Split('.').Length}");
                    if (val.Split('.').Length == 3 && val.Length > 100)
                    {
                        jwt = val;
                    }
                }
            }

            if (string.IsNullOrEmpty(jwt))
            {
                Console.WriteLine("  ❌ JWT не найден в ответе!");
                return;
            }

            Console.WriteLine($"  ✅ JWT получен (длина: {jwt.Length}, первые 20: {jwt[..20]}...)");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  ❌ Ошибка: {ex.Message}");
            return;
        }

        // Создаём HttpClient с JWT — НЕ через DefaultRequestHeaders (GH Actions маскирует)
        // Вместо этого добавляем заголовок вручную к каждому запросу
        var http = new HttpClient { BaseAddress = new Uri("https://tradeapi.finam.ru") };

        async Task<(int code, string body)> Get(string url)
        {
            var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.TryAddWithoutValidation("Authorization", $"Bearer {jwt}");
            var resp = await http.SendAsync(req);
            return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync());
        }

        async Task<(int code, string body)> Post(string url, string bodyJson)
        {
            var req = new HttpRequestMessage(HttpMethod.Post, url);
            req.Headers.TryAddWithoutValidation("Authorization", $"Bearer {jwt}");
            req.Content = new StringContent(bodyJson, Encoding.UTF8, "application/json");
            var resp = await http.SendAsync(req);
            return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync());
        }

        // ═══════════════════════════════════════════════
        // Шаг 2: Проверка JWT — session details
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 2: Session details ---");
        try
        {
            var (code, body) = await Post("/v1/sessions/details", "{}");
            Console.WriteLine($"  HTTP {code}: {body[..Math.Min(600, body.Length)]}");

            if (code == 200)
            {
                using var doc = JsonDocument.Parse(body);
                var flat = FlattenJson(doc.RootElement);
                foreach (var kv in flat)
                {
                    Console.WriteLine($"    {kv.Key} = {kv.Value}");
                }
            }
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // ═══════════════════════════════════════════════
        // Шаг 3: Время сервера (unauthenticated)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 3: Время сервера ---");
        try
        {
            var (code, body) = await Get("/v1/assets/clock");
            Console.WriteLine($"  HTTP {code}: {body[..Math.Min(200, body.Length)]}");
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // ═══════════════════════════════════════════════
        // Шаг 4: Список инструментов (SBER@MISX формат)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 4: Инструменты ---");
        try
        {
            var (code, body) = await Get("/v1/assets/all");
            Console.WriteLine($"  HTTP {code}, размер: {body.Length}");

            if (code == 200)
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.TryGetProperty("assets", out var assets))
                {
                    Console.WriteLine($"  📊 Всего инструментов: {assets.GetArrayLength()}");

                    // Ищем SBER, GAZP, Si
                    var targets = new[] { "SBER", "GAZP", "Si" };
                    foreach (var target in targets)
                    {
                        var found = new List<string>();
                        foreach (var asset in assets.EnumerateArray())
                        {
                            var symbol = asset.GetProperty("symbol").GetString() ?? "";
                            var ticker = asset.TryGetProperty("ticker", out var t) ? t.GetString() : "";
                            var name = asset.TryGetProperty("name", out var n) ? n.GetString() : "";
                            var isArchived = asset.TryGetProperty("is_archived", out var a) && a.GetBoolean();
                            
                            if (!isArchived && (ticker?.Contains(target, StringComparison.OrdinalIgnoreCase) == true))
                            {
                                var type = asset.TryGetProperty("type", out var tp) ? tp.GetString() : "?";
                                found.Add($"{symbol} ({type}) - {name}");
                            }
                        }
                        
                        Console.WriteLine($"\n  🔍 '{target}' — найдено {found.Count}:");
                        foreach (var f in found.Take(5))
                            Console.WriteLine($"    {f}");
                        if (found.Count > 5) Console.WriteLine($"    ... и ещё {found.Count - 5}");
                    }
                }
            }
            else
            {
                Console.WriteLine($"  ❌ {body[..Math.Min(300, body.Length)]}");
            }
        }
        catch (Exception ex) { Console.WriteLine($"  ⚠️ {ex.Message}"); }

        // Символы для дальнейших тестов
        var testSymbols = new[] { "SBER@MISX", "GAZP@MISX" };

        // ═══════════════════════════════════════════════
        // Шаг 5: Конкретный инструмент
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 5: Информация по инструменту ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var (code, body) = await Get($"/v1/assets/{Uri.EscapeDataString(sym)}");
                Console.WriteLine($"  {sym}: HTTP {code} - {body[..Math.Min(400, body.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 6: Дневные свечи за последний месяц
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 6: Дневные свечи ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var start = DateTime.UtcNow.AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ssZ");
                var end = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
                var url = $"/v1/instruments/{Uri.EscapeDataString(sym)}/bars?timeframe=TIME_FRAME_D&interval.startTime={start}&interval.endTime={end}";
                var (code, body) = await Get(url);

                Console.WriteLine($"  {sym}: HTTP {code} (size: {body.Length})");
                if (code == 200)
                {
                    using var doc = JsonDocument.Parse(body);
                    if (doc.RootElement.TryGetProperty("bars", out var bars))
                    {
                        Console.WriteLine($"    Свечей: {bars.GetArrayLength()}");
                        int i = 0;
                        foreach (var bar in bars.EnumerateArray())
                        {
                            Console.WriteLine($"    {bar}");
                            if (++i >= 5) { Console.WriteLine("    ..."); break; }
                        }
                    }
                    else
                    {
                        Console.WriteLine($"    {body[..Math.Min(500, body.Length)]}");
                    }
                    break;
                }
                else
                {
                    Console.WriteLine($"    {body[..Math.Min(300, body.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 7: Стакан (orderbook)
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 7: Стакан ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var (code, body) = await Get($"/v1/instruments/{Uri.EscapeDataString(sym)}/orderbook");
                Console.WriteLine($"  {sym}: HTTP {code} (size: {body.Length})");
                if (code == 200)
                {
                    Console.WriteLine($"    {body[..Math.Min(800, body.Length)]}");
                    break;
                }
                else
                {
                    Console.WriteLine($"    {body[..Math.Min(300, body.Length)]}");
                }
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 8: Последние котировки
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 8: Последние котировки ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var (code, body) = await Get($"/v1/instruments/{Uri.EscapeDataString(sym)}/quotes/latest");
                Console.WriteLine($"  {sym}: HTTP {code} - {body[..Math.Min(500, body.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 9: Последние сделки
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 9: Последние сделки ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var (code, body) = await Get($"/v1/instruments/{Uri.EscapeDataString(sym)}/trades/latest");
                Console.WriteLine($"  {sym}: HTTP {code} - {body[..Math.Min(500, body.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 10: Расписание
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 10: Расписание торгов ---");
        foreach (var sym in testSymbols)
        {
            try
            {
                var (code, body) = await Get($"/v1/assets/{Uri.EscapeDataString(sym)}/schedule");
                Console.WriteLine($"  {sym}: HTTP {code} - {body[..Math.Min(500, body.Length)]}");
            }
            catch (Exception ex) { Console.WriteLine($"  ❌ {sym}: {ex.Message}"); }
        }

        // ═══════════════════════════════════════════════
        // Шаг 11: Usage
        // ═══════════════════════════════════════════════
        Console.WriteLine("\n--- Шаг 11: Usage ---");
        try
        {
            var (code, body) = await Get("/v1/usage");
            Console.WriteLine($"  HTTP {code}: {body[..Math.Min(500, body.Length)]}");
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
