namespace HedgeFund.Core.Models;

/// <summary>Данные котировки инструмента</summary>
public class QuoteData
{
    public string Ticker { get; set; } = string.Empty;
    public double Bid { get; set; }
    public double Ask { get; set; }
    public double Last { get; set; }
    public double Change { get; set; }
    public double ChangePercent { get; set; }
    public double High { get; set; }
    public double Low { get; set; }
    public double Open { get; set; }
    public double PrevClose { get; set; }
    public long Volume { get; set; }
    public long OpenInterest { get; set; }
    public double Spread => Ask - Bid;
    public DateTime Time { get; set; }
}
