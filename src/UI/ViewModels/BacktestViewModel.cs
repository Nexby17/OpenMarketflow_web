using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Input;
using HedgeFund.Core.Backtesting;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel вкладки "Тестирование"</summary>
public class BacktestViewModel : BaseViewModel
{
    private CancellationTokenSource? _cts;

    public BacktestViewModel()
    {
        // Стратегии
        StrategyNames = new ObservableCollection<string>
        {
            "Momentum Breakout",
            "RSI Scalp",
            "Bollinger Bounce",
            "VWAP Reversion",
            "Scalping (EMA Cross + RSI)",
            "Breakout (BB + ATR)"
        };
        SelectedStrategyName = StrategyNames[0];

        // Инструменты
        Tickers = new ObservableCollection<string> { "SiM6", "SiU6", "SBER", "GAZP", "BRM6", "GDM6" };
        SelectedTicker = Tickers[0];

        // Таймфреймы
        Timeframes = new ObservableCollection<string> { "1 мин", "5 мин", "10 мин", "1 час", "День" };
        SelectedTimeframe = Timeframes[1]; // 5 мин по умолчанию

        // Даты
        DateFrom = DateTime.Today.AddMonths(-1);
        DateTo = DateTime.Today;

        // Параметры стратегии
        ChannelPeriod = 10;
        VolumeMult = 2.0;
        ExitSMA = 5;
        StopLoss = 0;
        TakeProfit = 200;
        Commission = 1.0;
        Lots = 1;
        CloseEndOfDay = true;

        // Коллекции
        BacktestTrades = new ObservableCollection<TradeRecord>();
        EquityPoints = new List<double>();

        // Команды
        RunTestCommand = new RelayCommand(_ =>
        {
            _ = Task.Run(async () =>
            {
                try { await RunTestAsync(); }
                catch (Exception ex)
                {
                    App.Current?.Dispatcher.Invoke(() =>
                    {
                        StatusText = $"❌ Ошибка: {ex.Message}";
                        IsRunning = false;
                    });
                }
            });
        }, _ => !IsRunning);
        CancelTestCommand = new RelayCommand(_ => CancelTest(), _ => IsRunning);
    }

    // === Параметры выбора ===

    public ObservableCollection<string> StrategyNames { get; }

    private string _selectedStrategyName = string.Empty;
    public string SelectedStrategyName
    {
        get => _selectedStrategyName;
        set => SetField(ref _selectedStrategyName, value);
    }

    public ObservableCollection<string> Tickers { get; }

    private string _selectedTicker = string.Empty;
    public string SelectedTicker
    {
        get => _selectedTicker;
        set => SetField(ref _selectedTicker, value);
    }

    public ObservableCollection<string> Timeframes { get; }

    private string _selectedTimeframe = string.Empty;
    public string SelectedTimeframe
    {
        get => _selectedTimeframe;
        set => SetField(ref _selectedTimeframe, value);
    }

    private DateTime _dateFrom;
    public DateTime DateFrom
    {
        get => _dateFrom;
        set => SetField(ref _dateFrom, value);
    }

    private DateTime _dateTo;
    public DateTime DateTo
    {
        get => _dateTo;
        set => SetField(ref _dateTo, value);
    }

    // === Параметры стратегии ===

    private int _channelPeriod;
    public int ChannelPeriod
    {
        get => _channelPeriod;
        set => SetField(ref _channelPeriod, value);
    }

    private double _volumeMult;
    public double VolumeMult
    {
        get => _volumeMult;
        set => SetField(ref _volumeMult, value);
    }

    private int _exitSMA;
    public int ExitSMA
    {
        get => _exitSMA;
        set => SetField(ref _exitSMA, value);
    }

    private double _stopLoss;
    public double StopLoss
    {
        get => _stopLoss;
        set => SetField(ref _stopLoss, value);
    }

    private double _takeProfit;
    public double TakeProfit
    {
        get => _takeProfit;
        set => SetField(ref _takeProfit, value);
    }

    private double _commission;
    public double Commission
    {
        get => _commission;
        set => SetField(ref _commission, value);
    }

    private int _lots;
    public int Lots
    {
        get => _lots;
        set => SetField(ref _lots, value);
    }

