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
    }

    public class DataProviderOrder
    {
        [JsonPropertyName("id")] public string Id { get; set; }
        [JsonPropertyName("symbol")] public string Symbol { get; set; }
        [JsonPropertyName("side")] public string Side { get; set; }
        [JsonPropertyName("type")] public string Type { get; set; }
        [JsonPropertyName("price")] public double Price { get; set; }
        [JsonPropertyName("quantity")] public int Quantity { get; set; }
        [JsonPropertyName("status")] public string Status { get; set; }
        [JsonPropertyName("comment")] public string Comment { get; set; }
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
            _http = new HttpClient
            {
                BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"),
                Timeout = TimeSpan.FromSeconds(2)
            };
            _http.DefaultRequestHeaders.ConnectionClose = false;
        }

        public async Task<DataProviderQuote> GetQuoteAsync(string symbol)
        {
            return await GetAsync<DataProviderQuote>($"quote/{symbol}").ConfigureAwait(false);
        }

        public async Task<List<DataProviderCandle>> GetCandlesAsync(string symbol, string tf = "M1", int limit = 100)
        {
            return await GetAsync<List<DataProviderCandle>>($"candles/{symbol}?tf={tf}&limit={limit}").ConfigureAwait(false);
        }

        public async Task<DataProviderPosition> GetPositionAsync(string account, string ticker)
        {
            try
            {
                var json = await _http.GetStringAsync($"position?account={account}&ticker={ticker}").ConfigureAwait(false);
                var doc = JsonDocument.Parse(json);
                if (doc.RootElement.TryGetProperty("error", out _))
                    return null;
                return JsonSerializer.Deserialize<DataProviderPosition>(json, _json);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DP] GetPosition error: {ex.Message}");
                return null;
            }
        }

        public async Task<List<DataProviderOrder>> GetOrdersAsync()
        {
            return await GetAsync<List<DataProviderOrder>>("orders").ConfigureAwait(false);
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

        public void Dispose() => _http.Dispose();
    }
}
