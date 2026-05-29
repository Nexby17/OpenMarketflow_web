using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace HedgeFund.Brokers.Finam
{
    // Response models
    public class DataProviderQuote
    {
        [JsonPropertyName("symbol")] public string Symbol { get; set; }
        [JsonPropertyName("bid")] public double Bid { get; set; }
        [JsonPropertyName("ask")] public double Ask { get; set; }
        [JsonPropertyName("last")] public double Last { get; set; }
        [JsonPropertyName("bid_size")] public double BidSize { get; set; }
        [JsonPropertyName("ask_size")] public double AskSize { get; set; }
        [JsonPropertyName("last_size")] public double LastSize { get; set; }
        [JsonPropertyName("volume")] public double Volume { get; set; }
        [JsonPropertyName("timestamp")] public string Timestamp { get; set; }
    }

    public class DataProviderCandle
    {
        [JsonPropertyName("timestamp")] public string Timestamp { get; set; }
        [JsonPropertyName("open")] public double Open { get; set; }
        [JsonPropertyName("high")] public double High { get; set; }
        [JsonPropertyName("low")] public double Low { get; set; }
        [JsonPropertyName("close")] public double Close { get; set; }
        [JsonPropertyName("volume")] public double Volume { get; set; }
    }

    public class DataProviderPosition
    {
        [JsonPropertyName("ticker")] public string Ticker { get; set; }
        [JsonPropertyName("dir")] public int Dir { get; set; }
        [JsonPropertyName("lots")] public int Lots { get; set; }
        [JsonPropertyName("avg_price")] public double AvgPrice { get; set; }
        [JsonPropertyName("current_price")] public double CurrentPrice { get; set; }
        [JsonPropertyName("error")] public string Error { get; set; }
    }

    public class DataProviderOrder
    {
        [JsonPropertyName("order_id")] public string Id { get; set; }
        [JsonPropertyName("symbol")] public string Symbol { get; set; }
        [JsonPropertyName("side")] public string Side { get; set; }
        [JsonPropertyName("type")] public string Type { get; set; }
        [JsonPropertyName("limit_price")] public double Price { get; set; }
        [JsonPropertyName("quantity")] public double Quantity { get; set; }
        [JsonPropertyName("status")] public string Status { get; set; }
        [JsonPropertyName("comment")] public string Comment { get; set; }
        [JsonPropertyName("is_active")] public bool IsActive { get; set; }
    }

    public class DataProviderFill
    {
        [JsonPropertyName("order_id")] public string OrderId { get; set; }
        [JsonPropertyName("symbol")] public string Symbol { get; set; }
        [JsonPropertyName("side")] public string Side { get; set; }
        [JsonPropertyName("quantity")] public double Quantity { get; set; }
        [JsonPropertyName("limit_price")] public double Price { get; set; }
        [JsonPropertyName("account_id")] public string AccountId { get; set; }
        [JsonPropertyName("fill_ts")] public double FillTs { get; set; }
    }

    /// <summary>
    /// HTTP client for DataProvider REST API (localhost:5060).
    /// Thread-safe. Graceful fallback on errors.
    /// </summary>
    public class DataProviderClient : IDisposable
    {
        private static readonly JsonSerializerOptions _json = new()
        {
            PropertyNameCaseInsensitive = true
        };

        private readonly HttpClient _http;

        public DataProviderClient(string baseUrl = "http://localhost:5060")
        {
            var handler = new SocketsHttpHandler
            {
                PooledConnectionLifetime = TimeSpan.FromMinutes(5),
                PooledConnectionIdleTimeout = TimeSpan.FromMinutes(2),
                MaxConnectionsPerServer = 4,
                EnableMultipleHttp2Connections = true,
            };
            _http = new HttpClient(handler)
            {
                BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"),
                Timeout = TimeSpan.FromSeconds(3)
            };
        }

        public async Task<DataProviderQuote> GetQuoteAsync(string symbol)
        {
            return await GetAsync<DataProviderQuote>($"quote/{symbol}").ConfigureAwait(false);
        }

        public async Task<List<DataProviderCandle>> GetCandlesAsync(string symbol, string tf = "M1", int limit = 100)
        {
            try
            {
                var json = await _http.GetStringAsync($"candles/{symbol}?tf={tf}&limit={limit}").ConfigureAwait(false);
                var doc = JsonDocument.Parse(json);
                // DataProvider returns { "bars": [...] }
                if (doc.RootElement.TryGetProperty("bars", out var bars))
                    return JsonSerializer.Deserialize<List<DataProviderCandle>>(bars.GetRawText(), _json);
                return JsonSerializer.Deserialize<List<DataProviderCandle>>(json, _json);
            }
            catch (Exception ex) { Console.WriteLine($"[DP] GET candles/{symbol} error: {ex.Message}"); return null; }
        }

        public async Task<DataProviderPosition> GetPositionAsync(string account, string ticker)
        {
            try
            {
                var json = await _http.GetStringAsync($"position?account={account}&ticker={ticker}").ConfigureAwait(false);
                var doc = JsonDocument.Parse(json);
                // "error" field means fetch failed — return position WITH error flag
                // so caller can distinguish "no position" from "API error"
                return JsonSerializer.Deserialize<DataProviderPosition>(json, _json);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DP] GetPosition error: {ex.Message}");
                return null;
            }
        }

        public async Task<List<DataProviderOrder>> GetOrdersAsync(string account = "")
        {
            var path = string.IsNullOrEmpty(account) ? "orders" : $"orders?account={account}";
            return await GetAsync<List<DataProviderOrder>>(path).ConfigureAwait(false);
        }

        private async Task<T> GetAsync<T>(string path) where T : class
        {
            try
            {
                var json = await _http.GetStringAsync(path).ConfigureAwait(false);
                return JsonSerializer.Deserialize<T>(json, _json);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DP] GET {path} error: {ex.Message}");
                return null;
            }
        }

        public async Task<List<DataProviderFill>> GetRecentFillsAsync(string account = "", string symbol = "")
        {
            var path = $"recent-fills?account={account}&symbol={symbol}";
            return await GetAsync<List<DataProviderFill>>(path).ConfigureAwait(false);
        }

        public async Task InvalidateCacheAsync(string accountId)
        {
            try { await _http.PostAsync($"invalidate?account={accountId}", null).ConfigureAwait(false); }
            catch { }
        }

        public void Dispose() => _http.Dispose();
    }
}
