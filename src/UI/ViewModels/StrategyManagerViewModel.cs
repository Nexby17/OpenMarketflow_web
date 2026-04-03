using System.Collections.ObjectModel;
using System.Windows.Input;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel одной стратегии в менеджере</summary>
public class StrategyItemViewModel : BaseViewModel
{
    private string _name = string.Empty;
    public string Name { get => _name; set => SetField(ref _name, value); }

    private StrategyState _state;
    public StrategyState State
    {
        get => _state;
        set
        {
            if (SetField(ref _state, value))
            {
                OnPropertyChanged(nameof(StateText));
                OnPropertyChanged(nameof(StateColor));
                OnPropertyChanged(nameof(CanStart));
                OnPropertyChanged(nameof(CanStop));
                OnPropertyChanged(nameof(CanPause));
            }
        }
    }

    public string StateText => State switch
    {
        StrategyState.Running => "▶ Работает",
        StrategyState.Paused => "⏸ Пауза",
        _ => "⏹ Остановлена"
    };

    public string StateColor => State switch
    {
        StrategyState.Running => "#22C55E",
        StrategyState.Paused => "#F59E0B",
        _ => "#6B7280"
    };

    private string _ticker = string.Empty;
    public string Ticker { get => _ticker; set => SetField(ref _ticker, value); }

    private double _pnl;
    public double PnL
    {
        get => _pnl;
        set
        {
            if (SetField(ref _pnl, value))
                OnPropertyChanged(nameof(PnLColor));
        }
    }

    public string PnLColor => PnL switch { > 0 => "#22C55E", < 0 => "#EF4444", _ => "#E4E4F0" };

    private int _tradesCount;
    public int TradesCount { get => _tradesCount; set => SetField(ref _tradesCount, value); }

    private int _openPositions;
    public int OpenPositions { get => _openPositions; set => SetField(ref _openPositions, value); }

    private DateTime? _startTime;
    public DateTime? StartTime { get => _startTime; set => SetField(ref _startTime, value); }

    public string RunDuration
    {
        get
        {
            if (StartTime == null || State == StrategyState.Stopped) return "—";
            var d = DateTime.Now - StartTime.Value;
            return d.TotalHours >= 1 ? $"{d.Hours}ч {d.Minutes}м" : $"{d.Minutes}м {d.Seconds}с";
        }
    }

    public bool CanStart => State == StrategyState.Stopped;
    public bool CanStop => State != StrategyState.Stopped;
    public bool CanPause => State == StrategyState.Running;

    public void UpdateFrom(StrategyStatus status)
    {
        State = status.State;
        Ticker = status.Ticker;
        PnL = status.PnL;
        TradesCount = status.TradesCount;
        OpenPositions = status.OpenPositions;
        StartTime = status.StartTime;
        OnPropertyChanged(nameof(RunDuration));
    }
}

/// <summary>ViewModel менеджера стратегий</summary>
public class StrategyManagerViewModel : BaseViewModel
{
    public StrategyManagerViewModel()
    {
        StartStrategyCommand = new RelayCommand<StrategyItemViewModel>(StartStrategy);
        StopStrategyCommand = new RelayCommand<StrategyItemViewModel>(StopStrategy);
        PauseStrategyCommand = new RelayCommand<StrategyItemViewModel>(PauseStrategy);
        StopAndCloseCommand = new RelayCommand<StrategyItemViewModel>(StopAndClose);
        EmergencyStopAllCommand = new RelayCommand(EmergencyStopAll);

        // Инструменты для выбора
        Instruments = new ObservableCollection<string> { "SiM6", "SiU6", "SBER", "GAZP", "BRM6", "GDM6" };
        SelectedInstrument = "Si";

        // Актуальные стратегии
        Strategies.Add(new StrategyItemViewModel { Name = "PSAR Grid MM (1 мин)", Ticker = "SiM6" });
        Strategies.Add(new StrategyItemViewModel { Name = "PSAR+EMA Combo (5 мин)", Ticker = "SiM6" });
        Strategies.Add(new StrategyItemViewModel { Name = "VStop Pure (30 сек, бумага)", Ticker = "SiM6" });
    }

