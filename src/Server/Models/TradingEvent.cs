namespace HedgeFund.Server.Models;

/// <summary>Событие исполнения сделки</summary>
public class TradeEvent
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public DateTime Time { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public string Direction { get; set; } = string.Empty;
    public double Price { get; set; }
    public int Volume { get; set; }
    public double PnL { get; set; }
    public string Strategy { get; set; } = string.Empty;
    public string Comment { get; set; } = string.Empty;
}

/// <summary>Событие сигнала от стратегии</summary>
public class SignalEvent
{
    public DateTime Time { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public string Direction { get; set; } = string.Empty;
    public string Strategy { get; set; } = string.Empty;
    public double Price { get; set; }
    public string Reason { get; set; } = string.Empty;
}

/// <summary>Обновление позиции</summary>
public class PositionEvent
{
    public string Ticker { get; set; } = string.Empty;
    public string Direction { get; set; } = string.Empty;
    public double EntryPrice { get; set; }
    public int Volume { get; set; }
    public double CurrentPrice { get; set; }
    public double PnL { get; set; }
}

/// <summary>Запрос одобрения сделки (режим подтверждения)</summary>
public class TradeApprovalEvent
{
    public string TradeId { get; set; } = Guid.NewGuid().ToString();
    public string Ticker { get; set; } = string.Empty;
    public string Direction { get; set; } = string.Empty;
    public double Price { get; set; }
    public int Volume { get; set; }
    public string Strategy { get; set; } = string.Empty;
    public string Reason { get; set; } = string.Empty;
    public DateTime Deadline { get; set; }
}

/// <summary>Результат бэктеста</summary>
public class BacktestResultEvent
{
    public string StrategyName { get; set; } = string.Empty;
    public string Ticker { get; set; } = string.Empty;
    public double TotalPnL { get; set; }
    public int TotalTrades { get; set; }
    public double WinRate { get; set; }
    public double ProfitFactor { get; set; }
    public double SharpeRatio { get; set; }
    public double MaxDrawdown { get; set; }
    public List<double> EquityCurve { get; set; } = new();
    public TimeSpan Duration { get; set; }
}
