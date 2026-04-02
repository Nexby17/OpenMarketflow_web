using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>Одна строка стакана для отображения</summary>
public class OrderBookRowViewModel : BaseViewModel
{
    private double _price;
    public double Price { get => _price; set => SetField(ref _price, value); }

    private long _bidVolume;
    public long BidVolume { get => _bidVolume; set => SetField(ref _bidVolume, value); }

    private long _askVolume;
    public long AskVolume { get => _askVolume; set => SetField(ref _askVolume, value); }

    private string _ourInfo = string.Empty;
    public string OurInfo { get => _ourInfo; set => SetField(ref _ourInfo, value); }

    private bool _isLastPrice;
    public bool IsLastPrice
    {
        get => _isLastPrice;
        set
        {
            if (SetField(ref _isLastPrice, value))
                OnPropertyChanged(nameof(RowBackground));
        }
    }

    private bool _hasOurOrder;
    public bool HasOurOrder
    {
        get => _hasOurOrder;
        set
        {
            if (SetField(ref _hasOurOrder, value))
                OnPropertyChanged(nameof(RowBackground));
        }
    }

    public string RowBackground => IsLastPrice ? "#33FFFFFF" : HasOurOrder ? "#33F59E0B" : "Transparent";
    public string BidColor => BidVolume > 0 ? "#22C55E" : "Transparent";
    public string AskColor => AskVolume > 0 ? "#EF4444" : "Transparent";

    // Ширина баров (для визуализации, 0-100)
    private double _bidBarWidth;
    public double BidBarWidth { get => _bidBarWidth; set => SetField(ref _bidBarWidth, value); }

    private double _askBarWidth;
    public double AskBarWidth { get => _askBarWidth; set => SetField(ref _askBarWidth, value); }
}

/// <summary>ViewModel стакана + графика</summary>
public class OrderBookViewModel : BaseViewModel
{
    private readonly DispatcherTimer _refreshTimer;

