using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>Обёртка позиции для отображения в DataGrid</summary>
public class PositionViewModel : BaseViewModel
{
    private readonly Position _position;
    private double _currentPrice;

    public PositionViewModel(Position position)
    {
        _position = position;
    }

    public string Ticker => _position.Ticker;
    public string Direction => _position.Direction.ToString();
    public double AveragePrice => _position.AveragePrice;
    public int TotalVolume => _position.TotalVolume;
    public int AveragingCount => _position.AveragingCount;

    public double CurrentPrice
    {
        get => _currentPrice;
        set
        {
            if (SetField(ref _currentPrice, value))
            {
                OnPropertyChanged(nameof(PnL));
                OnPropertyChanged(nameof(PnLColor));
            }
        }
    }

    public double PnL => _position.GetPnL(_currentPrice);
    
    public string PnLColor => PnL switch
    {
        > 0 => "#22C55E",  // green
        < 0 => "#EF4444",  // red
        _ => "#E4E4F0"     // neutral
    };
}
