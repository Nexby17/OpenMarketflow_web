using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>Обёртка ордера для DataGrid с редактированием</summary>
public class OrderViewModel : BaseViewModel
{
    public string Id { get; set; } = string.Empty;
    public string BrokerOrderId { get; set; } = string.Empty;
    public DateTime Timestamp { get; set; }

    private string _ticker = string.Empty;
    public string Ticker { get => _ticker; set => SetField(ref _ticker, value); }

    private string _direction = string.Empty;
    public string Direction { get => _direction; set => SetField(ref _direction, value); }

    private string _type = string.Empty;
    public string Type { get => _type; set => SetField(ref _type, value); }

    private double _price;
    public double Price
    {
        get => _price;
        set
        {
            if (SetField(ref _price, value))
                OnPropertyChanged(nameof(IsModified));
        }
    }

    private int _volume;
    public int Volume
    {
        get => _volume;
        set
        {
            if (SetField(ref _volume, value))
                OnPropertyChanged(nameof(IsModified));
        }
    }

    private int _filledVolume;
    public int FilledVolume { get => _filledVolume; set => SetField(ref _filledVolume, value); }

    private string _status = string.Empty;
    public string Status { get => _status; set => SetField(ref _status, value); }

    // Для отслеживания изменений
    public double OriginalPrice { get; set; }
    public int OriginalVolume { get; set; }
    public bool IsModified => Price != OriginalPrice || Volume != OriginalVolume;

    public string StatusColor => Status switch
    {
        "Active" or "Активная" => "#F59E0B",
        "Filled" or "Исполнена" => "#22C55E",
        "PartiallyFilled" or "Частично" => "#3B82F6",
        "Cancelled" or "Отменена" => "#6B7280",
        "Rejected" or "Отклонена" => "#EF4444",
        _ => "#E4E4F0"
    };

    public static OrderViewModel FromOrder(Order order) => new()
    {
        Id = order.Id,
        BrokerOrderId = order.BrokerOrderId,
        Timestamp = order.Timestamp,
        Ticker = order.Ticker,
        Direction = order.Direction == SignalDirection.Buy ? "Buy" : "Sell",
        Type = order.Type.ToString(),
        Price = order.Price,
        Volume = order.Volume,
        FilledVolume = order.FilledVolume,
        Status = order.Status.ToString(),
        OriginalPrice = order.Price,
        OriginalVolume = order.Volume
    };
}