    // === Коллекции ===
    public ObservableCollection<StrategyItemViewModel> Strategies { get; } = new();
    public ObservableCollection<string> Instruments { get; }

    private string _selectedInstrument = string.Empty;
    public string SelectedInstrument { get => _selectedInstrument; set => SetField(ref _selectedInstrument, value); }

    // === Суммарная статистика ===
    private int _runningCount;
    public int RunningCount { get => _runningCount; set => SetField(ref _runningCount, value); }

    private double _totalPnL;
    public double TotalPnL
    {
        get => _totalPnL;
        set
        {
            if (SetField(ref _totalPnL, value))
                OnPropertyChanged(nameof(TotalPnLColor));
        }
    }
    public string TotalPnLColor => TotalPnL switch { > 0 => "#22C55E", < 0 => "#EF4444", _ => "#E4E4F0" };

    private int _totalTrades;
    public int TotalTrades { get => _totalTrades; set => SetField(ref _totalTrades, value); }

    // === События ===
    public event Func<string, string, Dictionary<string, double>, Task>? StartRequested;
    public event Func<string, Task>? StopRequested;
    public event Func<string, Task>? PauseRequested;
    public event Func<string, Task>? StopAndCloseRequested;
    public event Func<Task>? EmergencyStopRequested;

    // === Команды ===
    public ICommand StartStrategyCommand { get; }
    public ICommand StopStrategyCommand { get; }
    public ICommand PauseStrategyCommand { get; }
    public ICommand StopAndCloseCommand { get; }
    public ICommand EmergencyStopAllCommand { get; }

    // === Методы ===

    private async void StartStrategy(StrategyItemViewModel? s)
    {
        if (s == null || !s.CanStart) return;
        if (StartRequested != null)
            await StartRequested.Invoke(s.Name, SelectedInstrument, new Dictionary<string, double>());
        s.State = StrategyState.Running;
        s.Ticker = SelectedInstrument;
        s.StartTime = DateTime.Now;
        RecalcSummary();
    }

    private async void StopStrategy(StrategyItemViewModel? s)
    {
        if (s == null || !s.CanStop) return;
        if (StopRequested != null)
            await StopRequested.Invoke(s.Name);
        s.State = StrategyState.Stopped;
        RecalcSummary();
    }

    private async void PauseStrategy(StrategyItemViewModel? s)
    {
        if (s == null || !s.CanPause) return;
        if (PauseRequested != null)
            await PauseRequested.Invoke(s.Name);
        s.State = StrategyState.Paused;
        RecalcSummary();
    }

    private async void StopAndClose(StrategyItemViewModel? s)
    {
        if (s == null) return;
        if (StopAndCloseRequested != null)
            await StopAndCloseRequested.Invoke(s.Name);
        s.State = StrategyState.Stopped;
        s.OpenPositions = 0;
        RecalcSummary();
    }

    private async void EmergencyStopAll()
    {
        if (EmergencyStopRequested != null)
            await EmergencyStopRequested.Invoke();
        foreach (var s in Strategies)
        {
            s.State = StrategyState.Stopped;
            s.OpenPositions = 0;
        }
        RecalcSummary();
    }

    public void UpdateStrategyStatus(StrategyStatus status)
    {
        var item = Strategies.FirstOrDefault(s => s.Name == status.Name);
        if (item != null)
            item.UpdateFrom(status);
        RecalcSummary();
    }

    private void RecalcSummary()
    {
        RunningCount = Strategies.Count(s => s.State == StrategyState.Running);
        TotalPnL = Strategies.Sum(s => s.PnL);
        TotalTrades = Strategies.Sum(s => s.TradesCount);
    }
}
