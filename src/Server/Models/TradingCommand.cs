namespace HedgeFund.Server.Models;

/// <summary>Тип команды от клиента к серверу</summary>
public enum CommandType
{
    Start,
    Stop,
    Pause,
    Resume,
    EmergencyStop,
    Approve,
    Reject,
    UpdateParams,
    RunBacktest
}

/// <summary>Команда управления торговлей</summary>
public class TradingCommand
{
    public CommandType Type { get; set; }
    public string StrategyName { get; set; } = string.Empty;
    public string Ticker { get; set; } = string.Empty;
    public Dictionary<string, double> Parameters { get; set; } = new();
    public string? TradeId { get; set; }
}