    public OrderBookViewModel()
    {
        Instruments = new ObservableCollection<string> { "Si", "BR", "GOLD", "SBER", "GAZP" };
        SelectedInstrument = "Si";

        Timeframes = new ObservableCollection<string> { "1m", "5m", "15m", "1h", "4h", "D" };
        SelectedTimeframe = "5m";

        SubscribeCommand = new RelayCommand(Subscribe);
        ChangeTimeframeCommand = new RelayCommand(ChangeTimeframe);

        _refreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(200) };
        _refreshTimer.Tick += (_, _) => OnPropertyChanged(nameof(RefreshTick));
    }

    // === Инструменты ===
    public ObservableCollection<string> Instruments { get; }

    private string _selectedInstrument = string.Empty;
    public string SelectedInstrument { get => _selectedInstrument; set => SetField(ref _selectedInstrument, value); }

    // === Стакан ===
    public ObservableCollection<OrderBookRowViewModel> OrderBookRows { get; } = new();

    private double _lastPrice;
    public double LastPrice { get => _lastPrice; set => SetField(ref _lastPrice, value); }

    private double _bestBid;
    public double BestBid { get => _bestBid; set => SetField(ref _bestBid, value); }

    private double _bestAsk;
    public double BestAsk { get => _bestAsk; set => SetField(ref _bestAsk, value); }

    private double _spreadValue;
    public double SpreadValue { get => _spreadValue; set => SetField(ref _spreadValue, value); }

    // === Позиция ===
    private string _posDirection = "—";
    public string PosDirection { get => _posDirection; set => SetField(ref _posDirection, value); }

    private double _posAvgPrice;
    public double PosAvgPrice { get => _posAvgPrice; set => SetField(ref _posAvgPrice, value); }

    private int _posVolume;
    public int PosVolume { get => _posVolume; set => SetField(ref _posVolume, value); }

    private double _posPnL;
    public double PosPnL
    {
        get => _posPnL;
        set
        {
            if (SetField(ref _posPnL, value))
                OnPropertyChanged(nameof(PosPnLColor));
        }
    }

    public string PosPnLColor => PosPnL switch { > 0 => "#22C55E", < 0 => "#EF4444", _ => "#E4E4F0" };

    // === График ===
    public ObservableCollection<string> Timeframes { get; }

    private string _selectedTimeframe = string.Empty;
    public string SelectedTimeframe
    {
        get => _selectedTimeframe;
        set
        {
            if (SetField(ref _selectedTimeframe, value))
                TimeframeChangeRequested?.Invoke(value);
        }
    }

    // Данные для рендеринга свечей (Canvas рисует в code-behind)
    private List<ClusterCandle> _candles = new();
    public List<ClusterCandle> Candles
    {
        get => _candles;
        set
        {
            _candles = value;
            OnPropertyChanged(nameof(Candles));
            OnPropertyChanged(nameof(CandleCount));
        }
    }

    public int CandleCount => _candles.Count;

    // Параметры отображения
    private double _chartZoom = 1.0;
    public double ChartZoom
    {
        get => _chartZoom;
        set
        {
            _chartZoom = Math.Clamp(value, 0.2, 5.0);
            OnPropertyChanged(nameof(ChartZoom));
        }
    }

    private double _chartScrollOffset;
    public double ChartScrollOffset
    {
        get => _chartScrollOffset;
        set => SetField(ref _chartScrollOffset, value);
    }

    // Для перерисовки
    public int RefreshTick => Environment.TickCount;

    // === События ===
    public event Action<string>? SubscribeBookRequested;
    public event Action<string>? TimeframeChangeRequested;

    // === Команды ===
    public ICommand SubscribeCommand { get; }
    public ICommand ChangeTimeframeCommand { get; }

    // === Методы ===

    private void Subscribe()
    {
        SubscribeBookRequested?.Invoke(SelectedInstrument);
    }

    private void ChangeTimeframe()
    {
        TimeframeChangeRequested?.Invoke(SelectedTimeframe);
    }

    public void UpdateOrderBook(OrderBookSnapshot snapshot)
    {
        LastPrice = snapshot.LastPrice;
        BestBid = snapshot.BestBid;
        BestAsk = snapshot.BestAsk;
        SpreadValue = snapshot.BestAsk - snapshot.BestBid;

        long maxVol = snapshot.Entries.Count > 0
            ? Math.Max(snapshot.Entries.Max(e => e.BidVolume), snapshot.Entries.Max(e => e.AskVolume))
            : 1;

        OrderBookRows.Clear();
        foreach (var entry in snapshot.Entries.OrderByDescending(e => e.Price))
        {
            var row = new OrderBookRowViewModel
            {
                Price = entry.Price,
                BidVolume = entry.BidVolume,
                AskVolume = entry.AskVolume,
                IsLastPrice = Math.Abs(entry.Price - snapshot.LastPrice) < 0.01,
                HasOurOrder = entry.OurOrderVolume.HasValue,
                OurInfo = FormatOurInfo(entry),
                BidBarWidth = maxVol > 0 ? (double)entry.BidVolume / maxVol * 100 : 0,
                AskBarWidth = maxVol > 0 ? (double)entry.AskVolume / maxVol * 100 : 0
            };
            OrderBookRows.Add(row);
        }
    }

    public void UpdatePosition(string direction, double avgPrice, int volume, double pnl)
    {
        PosDirection = direction;
        PosAvgPrice = avgPrice;
        PosVolume = volume;
        PosPnL = pnl;
    }

    public void UpdateCandles(List<ClusterCandle> candles)
    {
        Candles = candles;
    }

    public void StartRefresh() => _refreshTimer.Start();
    public void StopRefresh() => _refreshTimer.Stop();

    private static string FormatOurInfo(OrderBookEntry e)
    {
        var parts = new List<string>();
        if (e.OurOrderVolume.HasValue)
            parts.Add($"[{e.OurOrderDirection} {e.OurOrderVolume}]");
        if (e.OurPositionVolume.HasValue)
            parts.Add($"Поз: {e.OurPositionVolume} ({e.OurPositionPnL:+0;-0})");
        return string.Join(" ", parts);
    }
}
