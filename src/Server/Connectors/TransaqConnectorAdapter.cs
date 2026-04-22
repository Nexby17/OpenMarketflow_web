using HedgeFund.Core.Connectors;
using HedgeFund.Core.Models;

namespace HedgeFund.Server.Connectors;

/// <summary>
/// Transaq адаптер — заглушка (порт 39000 недоступен с VPS).
/// </summary>
public class TransaqConnectorAdapter : IConnector
{
    public string Name => "Transaq";
    public bool IsConnected => false;
    
    public Task<bool> ConnectAsync(string token) => Task.FromResult(false);
    public Task DisconnectAsync() => Task.CompletedTask;
    public Task<double> GetBalanceAsync() => Task.FromResult(0.0);
    public Task<Candle[]> GetCandlesAsync(string ticker, int tfMinutes, int count) => Task.FromResult(Array.Empty<Candle>());
    public Task<string?> PlaceOrderAsync(string ticker, string direction, string type, double price, int volume, string comment) => Task.FromResult<string?>(null);
    public Task<bool> CancelOrderAsync(string orderId) => Task.FromResult(false);
    public void SubscribeCandles(string ticker, int tfMinutes, Action<Candle> handler) { }
    public void UnsubscribeCandles() { }
    public void Dispose() { }
}
