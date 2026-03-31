using HedgeFund.Core.Models;

namespace HedgeFund.Core.Backtesting;

public class BacktestSettings
{
    public string Ticker { get; set; } = string.Empty;
    public double Commission { get; set; } = 1.0;
    public int Lots { get; set; } = 1;
    public double StopLoss { get; set; } = 0;
    public double TakeProfit { get; set; } = 0;
    public bool CloseEndOfDay { get; set; } = true;
}

public class BacktestResult
{
    public string StrategyName { get; set; } = string.Empty;
    public double TotalPnL { get; set; }
    public int TotalTrades { get; set; }
    public double WinRate { get; set; }
    public double ProfitFactor { get; set; }
    public double SharpeRatio { get; set; }
    public double MaxDrawdown { get; set; }
    public double MaxDrawdownPercent { get; set; }
    public double AvgWin { get; set; }
    public double AvgLoss { get; set; }
    public List<TradeRecord> Trades { get; set; } = new();
    public List<double> EquityCurve { get; set; } = new();
    public BacktestSettings Settings { get; set; } = new();
    public TimeSpan Duration { get; set; }
}
