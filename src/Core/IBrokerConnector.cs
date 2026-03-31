using HedgeFund.Core.Models;

namespace HedgeFund.Core;

/// <summary>
/// Единый интерфейс для подключения к брокерам.
/// Реализации: AlfaConnector, FinamConnector
/// </summary>
public interface IBrokerConnector : IDisposable
{
    string BrokerName { get; }
    bool IsConnected { get; }

    // === Подключение ===
    Task<bool> ConnectAsync(string login, string password);
    Task DisconnectAsync();

    // === Маркетдата ===
    /// <summary>Подписаться на свечи</summary>
    Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle);
    
    /// <summary>Подписаться на стакан (Level 2)</summary>
    Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk);
    
    /// <summary>Получить историю свечей</summary>
    Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to);

    // === Торговля ===
    Task<Order> PlaceOrderAsync(Order order);
    Task<bool> CancelOrderAsync(string orderId);
    Task<Order[]> GetActiveOrdersAsync();

    // === Позиции ===
    Task<Position[]> GetPositionsAsync();
    Task<double> GetBalanceAsync();

    // === События ===
    event Action<Trade> OnTrade;
    event Action<Order> OnOrderUpdate;
    event Action<string> OnError;
    event Action<bool> OnConnectionChanged;
}
