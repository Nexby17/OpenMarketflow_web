namespace HedgeFund.Core.Models;

public enum SignalDirection
{
    Buy,
    Sell,
    None
}

public enum SignalSource
{
    Strategy,       // Сигнал от стратегии (вход)
    Averaging,      // Сигнал на усреднение (добавка при отрицательном PnL)
    Grid,           // Grid fill (докупка/продажа уровня)
    TakeProfit,     // Grid level TP
    Exit            // Сигнал на выход (PnL > 0)
}

public class Signal
{
    public DateTime Timestamp { get; set; }
    public string Ticker { get; set; } = string.Empty;
    public SignalDirection Direction { get; set; }
    public SignalSource Source { get; set; }
    public string StrategyName { get; set; } = string.Empty;
    public double Price { get; set; }
    public int Volume { get; set; } = 1;
    public string Comment { get; set; } = string.Empty;
}
