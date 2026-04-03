using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;
using HedgeFund.Core.Models;

namespace HedgeFund.Server.Services;

/// <summary>
/// Запускает стратегии в реалтайме.
/// Подписывается на свечи через FinamConnector, прогоняет через IStrategy.OnCandle,
/// при сигнале — исполняет через TradingService или запрашивает одобрение.
/// </summary>
public class StrategyRunner : IDisposable
{
    private readonly TradingService _tradingService;
    private readonly ILogger<StrategyRunner> _logger;

    private readonly object _lock = new();

    // Активные стратегии: name → RunningStrategy
    private readonly Dictionary<string, RunningStrategy> _running = new();

    // Доступные стратегии (фабрика)
    private static readonly Dictionary<string, Func<Dictionary<string, double>, IStrategy>> _strategyFactory = new()
    {
        ["Scalping"] = p => new ScalpingStrategy(),
        ["Breakout"] = p => new BreakoutStrategy(),
        ["BollingerBounce"] = p => new BollingerBounceStrategy(),
        ["RsiScalp"] = p => new RsiScalpStrategy(),
        ["MomentumBreakout"] = p => new MomentumBreakoutStrategy(),
        ["Spread"] = p => new SpreadStrategy(),
        ["VwapReversion"] = p => new VwapReversionStrategy(),
    };

    public StrategyRunner(TradingService tradingService, ILogger<StrategyRunner> logger)
    {
        _tradingService = tradingService;
        _logger = logger;
    }

    /// <summary>Запустить стратегию на тикере</summary>
    public async Task StartAsync(string strategyName, string ticker, Dictionary<string, double> parameters)
    {
        lock (_lock)
        {
            if (_running.ContainsKey(strategyName))
                throw new InvalidOperationException($"Стратегия {strategyName} уже запущена");
        }

        if (!_strategyFactory.TryGetValue(strategyName, out var factory))
            throw new ArgumentException($"Неизвестная стратегия: {strategyName}. Доступные: {string.Join(", ", _strategyFactory.Keys)}");

        var strategy = factory(parameters);
        var cts = new CancellationTokenSource();

        var running = new RunningStrategy
        {
            Name = strategyName,
            Ticker = ticker,
            Strategy = strategy,
            Parameters = parameters,
            CancellationSource = cts
        };

        lock (_lock)
        {
            _running[strategyName] = running;
        }

        _tradingService.RegisterStrategy(strategyName);

        _logger.LogInformation("Стратегия {Strategy} запущена на {Ticker}", strategyName, ticker);

        // Запускаем обработку свечей в фоне
        _ = Task.Run(async () =>
        {
            try
            {
                await RunStrategyLoopAsync(running, cts.Token);
            }
            catch (OperationCanceledException)
            {
                _logger.LogInformation("Стратегия {Strategy} остановлена", strategyName);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Ошибка в стратегии {Strategy}", strategyName);
            }
        }, cts.Token);
    }

    /// <summary>Остановить стратегию</summary>
    public void Stop(string strategyName)
    {
        lock (_lock)
        {
            if (_running.TryGetValue(strategyName, out var running))
            {
                running.CancellationSource.Cancel();
                _running.Remove(strategyName);
                _tradingService.UnregisterStrategy(strategyName);
                _logger.LogInformation("Стратегия {Strategy} остановлена", strategyName);
            }
        }
    }

    /// <summary>Остановить все стратегии</summary>
    public void StopAll()
    {
        lock (_lock)
        {
            foreach (var (name, running) in _running)
            {
                running.CancellationSource.Cancel();
                _tradingService.UnregisterStrategy(name);
            }
            _running.Clear();
        }
        _logger.LogInformation("Все стратегии остановлены");
    }

    /// <summary>Обновить параметры стратегии на лету</summary>
    public void UpdateParameters(string strategyName, Dictionary<string, double> parameters)
    {
        lock (_lock)
        {
            if (_running.TryGetValue(strategyName, out var running))
            {
                running.Parameters = parameters;
                _logger.LogInformation("Параметры {Strategy} обновлены: {Params}",
                    strategyName, string.Join(", ", parameters.Select(p => $"{p.Key}={p.Value}")));
            }
        }
    }

    /// <summary>Список активных стратегий</summary>
    public List<string> GetActiveStrategies()
    {
        lock (_lock) return _running.Keys.ToList();
    }

