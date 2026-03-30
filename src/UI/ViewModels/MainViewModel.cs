using System.Collections.ObjectModel;
using System.Windows.Input;
using System.Windows.Threading;
using HedgeFund.Core;
using HedgeFund.Core.Averaging;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;

namespace HedgeFund.UI.ViewModels;

public class MainViewModel : BaseViewModel
{
    private TradingEngine? _engine;
    private readonly DispatcherTimer _uiTimer;

    public MainViewModel()
    {
        // Команды
        StartCommand = new RelayCommand(Start, () => !IsRunning);
        StopCommand = new RelayCommand(Stop, () => IsRunning);
        ToggleMartingaleCommand = new RelayCommand(ToggleMartingale);
        ToggleStopLossCommand = new RelayCommand(ToggleStopLoss);

        // Таймер обновления UI (200мс)
        _uiTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(200) };
        _uiTimer.Tick += (_, _) => RefreshUI();

        // Значения по умолчанию
        Brokers = new ObservableCollection<string> { "Альфа-Инвестиции", "Финам (Transaq)" };
        SelectedBroker = Brokers[0];

        Instruments = new ObservableCollection<string> { "SBER", "GAZP", "Si", "BR", "GOLD", "SPYF" };
        SelectedInstrument = Instruments[0];

        Strategies = new ObservableCollection<string> { "Scalping (EMA Cross + RSI)", "Breakout (BB + ATR)", "Spread Arbitrage" };
        SelectedStrategy = Strategies[0];

        StopLossModes = new ObservableCollection<string> { "Пункты", "%" };
        SelectedStopLossMode = StopLossModes[0];
    }

    // === Свойства привязки ===

    private bool _isRunning;
    public bool IsRunning
    {
        get => _isRunning;
        set
        {
            if (SetField(ref _isRunning, value))
            {
                OnPropertyChanged(nameof(StatusText));
                OnPropertyChanged(nameof(StatusColor));
                CommandManager.InvalidateRequerySuggested();
            }
        }
    }

    public string StatusText => IsRunning ? "● РАБОТАЕТ" : "○ ОСТАНОВЛЕН";
    public string StatusColor => IsRunning ? "#22C55E" : "#EF4444";

    // --- Выбор брокера/инструмента/стратегии ---
    public ObservableCollection<string> Brokers { get; }
    private string _selectedBroker = string.Empty;
    public string SelectedBroker
    {
        get => _selectedBroker;
        set => SetField(ref _selectedBroker, value);
    }

    public ObservableCollection<string> Instruments { get; }
    private string _selectedInstrument = string.Empty;
    public string SelectedInstrument
    {
        get => _selectedInstrument;
        set => SetField(ref _selectedInstrument, value);
    }

    public ObservableCollection<string> Strategies { get; }
    private string _selectedStrategy = string.Empty;
    public string SelectedStrategy
    {
        get => _selectedStrategy;
        set => SetField(ref _selectedStrategy, value);
    }

    // --- Усреднение ---
    private bool _isMartingale;
    public bool IsMartingale
    {
        get => _isMartingale;
        set
        {
            if (SetField(ref _isMartingale, value))
            {
                OnPropertyChanged(nameof(AveragingModeText));
                _engine?.ToggleMartingale();
            }
        }
    }
    public string AveragingModeText => IsMartingale ? "Мартингейл" : "Фикс (1 лот)";

    private int _baseLotSize = 1;
    public int BaseLotSize
    {
        get => _baseLotSize;
        set => SetField(ref _baseLotSize, value);
    }

    private int _maxAveraging = 0;
    public int MaxAveraging
    {
        get => _maxAveraging;
        set
        {
            if (SetField(ref _maxAveraging, value))
                _engine?.SetMaxAveraging(value);
        }
    }
    public string MaxAveragingText => MaxAveraging <= 0 ? "Без лимита" : MaxAveraging.ToString();

    // --- Стоп-лосс ---
    private bool _stopLossEnabled;
    public bool StopLossEnabled
    {
        get => _stopLossEnabled;
        set
        {
            if (SetField(ref _stopLossEnabled, value))
                _engine?.ToggleStopLoss();
        }
    }

    private double _stopLossValue = 100;
    public double StopLossValue
    {
        get => _stopLossValue;
        set
        {
            if (SetField(ref _stopLossValue, value))
                UpdateStopLoss();
        }
    }

    public ObservableCollection<string> StopLossModes { get; }
    private string _selectedStopLossMode = string.Empty;
    public string SelectedStopLossMode
    {
        get => _selectedStopLossMode;
        set
        {
            if (SetField(ref _selectedStopLossMode, value))
                UpdateStopLoss();
        }
    }

    // --- Баланс ---
    private double _balance;
    public double Balance
    {
        get => _balance;
        set => SetField(ref _balance, value);
    }

    // --- Позиции ---
    public ObservableCollection<PositionViewModel> Positions { get; } = new();

    // --- Лог ---
    public ObservableCollection<string> LogEntries { get; } = new();

    // === Команды ===

    public ICommand StartCommand { get; }
    public ICommand StopCommand { get; }
    public ICommand ToggleMartingaleCommand { get; }
    public ICommand ToggleStopLossCommand { get; }

    // === Логика ===

    private void Start()
    {
        if (IsRunning) return;

        var settings = new AveragingSettings
        {
            Enabled = true,
            Mode = IsMartingale ? AveragingMode.Martingale : AveragingMode.Fixed,
            BaseLotSize = BaseLotSize,
            MaxAveragingCount = MaxAveraging,
            StopLossEnabled = StopLossEnabled,
            StopLossPoints = SelectedStopLossMode == "Пункты" ? StopLossValue : 0,
            StopLossPercent = SelectedStopLossMode == "%" ? StopLossValue : 0,
            UsePercentStopLoss = SelectedStopLossMode == "%"
        };

        // TODO: создать реальный коннектор на основе SelectedBroker
        // Пока используем заглушку
        AddLog("Робот запущен. Брокер: " + SelectedBroker + ", Инструмент: " + SelectedInstrument);
        AddLog("Стратегия: " + SelectedStrategy);
        AddLog($"Усреднение: {AveragingModeText}, лимит: {MaxAveragingText}");
        if (StopLossEnabled)
            AddLog($"Стоп-лосс: {StopLossValue} {SelectedStopLossMode}");

        IsRunning = true;
        _uiTimer.Start();
    }

    private void Stop()
    {
        if (!IsRunning) return;

        _engine?.Stop();
        IsRunning = false;
        _uiTimer.Stop();
        AddLog("Робот остановлен.");
    }

    private void ToggleMartingale()
    {
        IsMartingale = !IsMartingale;
        AddLog($"Режим усреднения: {AveragingModeText}");
    }

    private void ToggleStopLoss()
    {
        StopLossEnabled = !StopLossEnabled;
        AddLog($"Стоп-лосс: {(StopLossEnabled ? "ВКЛ" : "ВЫКЛ")}");
    }

    private void UpdateStopLoss()
    {
        bool usePercent = SelectedStopLossMode == "%";
        _engine?.SetStopLoss(StopLossValue, usePercent);
    }

    private void RefreshUI()
    {
        // Обновление позиций и лога из движка
        if (_engine != null)
        {
            // Синхронизация лога
            while (LogEntries.Count < _engine.EventLog.Count)
                LogEntries.Add(_engine.EventLog[LogEntries.Count]);
        }
    }

    public void AddLog(string message)
    {
        var entry = $"[{DateTime.Now:HH:mm:ss}] {message}";
        LogEntries.Add(entry);
    }
}
