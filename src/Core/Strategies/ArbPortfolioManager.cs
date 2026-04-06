using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Управляет портфелем из нескольких арбитражных пар.
/// Создаёт и конфигурирует стратегии по каждому инструменту.
/// Обрабатывает сигналы и передаёт ордера брокеру.
/// </summary>
public class ArbPortfolioManager
{
    private readonly Dictionary<string, SpotFuturesArbStrategy> _strategies = new();
    private readonly List<ArbSignal> _signalLog = new();

    public IReadOnlyDictionary<string, SpotFuturesArbStrategy> Strategies => _strategies;
    public IReadOnlyList<ArbSignal> SignalLog => _signalLog;

    /// <summary>Общий PnL по всем стратегиям</summary>
    public double TotalPnL => _strategies.Values.Sum(s => s.TotalPnL);

    /// <summary>Общее количество сделок</summary>
    public int TotalTrades => _strategies.Values.Sum(s => s.TotalTrades);

    public event Action<ArbSignal>? OnSignal;
    public event Action<string>? OnLog;

    /// <summary>
    /// Инициализация портфеля с оптимальными параметрами из бэктеста.
    /// Валидировано: Walk-forward 20/20, Monte Carlo 100% profit, Sensitivity ✅
    /// </summary>
    public void InitializeDefaultPortfolio()
    {
        // ROSN — лидер по доходности (+27.1% net)
        // 1 фьюч. контракт = 100 акций, 1 лот акций = 1 акция
        AddStrategy(new SpotFuturesArbStrategy
        {
            Name = "ARB_ROSN",
            SpotTicker = "ROSN",
            FuturesTicker = "",
            LotSize = 100,          // акций в 1 фьюч. контракте
            SharesPerSpotLot = 1,   // акций в 1 лоте акции на МосБирже
            FuturesGO = 7500,       // ГО за 1 контракт (~руб)
            Window = 15,
            EntryZ = 1.0,
            ExitZ = 0.0,
            StopZ = 3.5,
            RollDaysBeforeExpiry = 20,
            FutLots = 1,
            SpotLots = 0,           // авто: 1х100/1 = 100 лотов акций
        });

        // TATN — лучший Sharpe. 1 контракт = 100 акций, 1 лот = 1 акция
        AddStrategy(new SpotFuturesArbStrategy
        {
            Name = "ARB_TATN",
            SpotTicker = "TATN",
            FuturesTicker = "",
            LotSize = 100,
            SharesPerSpotLot = 1,
            FuturesGO = 5000,
            Window = 15,
            EntryZ = 1.0,
            ExitZ = 0.5,
            StopZ = 3.5,
            RollDaysBeforeExpiry = 15,
            FutLots = 1,
        });

        // GAZP — стабильный, 0 roll losses. 1 контракт = 100 акций, 1 лот = 10 акций
        AddStrategy(new SpotFuturesArbStrategy
        {
            Name = "ARB_GAZP",
            SpotTicker = "GAZP",
            FuturesTicker = "",
            LotSize = 100,
            SharesPerSpotLot = 10,
            FuturesGO = 4000,
            Window = 20,
            EntryZ = 1.5,
            ExitZ = 0.0,
            StopZ = 3.5,
            RollDaysBeforeExpiry = 20,
            FutLots = 1,
        });

        // SBER — надёжный. 1 контракт = 100 акций, 1 лот = 10 акций
        AddStrategy(new SpotFuturesArbStrategy
        {
            Name = "ARB_SBER",
            SpotTicker = "SBER",
            FuturesTicker = "",
            LotSize = 100,
            SharesPerSpotLot = 10,
            FuturesGO = 6000,
            Window = 15,
            EntryZ = 1.5,
            ExitZ = -0.5,
            StopZ = 3.5,
            RollDaysBeforeExpiry = 10,
            FutLots = 1,
        });

        // ALRS — 100% WR. 1 контракт = 100 акций, 1 лот = 10 акций
        AddStrategy(new SpotFuturesArbStrategy
        {
            Name = "ARB_ALRS",
            SpotTicker = "ALRS",
            FuturesTicker = "",
            LotSize = 100,
            SharesPerSpotLot = 10,
            FuturesGO = 2000,
            Window = 15,
            EntryZ = 2.5,
            ExitZ = 0.5,
            StopZ = 3.0,
            RollDaysBeforeExpiry = 5,
            FutLots = 1,
        });

        Log($"Портфель инициализирован: {_strategies.Count} стратегий");
    }

