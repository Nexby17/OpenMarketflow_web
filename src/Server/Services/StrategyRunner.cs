using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;
using HedgeFund.Server.Models;

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

    // === Основной цикл стратегии ===

    private async Task RunStrategyLoopAsync(RunningStrategy running, CancellationToken ct)
    {
        // Ожидаем свечи через polling.
        // В реальном использовании здесь будет подписка через FinamConnector.SubscribeCandlesAsync
        // Пока используем polling с интервалом.

        var lastCandleTime = DateTime.MinValue;

        while (!ct.IsCancellationRequested)
        {
            try
            {
                // Проверяем, не на паузе ли сервис
                if (_tradingService.IsPaused)
                {
                    await Task.Delay(1000, ct);
                    continue;
                }

                if (!_tradingService.IsRunning)
                {
                    await Task.Delay(1000, ct);
                    continue;
                }

                // TODO: Получение свечей через коннектор
                // Пока это заглушка — реальная подписка будет через FinamConnector.SubscribeCandlesAsync
                // при интеграции с реальным брокером
                
                await Task.Delay(5000, ct); // Интервал polling
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Ошибка в цикле стратегии {Strategy}", running.Name);
                await Task.Delay(5000, ct);
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
    }
}