    private bool _closeEndOfDay;
    public bool CloseEndOfDay
    {
        get => _closeEndOfDay;
        set => SetField(ref _closeEndOfDay, value);
    }

    // === Состояние ===

    private bool _isRunning;
    public bool IsRunning
    {
        get => _isRunning;
        set
        {
            if (SetField(ref _isRunning, value))
                CommandManager.InvalidateRequerySuggested();
        }
    }

    private string _statusText = "Готов";
    public string StatusText
    {
        get => _statusText;
        set => SetField(ref _statusText, value);
    }

    private double _progress;
    public double Progress
    {
        get => _progress;
        set => SetField(ref _progress, value);
    }

    private bool _isIndeterminate;
    public bool IsIndeterminate
    {
        get => _isIndeterminate;
        set => SetField(ref _isIndeterminate, value);
    }

    // === Результаты ===

    private double _resultPnL;
    public double ResultPnL
    {
        get => _resultPnL;
        set
        {
            if (SetField(ref _resultPnL, value))
                OnPropertyChanged(nameof(ResultPnLColor));
        }
    }

    public string ResultPnLColor => ResultPnL switch
    {
        > 0 => "#22C55E",
        < 0 => "#EF4444",
        _ => "#E4E4F0"
    };

    private int _resultTrades;
    public int ResultTrades
    {
        get => _resultTrades;
        set => SetField(ref _resultTrades, value);
    }

    private double _resultWinRate;
    public double ResultWinRate
    {
        get => _resultWinRate;
        set => SetField(ref _resultWinRate, value);
    }

    private double _resultProfitFactor;
    public double ResultProfitFactor
    {
        get => _resultProfitFactor;
        set => SetField(ref _resultProfitFactor, value);
    }

    private double _resultSharpe;
    public double ResultSharpe
    {
        get => _resultSharpe;
        set => SetField(ref _resultSharpe, value);
    }

    private double _resultMaxDD;
    public double ResultMaxDD
    {
        get => _resultMaxDD;
        set => SetField(ref _resultMaxDD, value);
    }

    private double _resultAvgWin;
    public double ResultAvgWin
    {
        get => _resultAvgWin;
        set => SetField(ref _resultAvgWin, value);
    }

    private double _resultAvgLoss;
    public double ResultAvgLoss
    {
        get => _resultAvgLoss;
        set => SetField(ref _resultAvgLoss, value);
    }

    public ObservableCollection<TradeRecord> BacktestTrades { get; }

    public List<double> EquityPoints { get; private set; }

    /// <summary>Строит строку Points для Polyline из EquityPoints</summary>
    public string EquityPointsGeometry
    {
        get
        {
            if (EquityPoints.Count < 2) return string.Empty;

            double min = EquityPoints.Min();
            double max = EquityPoints.Max();
            double range = max - min;
            if (range < 0.01) range = 1;

            const double canvasW = 680;
            const double canvasH = 200;
            const double padY = 10;

            // Прореживание если точек слишком много
            int step = Math.Max(1, EquityPoints.Count / 2000);
            var points = new List<(double x, double y)>();

            for (int i = 0; i < EquityPoints.Count; i += step)
            {
                double x = (double)i / (EquityPoints.Count - 1) * canvasW;
                double y = canvasH - padY - ((EquityPoints[i] - min) / range) * (canvasH - 2 * padY);
                points.Add((x, y));
            }

            // Добавляем последнюю точку
            if (points.Count > 0)
            {
                int last = EquityPoints.Count - 1;
                double xLast = canvasW;
                double yLast = canvasH - padY - ((EquityPoints[last] - min) / range) * (canvasH - 2 * padY);
                points.Add((xLast, yLast));
            }

            return string.Join(" ", points.Select(p => $"{p.x:F1},{p.y:F1}"));
        }
    }

    public double ZeroLineY
    {
        get
        {
            if (EquityPoints.Count < 2) return 100;
            double min = EquityPoints.Min();
            double max = EquityPoints.Max();
            double range = max - min;
            if (range < 0.01) range = 1;
            const double canvasH = 200;
            const double padY = 10;
            return canvasH - padY - ((0 - min) / range) * (canvasH - 2 * padY);
        }
    }

    // === Команды ===

