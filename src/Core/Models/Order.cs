namespace HedgeFund.Core.Models;

public enum OrderStatus
{
    Pending,
    Active,
    Filled,
    PartiallyFilled,
    Cancelled,
    Rejected
}

public enum OrderType
{
    Market,
    Limit,
    StopLoss,
    TakeProfit
}

public class Order
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public DateTime Timestamp { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public SignalDirection Direction { get; set; }
    public OrderType Type { get; set; }
    public OrderStatus Status { get; set; } = OrderStatus.Pending;
    public double Price { get; set; }
    public int Volume { get; set; }
    public int FilledVolume { get; set; }
    public string BrokerOrderId { get; set; } = string.Empty;
    public string Comment { get; set; } = string.Empty;
}
