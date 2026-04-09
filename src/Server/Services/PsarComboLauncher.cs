using HedgeFund.Core;
using HedgeFund.Core.Averaging;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Лаунчер PSAR+EMA Combo стратегии на Финам.
/// Управление: Start() / Stop() / Pause()
/// TF: 5 мин. Инструмент: SiM6 (или актуальный SI фьючерс).
/// </summary>
public class PsarComboLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly PsarEmaComboStrategy _strategy;
    private readonly string _ticker;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(5);
    private CancellationTokenSource? _cts;

    public PsarEmaComboStrategy Strategy => _strategy;
    public bool IsConnected => _broker.IsConnected;

    /// <summary>
    /// Создать лаунчер.
    /// finamToken — торговый токен Финам (access_token).
    /// ticker — тикер фьючерса, например "SiM6".
    /// accountId — если пусто, берётся первый счёт из токена.
    /// </summary>
    public PsarComboLauncher(string finamToken, string ticker = "SiM6", string accountId = "")
    {
        _ticker = ticker;
        _broker = new FinamConnector();
        _strategy = new PsarEmaComboStrategy(new PsarEmaComboStrategy.Config
        {
            // Long
            L_SarStart = 0.03,
            L_SarStep = 0.02,
            L_SarMax = 0.2,
            L_EmaPeriod = 300,
            L_GridStep = 30,
            L_GridSpread = 60,
            // Short
            S_SarStart = 0.005,
            S_SarStep = 0.01,
            S_SarMax = 0.2,
            S_EmaPeriod = 300,
            S_GridStep = 20,
            S_GridSpread = 70,
            // Общие
            MaxGrid = 30,
            MinProfitPerLot = 28,
            Commission = 0.60,
            BaseLots = 1,
            // ATR фильтр
            AtrPeriod = 14,
            AtrFilter = 0.5,
            AtrFilterEnabled = true,
            // Динамические лоты
            LotStepProfit = 1000.0,
            MaxDynamicLots = 5
        });

        _broker.OnError += msg => Console.WriteLine($"[BROKER ERROR] {msg}");
        _broker.OnTrade += trade => Console.WriteLine($"[TRADE] {trade.Direction} {trade.Volume}x @ {trade.Price:F0}");

        _ = ConnectAndWarm(finamToken, accountId);
    }

    private async Task ConnectAndWarm(string token, string accountId)
    {
        Console.WriteLine($"[LAUNCHER] Подключение к Финам...");
        bool ok = await _broker.ConnectAsync(token, accountId);
        if (!ok)
        {
            Console.WriteLine("[LAUNCHER] ❌ Не удалось подключиться к Финам!");
            return;
        }
        Console.WriteLine($"[LAUNCHER] ✅ Подключён. Счёт привязан.");

        // Прогрев: загружаем исторические 5-мин свечи за последние 3 дня
        Console.WriteLine($"[LAUNCHER] 📐 Прогрев индикаторов ({_ticker}, 5-мин)...");
        var from = DateTime.UtcNow.AddDays(-5);
        var to = DateTime.UtcNow;
        var history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);

        if (history.Length > 0)
        {
            Console.WriteLine($"[LAUNCHER] Загружено {history.Length} исторических свечей для прогрева");
            foreach (var candle in history)
            {
                _strategy.OnCandle(candle, _ticker); // прогрев без торговли (Mode=Paused)
            }
            Console.WriteLine($"[LAUNCHER] ✅ Индикаторы прогреты. Ожидаю команду 'Старт'.");
        }
        else
        {
            Console.WriteLine($"[LAUNCHER] ⚠️ Нет исторических данных для прогрева");
        }

        // Подписка на 5-мин свечи
        await _broker.SubscribeCandlesAsync(_ticker, _timeframe, OnNewCandle);
        Console.WriteLine($"[LAUNCHER] 📡 Подписка на {_ticker} 5-мин активна");
    }

    private void OnNewCandle(Candle candle)
    {
        var signal = _strategy.OnCandle(candle, _ticker);

        if (signal != null && signal.Direction != SignalDirection.None)
        {
            Console.WriteLine($"[SIGNAL] {signal.Comment} | Dir={signal.Direction} Vol={signal.Volume} @ {signal.Price:F0}");

            // Исполнение через брокера
            if (_strategy.Mode == PsarEmaComboStrategy.StrategyMode.Running
                || _strategy.Mode == PsarEmaComboStrategy.StrategyMode.Stopped) // Stopped = закрыть позицию
            {
                _ = ExecuteSignal(signal);
            }
        }

        // Лог состояния
        if (_strategy.CurrentLots > 0)
        {
            Console.WriteLine($"[STATE] Pos={_strategy.PositionDirection} Lots={_strategy.CurrentLots} " +
                              $"Trades={_strategy.TotalTrades} PnL={_strategy.TotalPnL:F0} " +
                              $"LotLevel={_strategy.CurrentLotLevel} SessProf={_strategy.SessionProfit:F0}");
        }
    }

    private async Task ExecuteSignal(Signal signal)
    {
        try
        {
            var order = new Order
            {
                Ticker = _ticker,
                Direction = signal.Direction,
                Type = OrderType.Market,
                Volume = signal.Volume,
                Comment = signal.Comment
            };

            await _broker.PlaceOrderAsync(order);
            Console.WriteLine($"[EXEC] ✅ {signal.Direction} {signal.Volume}x {_ticker} | {signal.Comment}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXEC] ❌ Ошибка: {ex.Message}");
        }
    }

    // === Команды управления ===

    /// <summary>"Старт" — начать торговлю</summary>
    public void Start()
    {
        _strategy.Mode = PsarEmaComboStrategy.StrategyMode.Running;
        Console.WriteLine("[CMD] ▶️ СТАРТ — стратегия торгует");
    }

    /// <summary>"Стоп торги" — закрыть всё, больше не торговать</summary>
    public void StopTrading()
    {
        _strategy.Mode = PsarEmaComboStrategy.StrategyMode.Stopped;
        Console.WriteLine("[CMD] ⏹️ СТОП ТОРГИ — закрываем позиции, стратегия остановлена");
    }

    /// <summary>"Пауза" — не закрываем, не открываем новые</summary>
    public void Pause()
    {
        _strategy.Mode = PsarEmaComboStrategy.StrategyMode.Paused;
        Console.WriteLine("[CMD] ⏸️ ПАУЗА — позиции заморожены, новые не открываем");
    }

    /// <summary>Текущий статус</summary>
    public string GetStatus()
    {
        return $"Mode={_strategy.Mode} | Pos={_strategy.PositionDirection} | " +
               $"Lots={_strategy.CurrentLots} | MaxEver={_strategy.MaxLotsEver} | " +
               $"Trades={_strategy.TotalTrades} | PnL={_strategy.TotalPnL:F0} | " +
               $"LotLevel={_strategy.CurrentLotLevel} | SessProfit={_strategy.SessionProfit:F0} | " +
               $"ConsecLosses={_strategy.ConsecutiveLosses} | Connected={IsConnected}";
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _broker.Dispose();
    }
}
