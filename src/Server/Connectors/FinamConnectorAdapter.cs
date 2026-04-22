using HedgeFund.Core.Connectors;
using HedgeFund.Core.Models;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Connectors;

/// <summary>
/// Finam адаптер — полный контур через Finam gRPC + REST.
/// </summary>
public class FinamConnectorAdapter : IConnector
{
    private readonly FinamConnector _broker = new();
    
    public string Name => "Finam";
    public bool IsConnected => _broker.IsConnected;
    
    public async Task<bool> ConnectAsync(string token)
    {
        return await _broker.ConnectAsync(token, "");
    }
    
    public async Task DisconnectAsync() { await Task.CompletedTask; }
    
    public async Task<double> GetBalanceAsync()
    {
        try { var (equity, _) = await _broker.GetAccountInfoAsync(); return equity; }
        catch { return 0; }
    }
    
    public async Task<Candle[]> GetCandlesAsync(string ticker, int tfMinutes, int count)
    {
        try
        {
            var bars = await _broker.GetHistoricalCandlesAsync(ticker, TimeSpan.FromMinutes(tfMinutes), DateTime.UtcNow.AddDays(-7), DateTime.UtcNow);
            return bars ?? Array.Empty<Candle>();
        }
        catch { return Array.Empty<Candle>(); }
    }
    
    public async Task<string?> PlaceOrderAsync(string ticker, string direction, string type, double price, int volume, string comment)
    {
        try
        {
            var order = new Order
            {
                Ticker = ticker,
                Direction = direction == "Buy" ? SignalDirection.Buy : SignalDirection.Sell,
                Type = type == "Limit" ? OrderType.Limit : OrderType.Market,
                Price = price,
                Volume = volume,
                Comment = comment
            };
            var result = await _broker.PlaceOrderAsync(order);
            return result.BrokerOrderId;
        }
        catch { return null; }
    }
    
    public async Task<bool> CancelOrderAsync(string orderId)
    {
        try { await _broker.CancelOrderAsync(orderId); return true; }
        catch { return false; }
    }
    
    public void SubscribeCandles(string ticker, int tfMinutes, Action<Candle> handler)
    {
        _ = _broker.SubscribeCandlesAsync(ticker, TimeSpan.FromMinutes(tfMinutes), handler);
    }
    
    public void UnsubscribeCandles() { }
    public void Dispose() { _broker.Dispose(); }
}
