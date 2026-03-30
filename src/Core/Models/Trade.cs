namespace HedgeFund.Core.Models;

public class Trade
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public DateTime Timestamp { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public SignalDirection Direction { get; set; }
    public double Price { get; set; }
    public int Volume { get; set; }
    public string OrderId { get; set; } = string.Empty;
    public string BrokerTradeId { get; set; } = string.Empty;
    public double Commission { get; set; }
}
