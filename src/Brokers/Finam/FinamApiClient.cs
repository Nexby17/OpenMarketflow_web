using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// HTTP-клиент для Finam Trade API v1 (новый API).
/// Base URL: https://api.finam.ru
/// Auth: двухэтапная — access_token → JWT (POST /v1/sessions)
/// Все запросы с Authorization: Bearer {jwt}
/// JWT живёт 15 минут, обновляется автоматически за минуту до истечения.
/// </summary>
public class FinamApiClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly JsonSerializerOptions _jsonOptions;
    private readonly string _accessToken;
    private string? _jwt;
    private DateTime _jwtExpiresAt = DateTime.MinValue;
    private readonly SemaphoreSlim _authLock = new(1, 1);

    public const string BaseUrl = "https://api.finam.ru";
    private static readonly TimeSpan JwtLifetime = TimeSpan.FromMinutes(14); // обновляем за минуту до истечения (15 мин)

    public FinamApiClient(string accessToken)
    {
        _accessToken = accessToken;
        _http = new HttpClient(new SocketsHttpHandler
        {
            AllowAutoRedirect = true,
            MaxAutomaticRedirections = 5
        }, false) { BaseAddress = new Uri(BaseUrl) };
        _http.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        _http.Timeout = TimeSpan.FromSeconds(5);
        _http.DefaultRequestVersion = System.Net.HttpVersion.Version11;

        _jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
        };
    }

    // === Auth ===

    /// <summary>
    /// Получить JWT через POST /v1/sessions с access_token.
    /// Кеширует JWT на 14 минут.
    /// </summary>
    public async Task<string> AuthenticateAsync()
    {
        await _authLock.WaitAsync();
        try
        {
            var request = new AuthRequest { Secret = _accessToken };
            var json = JsonSerializer.Serialize(request, _jsonOptions);
            var content = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
            content.Headers.ContentType.CharSet = null; // Финам не принимает charset в Content-Type

            var response = await _http.PostAsync("/v1/sessions", content);
            var responseContent = await response.Content.ReadAsStringAsync();

            Console.WriteLine($"[FINAM-AUTH] Body first 80: {json[..Math.Min(80, json.Length)]}");
            Console.WriteLine($"[FINAM-AUTH] POST /v1/sessions → {(int)response.StatusCode} | Body sent: {json.Length} chars | Response: {responseContent[..Math.Min(200, responseContent.Length)]}");

            if (!response.IsSuccessStatusCode)
                throw new FinamApiException(response.StatusCode, responseContent);

            var authResponse = JsonSerializer.Deserialize<AuthResponse>(responseContent, _jsonOptions);
            _jwt = authResponse?.Token ?? throw new FinamApiException(
                System.Net.HttpStatusCode.InternalServerError, "Empty JWT in auth response");
            _jwtExpiresAt = DateTime.UtcNow.Add(JwtLifetime);

            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", _jwt);
            return _jwt;
        }
        finally
        {
            _authLock.Release();
        }
    }

    /// <summary>Get current JWT token (authenticates if needed).</summary>
    public async Task<string> GetJwtAsync() => await AuthenticateAsync();

    /// <summary>
    /// Получить детали токена (account_ids, readonly, exchanges).
    /// </summary>
    public async Task<TokenDetailsResponse?> GetTokenDetailsAsync()
    {
        await EnsureAuthenticatedAsync();

        var request = new TokenDetailsRequest { Token = _jwt! };
        return await PostAsync<TokenDetailsResponse>("/v1/sessions/details", request);
    }

    /// <summary>
    /// Автоматически обновить JWT если истёк или скоро истечёт.
    /// </summary>
    private async Task EnsureAuthenticatedAsync()
    {
        if (_jwt == null || DateTime.UtcNow >= _jwtExpiresAt)
        {
            await AuthenticateAsync();
        }
    }

    // === Assets Service ===

    /// <summary>Получить серверное время</summary>
    public async Task<ClockResponse?> GetClockAsync()
        => await GetAsync<ClockResponse>("/v1/assets/clock");

    /// <summary>Получить список бирж</summary>
    public async Task<ExchangesResponse?> GetExchangesAsync()
        => await GetAsync<ExchangesResponse>("/v1/exchanges");

    /// <summary>Получить список доступных инструментов</summary>
    public async Task<AssetsResponse?> GetAssetsAsync()
        => await GetAsync<AssetsResponse>("/v1/assets");

    /// <summary>Получить все инструменты с пагинацией</summary>
    public async Task<AllAssetsResponse?> GetAllAssetsAsync(long cursor = 0, bool onlyActive = true)
        => await GetAsync<AllAssetsResponse>($"/v1/assets/all?cursor={cursor}&only_active={onlyActive.ToString().ToLower()}");

    /// <summary>Получить инструмент по символу (ticker@mic)</summary>
    public async Task<AssetDetailResponse?> GetAssetAsync(string symbol, string accountId)
        => await GetAsync<AssetDetailResponse>($"/v1/assets/{symbol}?account_id={accountId}");

    /// <summary>Получить расписание торгов</summary>
    public async Task<ScheduleResponse?> GetScheduleAsync(string symbol)
        => await GetAsync<ScheduleResponse>($"/v1/assets/{symbol}/schedule");

    // === Accounts Service ===

    /// <summary>Получить список счетов</summary>
    public async Task<AccountsResponse?> GetAccountsAsync()
        => await GetAsync<AccountsResponse>("/v1/accounts");

    /// <summary>Получить информацию по счёту</summary>
    public async Task<AccountResponse?> GetAccountAsync(string accountId)
        => await GetAsync<AccountResponse>($"/v1/accounts/{accountId}");

    // === MarketData Service ===

    /// <summary>Получить свечи (бары)</summary>
    /// <summary>Получить стакан (orderbook)</summary>
    public async Task<object?> GetOrderBookAsync(string symbol)
    {
        await EnsureAuthenticatedAsync();
        try
        {
            var response = await _http.GetAsync($"/v1/instruments/{symbol}/orderbook");
            var content = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode) return new { rows = Array.Empty<object>(), error = content };
            // Возвращаем как есть — JSON прокинется на клиент
            var doc = System.Text.Json.JsonDocument.Parse(content);
            var orderbook = doc.RootElement.GetProperty("orderbook");
            var rows = new List<object>();
            foreach (var row in orderbook.GetProperty("rows").EnumerateArray())
            {
                var price = row.GetProperty("price").GetProperty("value").GetString() ?? "0";
                string bidVol = "0", askVol = "0";
                if (row.TryGetProperty("buy_size", out var bs)) bidVol = bs.GetProperty("value").GetString() ?? "0";
                if (row.TryGetProperty("sell_size", out var ss)) askVol = ss.GetProperty("value").GetString() ?? "0";
                rows.Add(new { price = double.Parse(price, System.Globalization.CultureInfo.InvariantCulture),
                               bid = double.Parse(bidVol, System.Globalization.CultureInfo.InvariantCulture),
                               ask = double.Parse(askVol, System.Globalization.CultureInfo.InvariantCulture) });
            }
            return new { rows };
        }
        catch (Exception ex) { return new { rows = Array.Empty<object>(), error = ex.Message }; }
    }

    public async Task<BarsResponse?> GetBarsAsync(string symbol, string timeframe, string from, string to)
        => await GetAsync<BarsResponse>($"/v1/instruments/{symbol}/bars?timeframe={timeframe}&interval.start_time={from}&interval.end_time={to}");

    // === Orders Service ===

    /// <summary>Получить список заявок</summary>
    public async Task<OrdersResponse?> GetOrdersAsync(string accountId)
        => await GetAsync<OrdersResponse>($"/v1/accounts/{accountId}/orders");

    /// <summary>Выставить заявку</summary>
    public async Task<PlaceOrderResponse?> PlaceOrderAsync(string accountId, PlaceOrderRequest request)
        => await PostAsync<PlaceOrderResponse>($"/v1/accounts/{accountId}/orders", request);

    /// <summary>Отменить заявку</summary>
    public async Task<CancelOrderResponse?> CancelOrderAsync(string accountId, string orderId)
        => await DeleteAsync<CancelOrderResponse>($"/v1/accounts/{accountId}/orders/{orderId}");

    // === HTTP helpers ===

    private async Task<T?> GetAsync<T>(string path) where T : class
    {
        await EnsureAuthenticatedAsync();
        try
        {
            var response = await _http.GetAsync(path);
            var content = await response.Content.ReadAsStringAsync();
            
            if (!response.IsSuccessStatusCode)
            {
                throw new FinamApiException(response.StatusCode, content);
            }
            
            return JsonSerializer.Deserialize<T>(content, _jsonOptions);
        }
        catch (FinamApiException) { throw; }
        catch (Exception ex)
        {
            throw new FinamApiException(System.Net.HttpStatusCode.InternalServerError, ex.Message);
        }
    }

    private async Task<T?> DeleteAsync<T>(string path) where T : class
    {
        await EnsureAuthenticatedAsync();
        try
        {
            var response = await _http.DeleteAsync(path);
            var content = await response.Content.ReadAsStringAsync();
            
            if (!response.IsSuccessStatusCode)
            {
                throw new FinamApiException(response.StatusCode, content);
            }
            
            return JsonSerializer.Deserialize<T>(content, _jsonOptions);
        }
        catch (FinamApiException) { throw; }
        catch (Exception ex)
        {
            throw new FinamApiException(System.Net.HttpStatusCode.InternalServerError, ex.Message);
        }
    }

    private async Task<T?> PostAsync<T>(string path, object body) where T : class
    {
        await EnsureAuthenticatedAsync();
        Console.WriteLine($"[REST] POST {path} | JWT={_jwt?.Substring(0,Math.Min(20,_jwt.Length))}...");
        try
        {
            var json = JsonSerializer.Serialize(body, _jsonOptions);
            var content = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
            var response = await _http.PostAsync(path, content);
            
            var responseContent = await response.Content.ReadAsStringAsync();
            
            Console.WriteLine($"[REST] POST {path} → {(int)response.StatusCode} ({responseContent.Length} bytes)");
            
            // Finam возвращает 308 для путей без trailing slash
            if (response.StatusCode == System.Net.HttpStatusCode.Redirect
                || response.StatusCode == System.Net.HttpStatusCode.RedirectMethod
                || (int)response.StatusCode == 308)
            {
                var location = response.Headers.Location?.ToString();
                if (!string.IsNullOrEmpty(location))
                {
                    Console.WriteLine($"[REST] 308 redirect {path} → {location}");
                    var newContent = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
                    response = await _http.PostAsync(location, newContent);
                }
            }
            
            var responseContent2 = await response.Content.ReadAsStringAsync();
            Console.WriteLine($"[REST] POST {path} → {(int)response.StatusCode} body={responseContent2}");
            
            if (!response.IsSuccessStatusCode)
            {
                throw new FinamApiException(response.StatusCode, responseContent2);
            }
            
            return JsonSerializer.Deserialize<T>(responseContent2, _jsonOptions);
        }
        catch (FinamApiException) { throw; }
        catch (Exception ex)
        {
            throw new FinamApiException(System.Net.HttpStatusCode.InternalServerError, ex.Message);
        }
    }

    public void Dispose() => _http.Dispose();
}

// === Исключение API ===

public class FinamApiException : Exception
{
    public System.Net.HttpStatusCode StatusCode { get; }
    public string ResponseBody { get; }

    public FinamApiException(System.Net.HttpStatusCode statusCode, string responseBody)
        : base($"Finam API error {(int)statusCode}: {responseBody}")
    {
        StatusCode = statusCode;
        ResponseBody = responseBody;
    }
}
