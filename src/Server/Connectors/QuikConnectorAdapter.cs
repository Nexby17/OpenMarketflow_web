using System.Text.Json;
using HedgeFund.Core.Connectors;
using HedgeFund.Core.Models;

namespace HedgeFund.Server.Connectors;

/// <summary>
/// QUIK адаптер — полный контур: данные из Lua bridge + торговля через команды.
/// </summary>
public class QuikConnectorAdapter : IConnector
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(5) };
    private bool _connected;
    
    public string Name => "QUIK";
    public bool IsConnected => _connected;
    
    public void MarkAlive() { _connected = true; }
    
    public async Task<bool> ConnectAsync(string token)
    {
        // QUIK подключение проверяется через heartbeat, не через токен
        return true;
    }
    
    public async Task DisconnectAsync() { _connected = false; await Task.CompletedTask; }
    
    public async Task<double> GetBalanceAsync()
    {
        try
        {
            var resp = await _http.GetAsync("http://localhost:5050/api/balance");
            var json = await resp.Content.ReadAsStringAsync();
            var doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("balance", out var bal))
                return bal.GetDouble();
            return 0;
        }
        catch { return 0; }
    }
    
    public async Task<Candle[]> GetCandlesAsync(string ticker, int tfMinutes, int count)
    {
        try
        {
            var resp = await _http.GetAsync("http://localhost:5050/quik/candles");
            var json = await resp.Content.ReadAsStringAsync();
            var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Array) return Array.Empty<Candle>();
            
            var candles = new List<Candle>();
            foreach (var c in doc.RootElement.EnumerateArray())
            {
                candles.Add(new Candle
                {
                    Timestamp = c.TryGetProperty("t", out var t) ? DateTime.Parse(t.GetString()!) : DateTime.UtcNow,
                    Open = c.TryGetProperty("o", out var o) ? o.GetDouble() : 0,
                    High = c.TryGetProperty("h", out var h) ? h.GetDouble() : 0,
                    Low = c.TryGetProperty("l", out var l) ? l.GetDouble() : 0,
                    Close = c.TryGetProperty("c", out var cl) ? cl.GetDouble() : 0,
                    Volume = c.TryGetProperty("v", out var v) ? (long)v.GetDouble() : 0
                });
            }
            return candles.ToArray();
        }
        catch { return Array.Empty<Candle>(); }
    }
    
    public async Task<string?> PlaceOrderAsync(string ticker, string direction, string type, double price, int volume, string comment)
    {
        try
        {
            var json = JsonSerializer.Serialize(new { action = direction.ToLower(), ticker, price, quantity = volume, type = type.ToLower(), comment });
            var content = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
            var resp = await _http.PostAsync("http://localhost:5050/quik/command", content);
            if (resp.IsSuccessStatusCode) return await resp.Content.ReadAsStringAsync();
            return null;
        }
        catch { return null; }
    }
    
    public async Task<bool> CancelOrderAsync(string orderId)
    {
        try
        {
            var resp = await _http.DeleteAsync($"http://localhost:5050/quik/order/{orderId}");
            return resp.IsSuccessStatusCode;
        }
        catch { return false; }
    }
    
    public void SubscribeCandles(string ticker, int tfMinutes, Action<Candle> handler) { }
    public void UnsubscribeCandles() { }
    public void Dispose() { _http.Dispose(); }
}
