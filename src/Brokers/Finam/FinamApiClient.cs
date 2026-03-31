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
        _http = new HttpClient { BaseAddress = new Uri(BaseUrl) };
        _http.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        _http.Timeout = TimeSpan.FromSeconds(30);

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

            var response = await _http.PostAsync("/v1/sessions", content);
            var responseContent = await response.Content.ReadAsStringAsync();

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
    public async Task<BarsResponse?> GetBarsAsync(string symbol, string timeframe, string from, string to)
        => await GetAsync<BarsResponse>($"/v1/marketdata/bars?symbol={symbol}&timeframe={timeframe}&interval.start_time={from}&interval.end_time={to}");

    // === Orders Service ===

    /// <summary>Получить список заявок</summary>
    public async Task<OrdersResponse?> GetOrdersAsync(string accountId)
        => await GetAsync<OrdersResponse>($"/v1/orders?account_id={accountId}");

    /// <summary>Выставить заявку</summary>
    public async Task<PlaceOrderResponse?> PlaceOrderAsync(PlaceOrderRequest request)
        => await PostAsync<PlaceOrderResponse>("/v1/orders", request);

    /// <summary>Отменить заявку</summary>
    public async Task<CancelOrderResponse?> CancelOrderAsync(string accountId, string orderId)
        => await PostAsync<CancelOrderResponse>("/v1/orders/cancel", new { account_id = accountId, order_id = orderId });

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

    private async Task<T?> PostAsync<T>(string path, object body) where T : class
    {
        await EnsureAuthenticatedAsync();
        try
        {
            var json = JsonSerializer.Serialize(body, _jsonOptions);
            var content = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
            var response = await _http.PostAsync(path, content);
            var responseContent = await response.Content.ReadAsStringAsync();
            
            if (!response.IsSuccessStatusCode)
            {
                throw new FinamApiException(response.StatusCode, responseContent);
            }
            
            return JsonSerializer.Deserialize<T>(responseContent, _jsonOptions);
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
