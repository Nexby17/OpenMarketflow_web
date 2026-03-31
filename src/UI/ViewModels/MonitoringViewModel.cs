using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel вкладки "Мониторинг" — метрики, equity curve, позиции</summary>
public class MonitoringViewModel : BaseViewModel
{
    private readonly DispatcherTimer _refreshTimer;

    public MonitoringViewModel()
    {
        EquityPoints = new ObservableCollection<double>();
        OpenPositions = new ObservableCollection<PositionViewModel>();

        // Таймер обновления метрик (каждые 500мс)
        _refreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        _refreshTimer.Tick += (_, _) => OnPropertyChanged(nameof(EquityPointsGeometry));
    }

    // === Метрики ===

    private double _balance;
    public double Balance
    {
        get => _balance;
        set => SetField(ref _balance, value);
    }

    private double _equity;
    public double Equity
    {
        get => _equity;
        set => SetField(ref _equity, value);
    }

    private double _pnlToday;
    public double PnLToday
    {
        get => _pnlToday;
        set
        {
            if (SetField(ref _pnlToday, value))
                OnPropertyChanged(nameof(PnLTodayColor));
        }
    }

    private double _pnlTotal;
    public double PnLTotal
    {
        get => _pnlTotal;
        set
        {
            if (SetField(ref _pnlTotal, value))
                OnPropertyChanged(nameof(PnLTotalColor));
        }
    }

    private double _winRate;
    public double WinRate
    {
        get => _winRate;
        set => SetField(ref _winRate, value);
    }

    private double _profitFactor;
    public double ProfitFactor
    {
        get => _profitFactor;
        set => SetField(ref _profitFactor, value);
    }

    private double _sharpeRatio;
    public double SharpeRatio
    {
        get => _sharpeRatio;
        set => SetField(ref _sharpeRatio, value);
    }

    private int _openPositionsCount;
    public int OpenPositionsCount
    {
        get => _openPositionsCount;
        set => SetField(ref _openPositionsCount, value);
    }

    private int _tradesToday;
    public int TradesToday
    {
        get => _tradesToday;
        set => SetField(ref _tradesToday, value);
    }

    // === Цвета PnL ===

    public string PnLTodayColor => PnLToday switch
    {
        > 0 => "#22C55E",
        < 0 => "#EF4444",
        _ => "#E4E4F0"
    };

    public string PnLTotalColor => PnLTotal switch
    {
        > 0 => "#22C55E",
        < 0 => "#EF4444",
        _ => "#E4E4F0"
    };

    // === Equity Curve ===

    private double _initialBalance;
    public double InitialBalance
    {
        get => _initialBalance;
        set => SetField(ref _initialBalance, value);
    }

    public ObservableCollection<double> EquityPoints { get; }

    /// <summary>Строит строку Points для Polyline из EquityPoints</summary>
    public string EquityPointsGeometry
    {
        get
        {
            if (EquityPoints.Count < 2) return string.Empty;

            double min = double.MaxValue;
            double max = double.MinValue;
            foreach (var p in EquityPoints)
            {
                if (p < min) min = p;
                if (p > max) max = p;
            }

            double range = max - min;
            if (range < 0.01) range = 1;

            const double canvasW = 780;
            const double canvasH = 200;
            const double padY = 10;

            double stepX = canvasW / (EquityPoints.Count - 1);
            var parts = new System.Text.StringBuilder();

            for (int i = 0; i < EquityPoints.Count; i++)
            {
                double x = i * stepX;
                double y = canvasH - padY - ((EquityPoints[i] - min) / range) * (canvasH - 2 * padY);
                if (i > 0) parts.Append(' ');
                parts.Append($"{x:F1},{y:F1}");
            }

            return parts.ToString();
        }
    }

    /// <summary>Y-координата линии начального баланса на Canvas</summary>
    public double InitialBalanceY
    {
        get
        {
            if (EquityPoints.Count < 2) return 100;

            double min = double.MaxValue;
            double max = double.MinValue;
            foreach (var p in EquityPoints)
            {
                if (p < min) min = p;
                if (p > max) max = p;
            }

            double range = max - min;
            if (range < 0.01) range = 1;

            const double canvasH = 200;
            const double padY = 10;

            return canvasH - padY - ((InitialBalance - min) / range) * (canvasH - 2 * padY);
        }
    }

    // === Позиции ===

    public ObservableCollection<PositionViewModel> OpenPositions { get; }

    // === Управление таймером ===

    public void StartRefresh() => _refreshTimer.Start();
    public void StopRefresh() => _refreshTimer.Stop();

    /// <summary>Добавить точку equity и обновить график</summary>
    public void AddEquityPoint(double value)
    {
        EquityPoints.Add(value);
        OnPropertyChanged(nameof(EquityPointsGeometry));
        OnPropertyChanged(nameof(InitialBalanceY));
    }

    /// <summary>Обновить метрики из данных сделок</summary>
    public void UpdateMetrics(double balance, double equity, double pnlToday, double pnlTotal,
        double winRate, double profitFactor, double sharpeRatio, int openPositions, int tradesToday)
    {
        Balance = balance;
        Equity = equity;
        PnLToday = pnlToday;
        PnLTotal = pnlTotal;
        WinRate = winRate;
        ProfitFactor = profitFactor;
        SharpeRatio = sharpeRatio;
        OpenPositionsCount = openPositions;
        TradesToday = tradesToday;
    }
}
