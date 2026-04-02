namespace HedgeFund.Core.Models;

/// <summary>Одна строка стакана</summary>
public class OrderBookEntry
{
    public double Price { get; set; }
    public long BidVolume { get; set; }
    public long AskVolume { get; set; }
    /// <summary>Наш ордер на этом уровне (null если нет)</summary>
    public int? OurOrderVolume { get; set; }
    /// <summary>Направление нашего ордера</summary>
    public string? OurOrderDirection { get; set; }
    /// <summary>Наша позиция на этом уровне</summary>
    public int? OurPositionVolume { get; set; }
    /// <summary>PnL нашей позиции на этом уровне</summary>
    public double? OurPositionPnL { get; set; }
}

/// <summary>Полный стакан</summary>
public class OrderBookSnapshot
{
    public string Ticker { get; set; } = string.Empty;
    public DateTime Time { get; set; }
    public List<OrderBookEntry> Entries { get; set; } = new();
    public double LastPrice { get; set; }
    public double BestBid { get; set; }
    public double BestAsk { get; set; }
}

/// <summary>Свеча с кластерным объёмом</summary>
public class ClusterCandle
{
    public DateTime Timestamp { get; set; }
    public double Open { get; set; }
    public double High { get; set; }
    public double Low { get; set; }
    public double Close { get; set; }
    public long Volume { get; set; }
    /// <summary>Распределение объёма по ценам внутри свечи</summary>
    public Dictionary<double, long> VolumeProfile { get; set; } = new();

    public bool IsBullish => Close >= Open;
}