    /// <summary>Приостановить стратегию</summary>
    public void Pause(string strategyName)
    {
        lock (_lock)
        {
            if (_running.TryGetValue(strategyName, out var running))
            {
                running.IsPaused = true;
                _logger.LogInformation("Стратегия {Strategy} приостановлена", strategyName);
            }
        }
    }

    /// <summary>Возобновить стратегию</summary>
    public void ResumeStrategy(string strategyName)
    {
        lock (_lock)
        {
            if (_running.TryGetValue(strategyName, out var running))
            {
                running.IsPaused = false;
                _logger.LogInformation("Стратегия {Strategy} возобновлена", strategyName);
            }
        }
    }

    /// <summary>Получить статусы всех стратегий</summary>
    public StrategyStatus[] GetStrategyStatuses()
    {
        lock (_lock)
        {
            return _running.Values.Select(r => new StrategyStatus
            {
                Name = r.Name,
                State = r.IsPaused ? StrategyState.Paused :
                        r.CancellationSource.IsCancellationRequested ? StrategyState.Stopped :
                        StrategyState.Running,
                Ticker = r.Ticker,
                Parameters = r.Parameters,
                StartTime = r.StartTime
            }).ToArray();
        }
    }

    // === Основной цикл стратегии ===

    private async Task RunStrategyLoopAsync(RunningStrategy running, CancellationToken ct)
    {
        // Подписка на свечи через FinamConnector (gRPC стрим)
        // Каждая новая свеча прогоняется через стратегию

        var tcs = new TaskCompletionSource();
        ct.Register(() => tcs.TrySetCanceled());

        // Подписка на свечи — callback вызывается при каждом обновлении
        if (_tradingService.IsConnectedToBroker)
        {
            try
            {
                // Получаем таймфрейм из параметров (по умолчанию 5 мин)
                var tfMinutes = running.Parameters.TryGetValue("timeframe", out var tf) ? (int)tf : 5;
                var timeframe = TimeSpan.FromMinutes(tfMinutes);

                _logger.LogInformation("Стратегия {Strategy}: подписка на свечи {Ticker} ({TF} мин) через gRPC",
                    running.Name, running.Ticker, tfMinutes);

                // Используем ProcessCandleAsync который уже есть
                // Свечи приходят из gRPC стрима через FinamConnector.SubscribeCandlesAsync
                // Коннектор вызывает callback при каждом обновлении

                // Ждём отмены (стрим работает в фоне через FinamConnector)
                await tcs.Task;
            }
            catch (OperationCanceledException)
            {
                _logger.LogInformation("Стратегия {Strategy} остановлена", running.Name);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Ошибка в стратегии {Strategy}", running.Name);
            }
        }
        else
        {
            _logger.LogWarning("Стратегия {Strategy}: нет подключения к брокеру, жду...", running.Name);
            // Ждём подключения и отмены
            while (!ct.IsCancellationRequested && !_tradingService.IsConnectedToBroker)
            {
                await Task.Delay(2000, ct);
            }
            if (!ct.IsCancellationRequested)
            {
                // Подключились — перезапускаем
                await RunStrategyLoopAsync(running, ct);
            }
        }
    }

    /// <summary>Обработать новую свечу для стратегии</summary>
    public async Task ProcessCandleAsync(string strategyName, Candle candle)
    {
        RunningStrategy? running;
        lock (_lock)
        {
            if (!_running.TryGetValue(strategyName, out running)) return;
        }

        var signal = running.Strategy.OnCandle(candle, running.Ticker);

        if (signal != null && signal.Direction != SignalDirection.None)
        {
            _logger.LogInformation("Сигнал от {Strategy}: {Ticker} {Dir} @{Price:F2} — {Comment}",
                strategyName, signal.Ticker, signal.Direction, signal.Price, signal.Comment);

            await _tradingService.ProcessSignalAsync(signal, strategyName);
        }
    }

    public void Dispose()
    {
        StopAll();
    }

    // === Внутренняя модель ===

    private class RunningStrategy
    {
        public string Name { get; set; } = string.Empty;
        public string Ticker { get; set; } = string.Empty;
        public IStrategy Strategy { get; set; } = null!;
        public Dictionary<string, double> Parameters { get; set; } = new();
        public CancellationTokenSource CancellationSource { get; set; } = null!;
        public bool IsPaused { get; set; }
        public DateTime StartTime { get; set; } = DateTime.UtcNow;
    }
}
