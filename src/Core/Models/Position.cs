namespace HedgeFund.Core.Models;

public class PositionEntry
{
    public DateTime Timestamp { get; set; }
    public double Price { get; set; }
    public int Volume { get; set; }
    public string Comment { get; set; } = string.Empty;
}

public class Position
{
    public string Ticker { get; set; } = string.Empty;
    public SignalDirection Direction { get; set; }
    
    /// <summary>Все входы (первоначальный + усреднения)</summary>
    public List<PositionEntry> Entries { get; set; } = new();
    
    /// <summary>Суммарный объём позиции</summary>
    public int TotalVolume => Entries.Sum(e => e.Volume);
    
    /// <summary>Средневзвешенная цена входа</summary>
    public double AveragePrice => TotalVolume == 0 ? 0 
        : Entries.Sum(e => e.Price * e.Volume) / TotalVolume;
    
    /// <summary>Количество усреднений (без первого входа)</summary>
    public int AveragingCount => Math.Max(0, Entries.Count - 1);
    
    /// <summary>Текущий PnL в пунктах (на 1 лот)</summary>
    public double GetPnL(double currentPrice)
    {
        if (TotalVolume == 0) return 0;
        
        double diff = currentPrice - AveragePrice;
        if (Direction == SignalDirection.Sell) diff = -diff;
        
        return diff * TotalVolume;
    }
    
    /// <summary>PnL положительный?</summary>
    public bool IsInProfit(double currentPrice) => GetPnL(currentPrice) > 0;
    
    /// <summary>Позиция открыта?</summary>
    public bool IsOpen => TotalVolume > 0;
    
    public DateTime OpenTime => Entries.FirstOrDefault()?.Timestamp ?? DateTime.MinValue;
}
