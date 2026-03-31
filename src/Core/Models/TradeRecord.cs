namespace HedgeFund.Core.Models;

/// <summary>Запись о закрытой сделке (для истории)</summary>
public class TradeRecord
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public DateTime EntryTime { get; set; }
    public DateTime ExitTime { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public SignalDirection Direction { get; set; }
    public double EntryPrice { get; set; }
    public double ExitPrice { get; set; }
    public int Volume { get; set; }
    public double PnL { get; set; }
    public string Strategy { get; set; } = string.Empty;
    public string Comment { get; set; } = string.Empty;

    public bool IsWin => PnL > 0;

    public string DirectionText => Direction switch
    {
        SignalDirection.Buy => "Покупка",
        SignalDirection.Sell => "Продажа",
        _ => "—"
    };
}