    /// <summary>Масштабировать лоты под капитал</summary>
    public void ScaleToCapital(double totalCapital)
    {
        // Аллокация по STRATEGIES.md
        var allocations = new Dictionary<string, double>
        {
            ["ROSN"] = 0.30,
            ["TATN"] = 0.25,
            ["GAZP"] = 0.25,
            ["SBER"] = 0.15,
            ["ALRS"] = 0.05,
        };

        foreach (var (key, strategy) in _strategies)
        {
            var spot = strategy.SpotTicker;
            if (!allocations.ContainsKey(spot)) continue;

            double alloc = totalCapital * allocations[spot];
            // Грубая оценка: стоимость пары ≈ spot_price * lot_size * 1.15 (акция + ГО)
            // Точнее рассчитается при подключении к бирже
            // Пока ставим консервативно
            double estimatedPairCost = 15000; // ~15K за пару (средняя)
            strategy.BaseLots = Math.Max(1, (int)(alloc * 0.5 / estimatedPairCost));

            Log($"{strategy.Name}: капитал {alloc:N0} ₽, базовый размер {strategy.BaseLots} лотов");
        }
    }

    public void AddStrategy(SpotFuturesArbStrategy strategy)
    {
        _strategies[strategy.Name] = strategy;
    }

    /// <summary>
    /// Обработать обновление цен для конкретной пары.
    /// Вызывается из коннектора при получении новой свечи/тика.
    /// </summary>
    public ArbSignal? ProcessPriceUpdate(string spotTicker, double spotPrice, 
                                          double futuresPrice, DateTime timestamp)
    {
        var strategy = _strategies.Values.FirstOrDefault(s => s.SpotTicker == spotTicker);
        if (strategy == null) return null;

        var signal = strategy.OnPriceUpdate(spotPrice, futuresPrice, timestamp);
        if (signal != null)
        {
            _signalLog.Add(signal);
            OnSignal?.Invoke(signal);
            Log(signal.ToString());
        }

        return signal;
    }

    /// <summary>Переключить все стратегии в режим</summary>
    public void SetMode(StrategyMode mode)
    {
        foreach (var s in _strategies.Values)
            s.Mode = mode;
        Log($"Все стратегии → {mode}");
    }

    /// <summary>Переключить конкретную стратегию</summary>
    public void SetMode(string strategyName, StrategyMode mode)
    {
        if (_strategies.TryGetValue(strategyName, out var s))
        {
            s.Mode = mode;
            Log($"{strategyName} → {mode}");
        }
    }

    /// <summary>Принудительно закрыть все позиции</summary>
    public List<ArbSignal> CloseAll(DateTime timestamp)
    {
        var signals = new List<ArbSignal>();
        foreach (var s in _strategies.Values)
        {
            if (s.CurrentPosition.IsOpen)
            {
                var signal = s.ForceClose(s.LastSpotPrice, s.LastFuturesPrice, timestamp);
                if (signal != null)
                {
                    signals.Add(signal);
                    _signalLog.Add(signal);
                    OnSignal?.Invoke(signal);
                }
            }
        }
        Log($"Закрыто {signals.Count} позиций");
        return signals;
    }

    /// <summary>Текущий статус портфеля</summary>
    public string GetStatus()
    {
        var lines = new List<string>
        {
            "═══ АРБИТРАЖНЫЙ ПОРТФЕЛЬ ═══",
            $"Общий PnL: {TotalPnL:+#,##0;-#,##0;0} ₽ | Сделок: {TotalTrades}",
            ""
        };

        foreach (var s in _strategies.Values)
        {
            string posInfo = s.CurrentPosition.IsOpen
                ? $"[{s.CurrentPosition.Direction}] x{s.CurrentPosition.Lots}"
                : "[нет позиции]";

            lines.Add($"{s.Name}: Z={s.LastZScore:F2} Basis={s.LastBasisAnnual:F1}% " +
                      $"{posInfo} PnL={s.TotalPnL:+#,##0;-#,##0;0} Mode={s.Mode}");
        }

        return string.Join("\n", lines);
    }

    private void Log(string message)
    {
        OnLog?.Invoke($"[ArbPortfolio] {message}");
    }
}