    public ICommand RunTestCommand { get; }
    public ICommand CancelTestCommand { get; }

    // Событие для ApplyToTrading
    public event Action<BacktestViewModel>? ApplyToTradingRequested;

    private RelayCommand? _applyToTradingCommand;
    public ICommand ApplyToTradingCommand => _applyToTradingCommand ??=
        new RelayCommand(() => ApplyToTradingRequested?.Invoke(this));

    // === Логика ===

    private async Task RunTestAsync()
    {
        if (IsRunning) return;

        IsRunning = true;
        IsIndeterminate = true;
        StatusText = "Загрузка данных с MOEX...";
        Progress = 0;

        _cts = new CancellationTokenSource();

        try
        {
            int interval = SelectedTimeframe switch
            {
                "1 мин" => 1,
                "5 мин" => 5,
                "10 мин" => 10,
                "1 час" => 60,
                "День" => 24,
                _ => 5
            };

            var progressReporter = new Progress<string>(msg => StatusText = msg);

            // Загрузка данных
            var candles = await MoexDataLoader.LoadCandlesAsync(
                SelectedTicker, interval, DateFrom, DateTo, progressReporter, _cts.Token);

            if (candles.Count == 0)
            {
                StatusText = "⚠ Нет данных для указанного периода";
                return;
            }

            StatusText = $"Тестирование на {candles.Count} свечах...";
            IsIndeterminate = false;
            Progress = 50;

            // Создаём стратегию
            IStrategy strategy = CreateStrategy();

            var settings = new BacktestSettings
            {
                Ticker = SelectedTicker,
                Commission = Commission,
                Lots = Lots,
                StopLoss = StopLoss,
                TakeProfit = TakeProfit,
                CloseEndOfDay = CloseEndOfDay
            };

            // Запускаем бэктест в фоновом потоке
            var engine = new BacktestEngine();
            var result = await Task.Run(() => engine.Run(strategy, candles, settings), _cts.Token);

            Progress = 100;

            // Обновляем результаты через Dispatcher (коллекции только из UI потока)
            Application.Current?.Dispatcher.Invoke(() =>
            {
                ResultPnL = result.TotalPnL;
                ResultTrades = result.TotalTrades;
                ResultWinRate = result.WinRate;
                ResultProfitFactor = double.IsInfinity(result.ProfitFactor) ? 999.99 : result.ProfitFactor;
                ResultSharpe = result.SharpeRatio;
                ResultMaxDD = result.MaxDrawdown;
                ResultAvgWin = result.AvgWin;
                ResultAvgLoss = result.AvgLoss;

                // Equity Curve
                EquityPoints = result.EquityCurve;
                OnPropertyChanged(nameof(EquityPointsGeometry));
                OnPropertyChanged(nameof(ZeroLineY));

                // Сделки
                BacktestTrades.Clear();
                foreach (var trade in result.Trades)
                    BacktestTrades.Add(trade);

                StatusText = $"✓ Готово за {result.Duration.TotalSeconds:F1} сек | " +
                             $"{candles.Count} свечей, {result.TotalTrades} сделок";
            });
        }
        catch (OperationCanceledException)
        {
            StatusText = "Тест отменён";
        }
        catch (Exception ex)
        {
            StatusText = $"✗ Ошибка: {ex.Message}";
        }
        finally
        {
            IsRunning = false;
            IsIndeterminate = false;
            _cts?.Dispose();
            _cts = null;
        }
    }

    private void CancelTest()
    {
        _cts?.Cancel();
    }

    private IStrategy CreateStrategy()
    {
        return SelectedStrategyName switch
        {
            "Momentum Breakout" => new MomentumBreakoutStrategy(
                ChannelPeriod, VolumeMult, ExitSMA, TakeProfit, StopLoss),
            "RSI Scalp" => new RsiScalpStrategy(),
            "Bollinger Bounce" => new BollingerBounceStrategy(),
            "VWAP Reversion" => new VwapReversionStrategy(),
            "Scalping (EMA Cross + RSI)" => new ScalpingStrategy(),
            "Breakout (BB + ATR)" => new BreakoutStrategy(),
            _ => new MomentumBreakoutStrategy(ChannelPeriod, VolumeMult, ExitSMA, TakeProfit, StopLoss)
        };
    }
}
