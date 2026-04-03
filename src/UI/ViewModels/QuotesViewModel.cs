using System.Collections.ObjectModel;
using System.Windows.Input;
using System.Windows.Threading;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel вкладки Котировки</summary>
public class QuotesViewModel : BaseViewModel
{
    private readonly DispatcherTimer _refreshTimer;

    public QuotesViewModel()
    {
        Instruments = new ObservableCollection<string> { "SiM6", "SiU6", "SBER", "GAZP", "BRM6", "GDM6" };
        SelectedInstrument = "Si";

        SubscribeCommand = new RelayCommand(Subscribe);

        _refreshTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _refreshTimer.Tick += (_, _) => OnPropertyChanged(nameof(TimeSinceUpdate));
    }

    // === Инструменты ===
    public ObservableCollection<string> Instruments { get; }

    private string _selectedInstrument = string.Empty;
    public string SelectedInstrument
    {
        get => _selectedInstrument;
        set
        {
            if (SetField(ref _selectedInstrument, value))
                SubscribeRequested?.Invoke(value);
        }
    }

    // === Котировка ===
    private double _bid;
    public double Bid { get => _bid; set => SetField(ref _bid, value); }

    private double _ask;
    public double Ask { get => _ask; set => SetField(ref _ask, value); }

    private double _last;
    public double Last
    {
        get => _last;
        set
        {
            var old = _last;
            if (SetField(ref _last, value))
            {
                OnPropertyChanged(nameof(LastColor));
                _lastDirection = value >= old ? 1 : -1;
            }
        }
    }

    private int _lastDirection = 0;
    public string LastColor => _lastDirection >= 0 ? "#22C55E" : "#EF4444";

    private double _change;
    public double Change
    {
        get => _change;
        set
        {
            if (SetField(ref _change, value))
                OnPropertyChanged(nameof(ChangeColor));
        }
    }

    private double _changePercent;
    public double ChangePercent
    {
        get => _changePercent;
        set
        {
            if (SetField(ref _changePercent, value))
                OnPropertyChanged(nameof(ChangeColor));
        }
    }

    public string ChangeColor => Change >= 0 ? "#22C55E" : "#EF4444";

    private double _high;
    public double High { get => _high; set => SetField(ref _high, value); }

    private double _low;
    public double Low { get => _low; set => SetField(ref _low, value); }

    private double _open;
    public double Open { get => _open; set => SetField(ref _open, value); }

    private double _prevClose;
    public double PrevClose { get => _prevClose; set => SetField(ref _prevClose, value); }

    private long _volume;
    public long Volume { get => _volume; set => SetField(ref _volume, value); }

    private long _openInterest;
    public long OpenInterest { get => _openInterest; set => SetField(ref _openInterest, value); }

    private double _spread;
    public double Spread { get => _spread; set => SetField(ref _spread, value); }

    private DateTime _updateTime;
    public DateTime UpdateTime
    {
        get => _updateTime;
        set
        {
            if (SetField(ref _updateTime, value))
                OnPropertyChanged(nameof(TimeSinceUpdate));
        }
    }

    public string TimeSinceUpdate
    {
        get
        {
            if (_updateTime == default) return "—";
            var diff = DateTime.Now - _updateTime;
            return diff.TotalSeconds < 2 ? "сейчас" : $"{diff.TotalSeconds:F0} сек назад";
        }
    }

    // === События ===
    public event Action<string>? SubscribeRequested;

    // === Команды ===
    public ICommand SubscribeCommand { get; }

    // === Методы ===
    private void Subscribe()
    {
        SubscribeRequested?.Invoke(SelectedInstrument);
    }

    public void UpdateQuote(QuoteData q)
    {
        Bid = q.Bid;
        Ask = q.Ask;
        Last = q.Last;
        Change = q.Change;
        ChangePercent = q.ChangePercent;
        High = q.High;
        Low = q.Low;
        Open = q.Open;
        PrevClose = q.PrevClose;
        Volume = q.Volume;
        OpenInterest = q.OpenInterest;
        Spread = q.Spread;
        UpdateTime = q.Time;
    }

    public void StartRefresh() => _refreshTimer.Start();
    public void StopRefresh() => _refreshTimer.Stop();
}
