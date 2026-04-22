using HedgeFund.Core.Models;

namespace HedgeFund.Core.Connectors;

/// <summary>
/// Единый интерфейс для подключения к брокеру.
/// Каждая реализация (Finam, QUIK, Transaq) полностью изолирована.
/// </summary>
public interface IConnector : IDisposable
{
    string Name { get; }
    bool IsConnected { get; }
    
    // === Подключение ===
    Task<bool> ConnectAsync(string token);
    Task DisconnectAsync();
    
    // === Данные ===
    Task<double> GetBalanceAsync();
    Task<Candle[]> GetCandlesAsync(string ticker, int tfMinutes, int count);
    
    // === Торговля ===
    Task<string?> PlaceOrderAsync(string ticker, string direction, string type, double price, int volume, string comment);
    Task<bool> CancelOrderAsync(string orderId);
    
    // === Подписки ===
    void SubscribeCandles(string ticker, int tfMinutes, Action<Candle> handler);
    void UnsubscribeCandles();
}

/// <summary>
/// Менеджер коннекторов. Хранит активный коннектор, переключает между ними.
/// </summary>
public class ConnectorManager : IDisposable
{
    private IConnector? _active;
    private readonly object _lock = new();
    
    public IConnector? Active
    {
        get { lock (_lock) return _active; }
        set { lock (_lock) _active = value; }
    }
    
    public string ActiveName => Active?.Name ?? "None";
    public bool IsConnected => Active?.IsConnected ?? false;
    public string[] AvailableConnectors => new[] { "Finam", "QUIK", "Transaq" };
    
    public async Task DisconnectAsync()
    {
        IConnector? old;
        lock (_lock) { old = _active; _active = null; }
        if (old != null)
        {
            try { old.UnsubscribeCandles(); } catch { }
            try { await old.DisconnectAsync(); } catch { }
            try { old.Dispose(); } catch { }
        }
    }
    
    public void Dispose() { _ = DisconnectAsync(); }
}
