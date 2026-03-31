namespace HedgeFund.Server.Models;

/// <summary>Текущий статус торгового сервера</summary>
public class ServerStatus
{
    public bool IsRunning { get; set; }
    public bool IsPaused { get; set; }
    public bool IsConnectedToBroker { get; set; }
    public double Balance { get; set; }
    public double Equity { get; set; }
    public double TodayPnL { get; set; }
    public double TotalPnL { get; set; }
    public int OpenPositions { get; set; }
    public int TodayTrades { get; set; }
    public List<string> ActiveStrategies { get; set; } = new();
    public List<PositionEvent> Positions { get; set; } = new();
    public DateTime ServerTime { get; set; }
    public string BrokerStatus { get; set; } = string.Empty;
}
