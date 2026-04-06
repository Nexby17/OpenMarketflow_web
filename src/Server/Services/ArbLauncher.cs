using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Лаунчер арбитражного портфеля (акция/фьючерс) на Финам.
///
/// Торгуемые пары (из бэктеста, walk-forward 20/20, +22% net/year):
///   ROSN / ROSNMx   — лидер по доходности
///   TATN / TATNMx    — лучший Sharpe  
///   GAZP / GAZRMx    — стабильный, 0 roll losses
///   SBER / SBRFMx    — надёжный
///   ALRS / ALRSMx    — 100% WR
///
/// x = ближайший контракт: M6(июнь), U6(сент), Z6(дек), H7(март)
/// 
/// Управление: Start() / Stop() / Pause()
/// </summary>
public class ArbLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly ArbPortfolioManager _portfolio;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(5);
    private readonly ILogger<ArbLauncher>? _logger;
    
    // Маппинг спот → фьючерс (обновляется при ролле)
    private readonly Dictionary<string, string> _spotToFutures = new();
    
    // Последние цены спотов и фьючерсов
    private readonly Dictionary<string, double> _lastSpotPrice = new();
    private readonly Dictionary<string, double> _lastFuturesPrice = new();
    
    // Дней до экспирации (обновляется раз в день)
    private int _daysToExpiry = 60;
    
    public ArbPortfolioManager Portfolio => _portfolio;
    public bool IsConnected => _broker.IsConnected;

    /// <summary>Актуальные тикеры фьючерсов (контракт M6 = июнь 2026)</summary>
    private static readonly Dictionary<string, string> DefaultFuturesMap = new()
    {
        ["ROSN"] = "ROSNM6",
        ["TATN"] = "TATNM6",
        ["GAZP"] = "GAZRM6",
        ["SBER"] = "SBRFM6",
        ["ALRS"] = "ALRSM6",
    };

    /// <summary>Размер лота фьючерса (акций в 1 контракте)</summary>
    private static readonly Dictionary<string, int> FuturesLotSize = new()
    {
        ["ROSN"] = 100,
        ["TATN"] = 100,
        ["GAZP"] = 100,
        ["SBER"] = 100,
        ["ALRS"] = 100,
    };

    /// <summary>
    /// Создать лаунчер арбитража.
    /// </summary>
    /// <param name="finamToken">Торговый токен Финам</param>
    /// <param name="futuresOverrides">Переопределение тикеров фьючерсов (опционально)</param>
    /// <param name="capital">Капитал на арбитраж (по умолчанию 10 млн)</param>
    /// <param name="logger">Логгер</param>
    public ArbLauncher(string finamToken, 
                       Dictionary<string, string>? futuresOverrides = null,
                       double capital = 10_000_000,
                       ILogger<ArbLauncher>? logger = null)
    {
        _logger = logger;
        _broker = new FinamConnector();
        _portfolio = new ArbPortfolioManager();
        
        // Инициализация портфеля с валидированными параметрами
        _portfolio.InitializeDefaultPortfolio();
        _portfolio.ScaleToCapital(capital);
        
        // Установка тикеров фьючерсов
        var futMap = futuresOverrides ?? DefaultFuturesMap;
        foreach (var (spot, fut) in futMap)
        {
            _spotToFutures[spot] = fut;
        }
        
        // Проставляем тикеры в стратегии
        foreach (var strategy in _portfolio.Strategies.Values)
        {
            if (_spotToFutures.TryGetValue(strategy.SpotTicker, out var futTicker))
            {
                strategy.FuturesTicker = futTicker;
            }
        }
        
        // Подписка на события
        _broker.OnError += msg => Log($"❌ BROKER: {msg}");
        _broker.OnTrade += trade => Log($"📊 TRADE: {trade.Direction} {trade.Volume}x @ {trade.Price:F2}");
        _portfolio.OnSignal += OnArbSignal;
        _portfolio.OnLog += msg => Log(msg);
        
        // Подключение и прогрев
        _ = ConnectAndInitialize(finamToken);
    }

    private async Task ConnectAndInitialize(string token)
    {
        Log("🔌 Подключение к Финам для арбитража...");
        bool ok = await _broker.ConnectAsync(token, "");
        if (!ok)
        {
            Log("❌ Не удалось подключиться к Финам!");
            return;
        }
        Log("✅ Подключён к Финам");

        // Рассчитываем дни до экспирации (ближайшая экспирация)
        // M6 = июнь 2026, экспирация ~17 июня
        _daysToExpiry = CalcDaysToExpiry();
        Log($"📅 Дней до экспирации: {_daysToExpiry}");

        // Устанавливаем DaysToExpiry в стратегиях
        foreach (var s in _portfolio.Strategies.Values)
            s.DaysToExpiry = _daysToExpiry;

        // Прогрев: загружаем исторические свечи за 5 дней
        Log("📐 Прогрев индикаторов...");
        var from = DateTime.UtcNow.AddDays(-7);
        var to = DateTime.UtcNow;

        foreach (var strategy in _portfolio.Strategies.Values)
        {
            try
            {
                // Грузим свечи спота
                var spotCandles = await _broker.GetHistoricalCandlesAsync(
                    strategy.SpotTicker, _timeframe, from, to);
                
                // Грузим свечи фьючерса
                var futCandles = await _broker.GetHistoricalCandlesAsync(
                    strategy.FuturesTicker, _timeframe, from, to);

                if (spotCandles.Length > 0 && futCandles.Length > 0)
                {
                    // Прогоняем прогрев — спаривая по времени
                    int si = 0, fi = 0;
                    while (si < spotCandles.Length && fi < futCandles.Length)
                    {
                        var st = spotCandles[si].Timestamp;
                        var ft = futCandles[fi].Timestamp;

                        if (Math.Abs((st - ft).TotalMinutes) <= 5)
                        {
                            strategy.OnPriceUpdate(
                                spotCandles[si].Close, 
                                futCandles[fi].Close, 
                                st);
                            si++; fi++;
                        }
                        else if (st < ft) si++;
                        else fi++;
                    }

                    Log($"  {strategy.Name}: прогрев {Math.Min(si, fi)} свечей | Z={strategy.LastZScore:F2} Basis={strategy.LastBasisAnnual:F1}%");
                }
                else
                {
                    Log($"  {strategy.Name}: ⚠️ нет данных (spot={spotCandles.Length}, fut={futCandles.Length})");
                }
            }
            catch (Exception ex)
            {
                Log($"  {strategy.Name}: ❌ ошибка прогрева: {ex.Message}");
            }
        }

        // Подписываемся на свечи
        Log("📡 Подписка на свечи...");
        foreach (var strategy in _portfolio.Strategies.Values)
        {
            var spotTicker = strategy.SpotTicker;
            var futTicker = strategy.FuturesTicker;

            await _broker.SubscribeCandlesAsync(spotTicker, _timeframe, 
                candle => OnSpotCandle(spotTicker, candle));

            await _broker.SubscribeCandlesAsync(futTicker, _timeframe,
                candle => OnFuturesCandle(spotTicker, candle));

            Log($"  ✅ {strategy.Name}: {spotTicker} + {futTicker}");
        }

        Log("🟢 Арбитраж инициализирован. Ожидаю команду 'Старт'.");
        Log($"Портфель: {_portfolio.Strategies.Count} пар");
    }

    // === Обработка свечей ===

    private void OnSpotCandle(string spotTicker, Candle candle)
    {
        _lastSpotPrice[spotTicker] = candle.Close;
        TryProcessUpdate(spotTicker, candle.Timestamp);
    }

    private void OnFuturesCandle(string spotTicker, Candle candle)
    {
        _lastFuturesPrice[spotTicker] = candle.Close;
        TryProcessUpdate(spotTicker, candle.Timestamp);
    }

    private void TryProcessUpdate(string spotTicker, DateTime timestamp)
    {
        // Обновляем только когда есть обе цены
        if (!_lastSpotPrice.TryGetValue(spotTicker, out var spotPrice)) return;
        if (!_lastFuturesPrice.TryGetValue(spotTicker, out var futPrice)) return;

        var signal = _portfolio.ProcessPriceUpdate(spotTicker, spotPrice, futPrice, timestamp);
        // Сигнал обрабатывается через событие OnSignal → OnArbSignal
    }

    // === Исполнение сигналов ===

    private void OnArbSignal(ArbSignal signal)
    {
        Log($"🔔 СИГНАЛ: {signal}");
        _ = ExecuteArbSignal(signal);
    }

    private async Task ExecuteArbSignal(ArbSignal signal)
    {
        try
        {
            var spotLots = signal.SpotLots > 0 ? signal.SpotLots : signal.Lots;
            var futLots = signal.FutLots > 0 ? signal.FutLots : signal.Lots;
            
            if (signal.Action == ArbAction.Open)
            {
                if (signal.Direction == ArbDirection.LongSpread)
                {
                    await PlaceOrder(signal.SpotTicker, SignalDirection.Buy, spotLots, $"ARB OPEN: лонг {spotLots} лот акций");
                    await PlaceOrder(signal.FuturesTicker, SignalDirection.Sell, futLots, $"ARB OPEN: шорт {futLots} контрактов");
                }
                else
                {
                    await PlaceOrder(signal.SpotTicker, SignalDirection.Sell, spotLots, $"ARB OPEN: шорт {spotLots} лот акций");
                    await PlaceOrder(signal.FuturesTicker, SignalDirection.Buy, futLots, $"ARB OPEN: лонг {futLots} контрактов");
                }
            }
            else if (signal.Action == ArbAction.Close)
            {
                if (signal.Direction == ArbDirection.LongSpread)
                {
                    await PlaceOrder(signal.SpotTicker, SignalDirection.Sell, spotLots, $"ARB CLOSE: продать {spotLots} лот акций");
                    await PlaceOrder(signal.FuturesTicker, SignalDirection.Buy, futLots, $"ARB CLOSE: купить {futLots} контрактов");
                }
                else
                {
                    await PlaceOrder(signal.SpotTicker, SignalDirection.Buy, spotLots, $"ARB CLOSE: купить {spotLots} лот акций");
                    await PlaceOrder(signal.FuturesTicker, SignalDirection.Sell, futLots, $"ARB CLOSE: продать {futLots} контрактов");
                }

                Log($"💰 PnL сделки: {signal.PnL:+#,##0;-#,##0;0} ₽ | Итого: {_portfolio.TotalPnL:+#,##0;-#,##0;0} ₽");
            }
        }
        catch (Exception ex)
        {
            Log($"❌ Ошибка исполнения: {ex.Message}");
        }
    }

    private async Task PlaceOrder(string ticker, SignalDirection direction, int lots, string comment)
    {
        var order = new Order
        {
            Ticker = ticker,
            Direction = direction,
            Type = OrderType.Market,
            Volume = lots,
            Comment = comment
        };

        await _broker.PlaceOrderAsync(order);
        Log($"  📤 {direction} {lots}x {ticker} | {comment}");
    }

    // === Команды управления ===

    /// <summary>"Старт" — начать арбитражную торговлю</summary>
    public void Start()
    {
        _portfolio.SetMode(StrategyMode.Running);
        Log("▶️ СТАРТ — арбитраж торгует");
    }

    /// <summary>"Стоп торги" — закрыть все позиции, остановить</summary>
    public void StopTrading()
    {
        _portfolio.SetMode(StrategyMode.Stopped);
        var closed = _portfolio.CloseAll(DateTime.UtcNow);
        Log($"⏹️ СТОП ТОРГИ — закрыто {closed.Count} позиций");
    }

    /// <summary>"Пауза" — не закрываем, не открываем новые</summary>
    public void Pause()
    {
        _portfolio.SetMode(StrategyMode.Paused);
        Log("⏸️ ПАУЗА — позиции заморожены");
    }

    /// <summary>Запустить/остановить конкретную пару</summary>
    public void SetPairMode(string spotTicker, StrategyMode mode)
    {
        var name = $"ARB_{spotTicker}";
        _portfolio.SetMode(name, mode);
    }

    /// <summary>Текущий статус портфеля</summary>
    public string GetStatus()
    {
        return _portfolio.GetStatus() + 
               $"\nConnected: {IsConnected} | DaysToExpiry: {_daysToExpiry}";
    }

    /// <summary>Ролл: перейти на новые контракты</summary>
    public async Task RollContractsAsync(Dictionary<string, string> newFutures, int newDaysToExpiry)
    {
        Log($"🔄 РОЛЛ контрактов. Новый DTE: {newDaysToExpiry}");
        _daysToExpiry = newDaysToExpiry;

        foreach (var (spot, newFut) in newFutures)
        {
            _spotToFutures[spot] = newFut;
            var strategy = _portfolio.Strategies.Values.FirstOrDefault(s => s.SpotTicker == spot);
            if (strategy != null)
            {
                var signal = strategy.OnContractRoll(newFut, newDaysToExpiry,
                    _lastSpotPrice.GetValueOrDefault(spot),
                    _lastFuturesPrice.GetValueOrDefault(spot),
                    DateTime.UtcNow);

                if (signal != null)
                {
                    await ExecuteArbSignal(signal);
                }

                // Переподписка на новый фьючерс
                await _broker.SubscribeCandlesAsync(newFut, _timeframe,
                    candle => OnFuturesCandle(spot, candle));

                Log($"  {spot}: {strategy.FuturesTicker} → {newFut}");
            }
        }
    }

    // === Утилиты ===

    private int CalcDaysToExpiry()
    {
        // Экспирации SI/фондовых фьючерсов MOEX: 3-й четверг месяца
        // M6 = июнь 2026 → ~18 июня 2026
        var now = DateTime.UtcNow.Date;
        
        // Ближайшие экспирации
        var expirations = new[]
        {
            new DateTime(2026, 6, 18),  // M6
            new DateTime(2026, 9, 17),  // U6
            new DateTime(2026, 12, 17), // Z6
            new DateTime(2027, 3, 18),  // H7
        };

        foreach (var exp in expirations)
        {
            var dte = (exp - now).Days;
            if (dte > 0) return dte;
        }

        return 90; // fallback
    }

    private void Log(string message)
    {
        var ts = DateTime.UtcNow.ToString("HH:mm:ss");
        var msg = $"[{ts}] [ARB] {message}";
        Console.WriteLine(msg);
        _logger?.LogInformation(msg);
    }

    public void Dispose()
    {
        _broker.Dispose();
    }
}
