using System.Collections.ObjectModel;
using System.Windows.Input;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel вкладки "Сделки" — история, фильтры, статистика</summary>
public class TradesViewModel : BaseViewModel
{
    private readonly ObservableCollection<TradeViewModel> _allTrades = new();

    public TradesViewModel()
    {
        FilterCommand = new RelayCommand(ApplyFilter);
        ResetFilterCommand = new RelayCommand(ResetFilter);

        FilteredTrades = new ObservableCollection<TradeViewModel>();
    }

    // === Коллекции ===

    public ObservableCollection<TradeViewModel> FilteredTrades { get; }

    // === Фильтры ===

    private DateTime? _filterFrom;
    public DateTime? FilterFrom
    {
        get => _filterFrom;
        set
        {
            if (SetField(ref _filterFrom, value))
                ApplyFilter();
        }
    }

    private DateTime? _filterTo;
    public DateTime? FilterTo
    {
        get => _filterTo;
        set
        {
            if (SetField(ref _filterTo, value))
                ApplyFilter();
        }
    }

    private string _filterTicker = string.Empty;
    public string FilterTicker
    {
        get => _filterTicker;
        set
        {
            if (SetField(ref _filterTicker, value))
                ApplyFilter();
        }
    }

    // === Суммарная статистика (по отфильтрованным) ===

    private double _totalPnL;
    public double TotalPnL
    {
        get => _totalPnL;
        set => SetField(ref _totalPnL, value);
    }

    private double _winRate;
    public double WinRate
    {
        get => _winRate;
        set => SetField(ref _winRate, value);
    }

    private int _totalTradesCount;
    public int TotalTradesCount
    {
        get => _totalTradesCount;
        set => SetField(ref _totalTradesCount, value);
    }

    public string TotalPnLColor => TotalPnL switch
    {
        > 0 => "#22C55E",
        < 0 => "#EF4444",
        _ => "#E4E4F0"
    };

    // === Команды ===

    public ICommand FilterCommand { get; }
    public ICommand ResetFilterCommand { get; }

    // === Методы ===

    /// <summary>Добавить сделку (вызывается из MainViewModel при закрытии позиции)</summary>
    public void AddTrade(TradeRecord record)
    {
        var vm = new TradeViewModel(record);
        _allTrades.Add(vm);
        ApplyFilter();
    }

    private void ApplyFilter()
    {
        FilteredTrades.Clear();

        foreach (var trade in _allTrades)
        {
            bool matchDate = true;
            bool matchTicker = true;

            if (FilterFrom.HasValue && trade.ExitTime < FilterFrom.Value)
                matchDate = false;
            if (FilterTo.HasValue && trade.ExitTime > FilterTo.Value.Date.AddDays(1))
                matchDate = false;
            if (!string.IsNullOrWhiteSpace(FilterTicker) &&
                !trade.Ticker.Contains(FilterTicker, StringComparison.OrdinalIgnoreCase))
                matchTicker = false;

            if (matchDate && matchTicker)
                FilteredTrades.Add(trade);
        }

        RecalcStats();
    }

    private void ResetFilter()
    {
        FilterFrom = null;
        FilterTo = null;
        FilterTicker = string.Empty;
        ApplyFilter();
    }

    private void RecalcStats()
    {
        TotalTradesCount = FilteredTrades.Count;
        TotalPnL = 0;
        int wins = 0;

        foreach (var t in FilteredTrades)
        {
            TotalPnL += t.PnL;
            if (t.PnL > 0) wins++;
        }

        WinRate = TotalTradesCount > 0 ? (double)wins / TotalTradesCount * 100.0 : 0;
        OnPropertyChanged(nameof(TotalPnLColor));
    }
}
