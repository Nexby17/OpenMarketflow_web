using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>Обёртка одной сделки для отображения в DataGrid</summary>
public class TradeViewModel : BaseViewModel
{
    private readonly TradeRecord _record;

    public TradeViewModel(TradeRecord record)
    {
        _record = record;
    }

    public DateTime EntryTime => _record.EntryTime;
    public DateTime ExitTime => _record.ExitTime;
    public string Ticker => _record.Ticker;
    public string Direction => _record.DirectionText;
    public double EntryPrice => _record.EntryPrice;
    public double ExitPrice => _record.ExitPrice;
    public int Volume => _record.Volume;
    public double PnL => _record.PnL;
    public string Strategy => _record.Strategy;
    public string Comment => _record.Comment;

    public string PnLColor => PnL switch
    {
        > 0 => "#22C55E",
        < 0 => "#EF4444",
        _ => "#E4E4F0"
    };
}
