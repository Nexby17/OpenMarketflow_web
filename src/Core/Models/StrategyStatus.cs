namespace HedgeFund.Core.Models;

public enum StrategyState
{
    Stopped,
    Running,
    Paused
}

/// <summary>Статус стратегии для отображения в UI</summary>
public class StrategyStatus
{
    public string Name { get; set; } = string.Empty;
    public StrategyState State { get; set; } = StrategyState.Stopped;
    public string Ticker { get; set; } = string.Empty;
    public double PnL { get; set; }
    public int TradesCount { get; set; }
    public DateTime? StartTime { get; set; }
    public Dictionary<string, double> Parameters { get; set; } = new();
    public int OpenPositions { get; set; }
    public int OpenOrders { get; set; }

    public string StateText => State switch
    {
        StrategyState.Running => "▶ Работает",
        StrategyState.Paused => "⏸ Пауза",
        _ => "⏹ Остановлена"
    };

    public string StateColor => State switch
    {
        StrategyState.Running => "#22C55E",
        StrategyState.Paused => "#F59E0B",
        _ => "#6B7280"
    };
}
