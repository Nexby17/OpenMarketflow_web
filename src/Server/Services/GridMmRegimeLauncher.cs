using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;
using Microsoft.AspNetCore.SignalR;
using HedgeFund.Server.Hubs;

namespace HedgeFund.Server.Services;

/// <summary>
/// Лаунчер Grid MM v6 с РЕАЛЬНЫМИ лимитными ордерами.
/// 
/// Архитектура:
/// - Стратегия: анализирует SAR×EMA → генерирует события (вход/выход)
/// - Лаунчер: исполняет события через брокера
///   - Вход: 1 лот маркетом + grid лимитки
///   - Grid fill: отслеживается по OnOrderUpdate
///   - Grid TP: отслеживается по OnOrderUpdate → round trip
///   - Close all: отмена всех лимиток + закрытие позиции маркетом
/// 
/// TF: 5 мин. Инструмент: SiM6 (или актуальный SI фьючерс).
/// </summary>
public class GridMmRegimeLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly QuikCandleProvider? _quikProvider;
    private readonly bool _useQuikData;
    private readonly GridMmRegimeStrategy _strategy;
    private readonly string _ticker;
    private readonly string _accountId;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(5);
    private readonly IHubContext<TradingHub>? _hub;
    private CancellationTokenSource? _cts;

    // Отслеживание лимитных ордеров
    private struct TrackedOrder
    {
        public string BrokerOrderId;
        public string Type;     // "grid_buy" | "grid_tp" | "entry_tp"
        public int LevelIndex;  // -1 для entry_tp
        public double Price;
        public int Volume;
        public bool IsBuy;      // true = buy, false = sell
        public double OriginalPrice; // цена grid_buy для recycling
    }
    private readonly Dictionary<string, TrackedOrder> _trackedOrders = new();
    private readonly object _orderLock = new();
    
    // Дедупликация свечей
    private DateTime _lastProcessedCandle = DateTime.MinValue;
    private bool _warmedUp = false; // После прогрева — торговля разрешена
    private DateTime _warmupEndTime = DateTime.MinValue; // Свечи старше этого — игнор
    
    // Entry TP ордер
    private string? _entryTpOrderId;
    
    // Close state
    private bool _closingAll;
    private readonly bool _forceEntryOnStart;

    public GridMmRegimeStrategy Strategy => _strategy;
    public bool IsConnected => _useQuikData || _broker.IsConnected;

    public GridMmRegimeLauncher(FinamConnector broker, string ticker = "SiM6", string accountId = "", IHubContext<TradingHub>? hub = null, bool useQuikData = false, bool forceEntryOnStart = false)
    {
        _useQuikData = useQuikData;
        _ticker = ticker;
        _accountId = accountId;
        _forceEntryOnStart = forceEntryOnStart;
        _hub = hub;
        _broker = broker; // Общий экземпляр из TradingService
        if (useQuikData)
            _quikProvider = new QuikCandleProvider();

        // Параметры синхронизированы с TOOLS.md (v6 SAR 0.009/0.01/0.2, EMA30, 5мин)
        _strategy = new GridMmRegimeStrategy(new GridMmRegimeStrategy.Config
        {
            SarStart = 0.009,
            SarStep = 0.01,
            SarMax = 0.2,
            EmaPeriod = 30,
            GridStep = 45.0,
            GridSpread = 50.0,
            MaxGridLevels = 70,
            MinProfitPerLot = 35.0,
            ClosePct = 0.30,
            Commission = 0.90,
            LotStepProfit = 9999999.0,
            MaxLots = 1
        });

        // Обработка заполнения ордеров
        _broker.OnOrderUpdate += OnOrderUpdate;
        _broker.OnTrade += trade => Console.WriteLine($"[TRADE] {trade.Direction} {trade.Volume}x @ {trade.Price:F0}");
        _broker.OnError += msg =>
        {
            Console.WriteLine($"[BROKER ERROR] {msg}");
            _ = HandleDisconnect();
        };

        _strategy.Params.ForceEntryOnStart = forceEntryOnStart;
        // Брокер уже подключён — сразу warmup
        _ = ConnectAndWarm(accountId);
    }

    private async Task ConnectAndWarm(string accountId)
    {
        try
        {
            System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} ConnectAndWarm started (shared broker)\n");
            Console.WriteLine("[GRID-MM-v6] 📐 Warmup (broker уже подключён)...");

            if (!_broker.IsConnected)
            {
                Console.WriteLine("[GRID-MM-v6] ❌ Broker не подключён!");
                return;
            }

        // Прогрев: 5-мин свечи
        Console.WriteLine($"[GRID-MM-v6] 📐 Прогрев индикаторов ({_ticker}, 5-мин)...");
            System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} About to load candles, useQuikData={_useQuikData}, quikProvider={_quikProvider != null}\n");
        Candle[] history;
        
        if (_useQuikData && _quikProvider != null)
        {
            System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} Loading QUIK candles...\n");
            try
            {
                var quikTask = _quikProvider.GetHistoricalCandlesAsync();
                var timeoutTask = Task.Delay(10000); // 10 sec timeout
                var completed = await Task.WhenAny(quikTask, timeoutTask);
                if (completed == timeoutTask)
                {
                    System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} QUIK timeout, fallback to Finam\n");
                    Console.WriteLine("[GRID-MM-v6] ⚠️ QUIK candles timeout, fallback to Finam");
                    var from = DateTime.UtcNow.AddDays(-7);
                    var to = DateTime.UtcNow;
                    history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);
                }
                else
                {
                    history = quikTask.Result.ToArray();
                    System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} QUIK candles loaded: {history.Length}\n");
                    if (history.Length == 0)
                    {
                        System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} QUIK empty, fallback to Finam\n");
                        Console.WriteLine("[GRID-MM-v6] ⚠️ QUIK candles empty, fallback to Finam");
                        var from = DateTime.UtcNow.AddDays(-7);
                        var to = DateTime.UtcNow;
                        history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);
                    }
                }
            }
            catch (Exception ex)
            {
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} QUIK failed: {ex.Message}\n");
                Console.WriteLine($"[GRID-MM-v6] ⚠️ QUIK candles failed: {ex.Message}, fallback to Finam");
                var from = DateTime.UtcNow.AddDays(-7);
                var to = DateTime.UtcNow;
                history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);
            }
        }
        else
        {
            var from = DateTime.UtcNow.AddDays(-7);
            var to = DateTime.UtcNow;
            history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);
        }

        if (history.Length > 0)
        {
            Console.WriteLine($"[GRID-MM-v6] Загружено {history.Length} исторических свечей");
            var origMode = _strategy.Mode;
            _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Paused; // Не генерировать entry signals
            try
            {
                for (int i = 0; i < history.Length; i++)
                {
                    _strategy.OnCandle(history[i], _ticker);
                    while (_strategy.HasPendingEvents)
                        _strategy.GetNextEvent();
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[GRID-MM-v6] ❌ ОШИБКА ПРОГРЕВА: {ex.Message}");
            }
            _strategy.Mode = origMode; // Восстанавливаем режим
            Console.WriteLine($"[GRID-MM-v6] ✅ Прогрето. SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0}");
            _warmedUp = true;
            _warmupEndTime = DateTime.UtcNow;
            _lastProcessedCandle = DateTime.UtcNow;
            _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Running; // Автостарт после прогрева
            
            // Восстанавливаем состояние (если есть открытая позиция)
            _ = RestoreStateAsync();

            // ForceEntry если включено
            _ = ForceEntryAsync();
        }
        else
        {
            Console.WriteLine($"[GRID-MM-v6] ⚠️ Нет исторических данных");
        }

        if (_useQuikData)
        {
            Console.WriteLine($"[GRID-MM-v6] 📡 QUIK poll активен. Ждём сигналов.");
            _cts = new CancellationTokenSource();
            _ = Task.Run(() => QuikPollLoop(_cts.Token));
        }
        else
        {
            Console.WriteLine($"[GRID-MM-v6] 📡 Подписка на {_ticker} 5-мин...");
            await _broker.SubscribeCandlesAsync(_ticker, _timeframe, OnNewCandle);
            Console.WriteLine($"[GRID-MM-v6] 📡 Активна. Ждём сигналов.");
        }
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[GRID-MM-v6] ❌ ConnectAndWarm error: {ex.Message}");
        System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} ConnectAndWarm ERROR: {ex.Message}\n{ex.StackTrace}\n");
    }
    }

    private async Task ForceEntryAsync()
    {
        if (!_forceEntryOnStart || _strategy.OpenLots > 0) return;
        
        Console.WriteLine("[GRID-MM-v6] ⚡ ForceEntry: determining direction...");
        
        // Определяем направление по текущим SAR/EMA (уже прогреты)
        int direction;
        double sar = _strategy.CurrentSar;
        double ema = _strategy.CurrentEma;
        
        if (sar > ema)
        {
            direction = -1; // SHORT
            Console.WriteLine($"[GRID-MM-v6] ⚡ ForceEntry: SHORT (SAR={sar:F0} > EMA={ema:F0})");
        }
        else if (sar < ema)
        {
            direction = 1; // LONG
            Console.WriteLine($"[GRID-MM-v6] ⚡ ForceEntry: LONG (SAR={sar:F0} < EMA={ema:F0})");
        }
        else
        {
            Console.WriteLine($"[GRID-MM-v6] ⚠️ ForceEntry: SAR==EMA, skipping");
            return;
        }
        
        // Создаём событие входа
        var entryEvt = new GridMmRegimeStrategy.StrategyEvent
        {
            Type = GridMmRegimeStrategy.StrategyEvent.EventType.EntryMarket,
            Direction = direction,
            Volume = 1,
            Reason = "ForceEntry"
        };
        
        await ExecuteEntryAsync(entryEvt);
        
        // Вычисляем grid цены (как в стратегии)
        double step = _strategy.Params.GridStep;
        var prices = new List<double>();
        var tpPrices = new List<double>();
        for (int i = 0; i < _strategy.Params.MaxGridLevels; i++)
        {
            double gridPrice = direction == 1 
                ? sar - step * (i + 1)  // long: BUY ниже
                : sar + step * (i + 1); // short: SELL выше
            double tpPrice = direction == 1
                ? gridPrice + _strategy.Params.GridSpread  // long: TP выше
                : gridPrice - _strategy.Params.GridSpread; // short: TP ниже
            prices.Add(gridPrice);
            tpPrices.Add(tpPrice);
        }
        
        // Создаём событие для grid лимиток
        var gridEvt = new GridMmRegimeStrategy.StrategyEvent
        {
            Type = GridMmRegimeStrategy.StrategyEvent.EventType.GridLimitOrders,
            Direction = direction,
            Price = sar,
            Volume = 1,
            GridPrices = prices.ToArray(),
            GridTpPrices = tpPrices.ToArray(),
            Reason = $"Grid: {_strategy.Params.MaxGridLevels} уровней, step={step}, spread={_strategy.Params.GridSpread}"
        };
        
        await PlaceGridLimitsAsync(gridEvt);
        Console.WriteLine("[GRID-MM-v6] ✅ ForceEntry completed");
    }

    private async Task QuikPollLoop(CancellationToken ct)
    {
        DateTime? lastCandleTime = null;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(5000, ct); // Poll каждые 5 секунд
                if (_quikProvider == null) continue;
                
                var candles = await _quikProvider.GetHistoricalCandlesAsync();
                if (candles.Count == 0) continue;
                
                var latest = candles.LastOrDefault();
                if (latest == null) continue;
                
                // Новая свеча?
                if (lastCandleTime == null || latest.Timestamp > lastCandleTime)
                {
                    // Проверяем, не старая ли это свеча (уже обработана)
                    if (lastCandleTime != null && latest.Timestamp <= lastCandleTime.Value)
                        continue;
                    
                    OnNewCandle(latest);
                    lastCandleTime = latest.Timestamp;
                }
            }
            catch (TaskCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[QUIK POLL] Error: {ex.Message}");
            }
        }
    }

    private int _candleCount = 0;
    
    private async void OnNewCandle(Candle candle)
    {
        // === ЗАЩИТА: после прогрева игнорируем все старые свечи ===
        if (!_warmedUp) return; // Прогрев ещё не завершён
        if (candle.Timestamp <= _warmupEndTime) return; // Старая свеча — пропускаем
        
        // === ДЕДУПЛИКАЦИЯ: пропускаем повторные свечи ===
        if (candle.Timestamp <= _lastProcessedCandle)
            return;
        _lastProcessedCandle = candle.Timestamp;
        
        _candleCount++;
        
        // Логируем каждую 12-ю свечу (~раз в час на 5-мин)
        if (_candleCount % 12 == 1)
        {
            Console.WriteLine($"[GRID-MM-v6] 🕯️ #{_candleCount}: {candle.Timestamp:HH:mm:ss} " +
                              $"O={candle.Open:F0} H={candle.High:F0} L={candle.Low:F0} C={candle.Close:F0} " +
                              $"| SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0}");
        }
        
        // Получаем текущий баланс от брокера
        double portfolioValue = _broker.IsConnected ? (await _broker.GetAccountInfoAsync()).equity : 1000000.0;
        _strategy.SetPortfolioValue(portfolioValue);
        _strategy.OnCandle(candle, _ticker);
        
        // Обновляем статус в UI (каждую свечу)
        _ = BroadcastStatus();
        
        // Обрабатываем события
        while (_strategy.HasPendingEvents)
        {
            var evt = _strategy.GetNextEvent();
            if (evt == null) break;
            
            switch (evt.Type)
            {
                case GridMmRegimeStrategy.StrategyEvent.EventType.EntryMarket:
                    // Защита: не входим если уже в позиции
                    if (_strategy.PositionDirection != 0)
                    {
                        Console.WriteLine($"[SIGNAL] ⚠️ ПРОПУСК entry — уже в позиции dir={_strategy.PositionDirection}");
                        break;
                    }
                    Console.WriteLine($"[SIGNAL] 📈 {evt.Reason} @ {evt.Price:F0}");
                    _ = ExecuteEntryAsync(evt);
                    break;
                    
                case GridMmRegimeStrategy.StrategyEvent.EventType.GridLimitOrders:
                    Console.WriteLine($"[SIGNAL] 📊 {evt.Reason}");
                    _ = PlaceGridLimitsAsync(evt);
                    break;
                    
                case GridMmRegimeStrategy.StrategyEvent.EventType.CloseAllMarket:
                    Console.WriteLine($"[SIGNAL] 🔒 {evt.Reason}");
                    _ = ExecuteCloseAllAsync(evt);
                    break;
            }
        }
        
        // === ПРОВЕРКА ПОТЕРЯННЫХ ОРДЕРОВ (каждую свечу при позиции) ===
        if (_strategy.PositionDirection != 0)
        {
            _ = VerifyAndRestoreLostOrdersAsync();
        }
        
        // Статус при наличии позиции
        if (_strategy.PositionDirection != 0 && _candleCount % 12 == 0)
        {
            Console.WriteLine(_strategy.GetStatus());
        }
    }

    // ==================== ИСПОЛНЕНИЕ ====================

    /// <summary>
    /// Вход маркетом (без Entry TP — лимитки расставляются через GridLimitOrders)
    /// </summary>
    private async Task ExecuteEntryAsync(GridMmRegimeStrategy.StrategyEvent evt)
    {
        try
        {
            // Маркет ордер на вход
            var entryOrder = new Order
            {
                Ticker = _ticker,
                Direction = evt.Direction == 1 ? SignalDirection.Buy : SignalDirection.Sell,
                Type = OrderType.Market,
                Volume = evt.Volume,
                Comment = evt.Reason
            };
            var result = await _broker.PlaceOrderAsync(entryOrder);
            Console.WriteLine("[EXEC] ✅ Entry {0} {1}x {2} → order={3}", evt.Direction, evt.Volume, _ticker, result.BrokerOrderId);
            
            // Broadcast trade + position
            await BroadcastTrade(evt.Direction == 1 ? "Buy" : "Sell", evt.Volume, 0, "Entry");
            await BroadcastStatus();
            
            // Проверяем реальную позицию через 1 сек
            _ = Task.Run(async () => {
                await Task.Delay(1000);
                try {
                    var positions = await _broker.GetPositionsAsync();
                    var pos = positions.FirstOrDefault(p => p.Ticker == _ticker);
                    Console.WriteLine($"[EXEC] 🔍 Position check: {(pos != null ? $"{pos.Direction} {pos.Entries[0].Volume}x @ {pos.Entries[0].Price:F0}" : "NONE")}");
                } catch(Exception ex) { Console.WriteLine($"[EXEC] 🔍 Position check failed: {ex.Message}"); }
            });
            
            // Entry TP НЕ выставляется — grid лимитки расставляются через PlaceGridLimitsAsync
            // Каждый уровень грида имеет свой TP при исполнении
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXEC] ❌ Entry failed: {ex.Message}");
        }
    }

    // Какой следующий grid уровень ставить
    private int _nextGridLevel = 0;
    private GridMmRegimeStrategy.StrategyEvent? _gridConfig; // сохраняем конфигурацию grid'а
    
    /// <summary>
    /// Сохраняем конфигурацию grid и ставим первый уровень.
    /// </summary>
    private async Task PlaceGridLimitsAsync(GridMmRegimeStrategy.StrategyEvent evt)
    {
        _gridConfig = evt;
        _nextGridLevel = 0;
        
        Console.WriteLine($"[GRID] Инкрементальный режим: {evt.GridPrices.Length} уровней, ставим по 1");
        
        // Ставим первый уровень
        await PlaceNextGridLevelAsync();
    }
    
    /// <summary>
    /// Ставим следующий grid уровень (1 лимитка).
    /// </summary>
    private async Task PlaceNextGridLevelAsync()
    {
        if (_gridConfig == null) return;
        if (_nextGridLevel >= _gridConfig.GridPrices.Length) return;
        
        int dir = _gridConfig.Direction;
        int j = _nextGridLevel;
        double price = Math.Round(_gridConfig.GridPrices[j]);
        double tpPrice = Math.Round(_gridConfig.GridTpPrices[j]);
        
        var gridOrder = new Order
        {
            Ticker = _ticker,
            Direction = dir == 1 ? SignalDirection.Buy : SignalDirection.Sell, // LONG→BUY ниже, SHORT→SELL выше (наращивание позиции)
            Type = OrderType.Limit,
            Price = price,
            Volume = _gridConfig.Volume,
            Comment = $"Grid[{j}]"
        };
        
        try
        {
            var result = await _broker.PlaceOrderAsync(gridOrder);
            lock (_orderLock)
            {
                _trackedOrders[result.BrokerOrderId] = new TrackedOrder
                {
                    BrokerOrderId = result.BrokerOrderId,
                    Type = "grid_buy",
                    LevelIndex = j,
                    Price = price,
                    Volume = _gridConfig.Volume,
                    IsBuy = dir == 1,
                    OriginalPrice = price
                    };
            }
            _strategy.SetGridOrderIds(j, result.BrokerOrderId, null);
            Console.WriteLine($"[GRID] 📌 Level {j} @ {price:F0} (TP={tpPrice:F0}) → order={result.BrokerOrderId}");
            System.IO.File.AppendAllText("/tmp/orders.log", $"{DateTime.UtcNow:HH:mm:ss} GRID-TRACK: level={j} id={result.BrokerOrderId} price={price} tp={tpPrice}\n");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[GRID] ❌ Level {j} failed: {ex.Message}");
        }
    }


    /// <summary>
    /// Закрытие: отменяем ВСЕ лимитки, затем маркетом закрываем позицию.
    /// </summary>
    private async Task ExecuteCloseAllAsync(GridMmRegimeStrategy.StrategyEvent evt)
    {
        if (_closingAll) return;
        _closingAll = true;
        
        try
        {
            // 1. Отменяем все лимитки
            List<string> ordersToCancel;
            lock (_orderLock)
            {
                ordersToCancel = _trackedOrders.Keys.ToList();
            }
            
            int cancelled = 0;
            foreach (var orderId in ordersToCancel)
            {
                try
                {
                    await _broker.CancelOrderAsync(orderId);
                    cancelled++;
                }
                catch { /* ордер уже исполнен или отменён */ }
            }
            
            lock (_orderLock)
            {
                _trackedOrders.Clear();
            }
            _entryTpOrderId = null;
            _gridConfig = null;
            _nextGridLevel = 0;
            
            Console.WriteLine($"[CLOSE] Отменено {cancelled} лимитных ордеров");
            
            // 2. Получаем текущую позицию
            var positions = await _broker.GetPositionsAsync();
            var pos = positions.FirstOrDefault(p => p.Ticker == _ticker);
            
            if (pos != null && pos.Entries.Count > 0 && pos.Entries[0].Volume > 0)
            {
                int openQty = pos.Entries[0].Volume;
                var closeDir = pos.Direction == SignalDirection.Buy ? SignalDirection.Sell : SignalDirection.Buy;
                
                var closeOrder = new Order
                {
                    Ticker = _ticker,
                    Direction = closeDir,
                    Type = OrderType.Market,
                    Volume = openQty,
                    Comment = evt.Reason
                };
                
                var result = await _broker.PlaceOrderAsync(closeOrder);
                Console.WriteLine($"[CLOSE] ✅ Market close {openQty}x → order={result.BrokerOrderId}");
            }
            else
            {
                Console.WriteLine($"[CLOSE] Позиция уже закрыта");
            }
            
            // PnL: используем (entryPrice - entryPrice) как approximation,
            // реальный PnL будет из OnTrade callback
            double approxPnl = _strategy.EntryPrice > 0 ? _strategy.CalcGridPnL(_strategy.EntryPrice) : 0;
            _strategy.OnCloseAllComplete(approxPnl);
            _strategy.ResetPosition();
            await BroadcastTrade("Close", 0, 0, "Close All");
            await BroadcastStatus();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CLOSE] ❌ Error: {ex.Message}");
        }
        finally
        {
            _closingAll = false;
        }
    }

    // ==================== ОБРАБОТКА ЗАПОЛНЕНИЯ ОРДЕРОВ ====================

    private void OnOrderUpdate(Order order)
    {
        System.IO.File.AppendAllText("/tmp/orders.log", $"{DateTime.UtcNow:HH:mm:ss} ORDER: id={order.BrokerOrderId} status={order.Status} dir={order.Direction} vol={order.Volume}/{order.FilledVolume}\n");
        // Логируем ВСЕ обновления ордеров (не только заполненные)
        lock (_orderLock)
        {
            if (_trackedOrders.TryGetValue(order.BrokerOrderId, out var t))
                Console.WriteLine($"[ORDER] {t.Type}[{t.LevelIndex}] id={order.BrokerOrderId} status={order.Status} vol={order.FilledVolume}/{order.Volume}");
            else
                Console.WriteLine($"[ORDER] unknown id={order.BrokerOrderId} status={order.Status} dir={order.Direction} vol={order.Volume}");
        }
        
        if (order.Status != OrderStatus.Filled && order.Status != OrderStatus.PartiallyFilled)
            return;
        
        TrackedOrder tracked;
        lock (_orderLock)
        {
            if (!_trackedOrders.TryGetValue(order.BrokerOrderId, out tracked))
                return;
        }
        
        Console.WriteLine($"[ORDER FILL] {tracked.Type}[{tracked.LevelIndex}] @ {tracked.Price:F0} vol={order.FilledVolume}");
        
        switch (tracked.Type)
        {
            case "entry_tp":
                // Entry TP заполнился — round trip
                _strategy.OnEntryTp();
                lock (_orderLock) { _trackedOrders.Remove(order.BrokerOrderId); }
                _entryTpOrderId = null;
                
                // Ставим новый TP для entry (на уровне entry price, чтобы закрыть безубыток)
                // Нет — entry закрыт, оставшиеся только grid уровни
                break;
                
            case "grid_buy":
                // Grid лимитка заполнилась — уровень активен, ставим TP лимитку
                _strategy.OnGridFill(tracked.LevelIndex);
                
                // Убираем из отслеживания buy ордер
                lock (_orderLock) { _trackedOrders.Remove(order.BrokerOrderId); }
                
                // Ставим TP лимитку
                int dir = _strategy.PositionDirection;
                double tpPrice = tracked.IsBuy 
                    ? tracked.Price + _strategy.Params.GridSpread   // long: TP выше
                    : tracked.Price - _strategy.Params.GridSpread;  // short: TP ниже
                tpPrice = Math.Round(tpPrice);
                
                var tpOrder = new Order
                {
                    Ticker = _ticker,
                    Direction = tracked.IsBuy ? SignalDirection.Sell : SignalDirection.Buy,
                    Type = OrderType.Limit,
                    Price = tpPrice,
                    Volume = tracked.Volume,
                    Comment = $"Grid[{tracked.LevelIndex}] TP"
                };
                
                _ = Task.Run(async () =>
                {
                    try
                    {
                        var result = await _broker.PlaceOrderAsync(tpOrder);
                        lock (_orderLock)
                        {
                            _trackedOrders[result.BrokerOrderId] = new TrackedOrder
                            {
                                BrokerOrderId = result.BrokerOrderId,
                                Type = "grid_tp",
                                LevelIndex = tracked.LevelIndex,
                                Price = tpPrice,
                                Volume = tracked.Volume,
                                IsBuy = !tracked.IsBuy
                            };
                        }
                        _strategy.SetGridOrderIds(tracked.LevelIndex, null, result.BrokerOrderId);
                        Console.WriteLine($"[GRID] 📌 TP limit for level {tracked.LevelIndex} @ {tpPrice:F0}");
                        
                        // Ставим следующий grid уровень
                        _nextGridLevel++;
                        await PlaceNextGridLevelAsync();
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"[GRID] ❌ TP order failed for level {tracked.LevelIndex}: {ex.Message}");
                    }
                });
                break;
                
            case "grid_tp":
                // TP заполнился — round trip!
                _strategy.OnGridTp(tracked.LevelIndex);
                lock (_orderLock) { _trackedOrders.Remove(order.BrokerOrderId); }
                
                // Ставим новый grid buy лимитку на том же уровне (recycling)
                dir = _strategy.PositionDirection;
                if (dir != 0 && !_closingAll)
                {
                // Recycle: ставим grid_buy обратно на оригинальной цене
                double recycleBuyPrice = tracked.OriginalPrice > 0 ? tracked.OriginalPrice : tracked.Price;
                    recycleBuyPrice = Math.Round(recycleBuyPrice);
                    bool recycleIsBuy = dir == 1; // Long→buy, Short→sell
                    
                    var recycleOrder = new Order
                    {
                        Ticker = _ticker,
                        Direction = recycleIsBuy ? SignalDirection.Buy : SignalDirection.Sell,
                        Type = OrderType.Limit,
                        Price = recycleBuyPrice,
                        Volume = tracked.Volume,
                        Comment = $"Grid[{tracked.LevelIndex}] recycle"
                    };
                    
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            var result = await _broker.PlaceOrderAsync(recycleOrder);
                            lock (_orderLock)
                            {
                                _trackedOrders[result.BrokerOrderId] = new TrackedOrder
                                {
                                    BrokerOrderId = result.BrokerOrderId,
                                    Type = "grid_buy",
                                    LevelIndex = tracked.LevelIndex,
                                    Price = recycleBuyPrice,
                                    Volume = tracked.Volume,
                                    IsBuy = recycleIsBuy,
                                    OriginalPrice = recycleBuyPrice
                                };
                            }
                            _strategy.SetGridOrderIds(tracked.LevelIndex, result.BrokerOrderId, null);
                            Console.WriteLine($"[GRID] ♻️ Recycle level {tracked.LevelIndex} @ {recycleBuyPrice:F0}");
                        }
                        catch (Exception ex)
                        {
                            Console.WriteLine($"[GRID] ❌ Recycle failed for level {tracked.LevelIndex}: {ex.Message}");
                        }
                    });
                }
                break;
        }
    }

    // ==================== УПРАВЛЕНИЕ ====================

    public void Start()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Running;
        Console.WriteLine("[CMD] ▶️ СТАРТ — Grid MM v6 торгует (лимитные ордера)");
        
        // ForceEntry если включено и нет позиции
        if (_warmedUp && _forceEntryOnStart && _strategy.OpenLots == 0)
        {
            _ = ForceEntryAsync();
        }
    }
    
    public void StopTrading()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Stopped;
        Console.WriteLine("[CMD] ⏹️ СТОП — закрытие позиций и отмена лимиток");
        
        // Немедленно отменяем все ордера и закрываем позицию
        _ = ExecuteCloseAllAsync(new GridMmRegimeStrategy.StrategyEvent
        {
            Type = GridMmRegimeStrategy.StrategyEvent.EventType.CloseAllMarket,
            Direction = _strategy.PositionDirection,
            Volume = _strategy.OpenLots,
            Reason = "Manual stop"
        });
    }

    public void Pause()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Paused;
        Console.WriteLine("[CMD] ⏸️ ПАУЗА");
    }

    private async Task BroadcastTrade(string direction, int volume, double price, string comment)
    {
        if (_hub == null) return;
        try
        {
            var tradeEvent = new TradeEvent
            {
                Time = DateTime.UtcNow,
                Ticker = _ticker,
                Direction = direction,
                Price = price,
                Volume = volume,
                Strategy = "Grid MM v6",
                Comment = comment
            };
            await _hub.Clients.All.SendAsync("OnTradeExecuted", tradeEvent);
        } catch { }
    }

    private async Task BroadcastPosition()
    {
        if (_hub == null) return;
        try
        {
            var positions = await _broker.GetPositionsAsync();
            var pos = positions.FirstOrDefault(p => p.Ticker == _ticker);
            if (pos != null && pos.Entries.Count > 0)
            {
                var e = pos.Entries[0];
                var posEvent = new PositionEvent
                {
                    Ticker = _ticker,
                    Direction = pos.Direction.ToString(),
                    EntryPrice = e.Price,
                    Volume = e.Volume,
                    CurrentPrice = 0,
                    PnL = 0
                };
                await _hub.Clients.All.SendAsync("OnPositionUpdated", posEvent);
            }
            else
            {
                await _hub.Clients.All.SendAsync("OnPositionUpdated", new PositionEvent
                {
                    Ticker = _ticker, Direction = "None", EntryPrice = 0, Volume = 0, CurrentPrice = 0, PnL = 0
                });
            }
        } catch { }
    }

    public async Task BroadcastStatus()
    {
        if (_hub == null) return;
        try
        {
            var status = GetStatus();
            await _hub.Clients.All.SendAsync("OnStatusUpdate", new { detail = status });
            await BroadcastPosition();
            // Account balance
            try {
                if (_broker.IsConnected) {
                    var balance = await _broker.GetBalanceAsync();
                    await _hub.Clients.All.SendAsync("OnEquityUpdate", balance);
                }
            } catch { }
        } catch { }
    }
    
    public string GetStatus()
    {
        int tracked;
        lock (_orderLock) { tracked = _trackedOrders.Count; }
        
        string gridInfo = _strategy.PositionDirection != 0 
            ? $"OpenLots={_strategy.OpenLots} GridFilled={_strategy.FilledGridLevels} " +
              $"RT={_strategy.OpenLots} TrackedOrders={tracked} "
            : "";
            
        return $"Mode={_strategy.Mode} | Pos={_strategy.PositionDirection} | " +
               $"Lots={_strategy.CurrentLotLevel} | {_strategy.GetStatus()} | " +
               $"{gridInfo}Connected={IsConnected}";
    }

    private async Task HandleDisconnect()
    {
        Console.WriteLine("[GRID-MM-v6] ⚠️ Разрыв соединения...");
        await Task.Delay(5000);
        
        if (_broker.IsConnected)
        {
            Console.WriteLine("[GRID-MM-v6] ✅ Соединение восстановлено");
            _ = RestoreStateAsync();
        }
    }

    /// <summary>
    /// Восстановление состояния после разрыва связи или при старте.
    /// Проверяет позицию, ордера и расставляет недостающие лимитки.
    /// </summary>
    private async Task RestoreStateAsync()
        {
            try
            {
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} RestoreStateAsync started\n");
                Console.WriteLine("[GRID-MM-v6] 🔄 Восстановление состояния...");
                
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} RestClient={_broker.RestClient != null} accountId={_accountId}\n");
                
                // 1. Проверяем открытую позицию через /api/positions (надёжный источник)
                Position? position = null;
                if (!string.IsNullOrEmpty(_accountId))
                {
                    try
                    {
                        // Получаем позиции через локальный API (он сам использует кэш/QUIK/gRPC)
                        using var httpClient = new HttpClient();
                        httpClient.BaseAddress = new Uri("http://localhost:5050");
                        var response = await httpClient.GetAsync($"/api/positions?accountId={_accountId}");
                        if (response.IsSuccessStatusCode)
                        {
                            var positionsJson = await response.Content.ReadAsStringAsync();
                            var positionsData = System.Text.Json.JsonDocument.Parse(positionsJson);
                            var posArray = positionsData.RootElement.EnumerateArray();
                            
                            foreach (var pos in posArray)
                            {
                                string ticker = pos.GetProperty("ticker").GetString() ?? "";
                                if (ticker == _ticker)
                                {
                                    string dirStr = pos.GetProperty("dir").GetString() ?? "";
                                    int qty = pos.GetProperty("qty").GetInt32();
                                    double avgPrice = pos.GetProperty("avgPrice").GetDouble();
                                    
                                    if (qty != 0)
                                    {
                                        int dir = dirStr == "Buy" ? 1 : -1;
                                        int lots = Math.Abs(qty);
                                        
                                        position = new Position
                                        {
                                            Ticker = _ticker,
                                            Direction = dir > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                                            Entries = new List<PositionEntry>
                                            {
                                                new PositionEntry { Price = avgPrice, Volume = lots }
                                            }
                                        };
                                        Console.WriteLine($"[GRID-MM-v6] 📌 /api/positions позиция: {dirStr} {lots}x @ {avgPrice:F0}");
                                        System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} /api/positions found: dir={dir} avgPrice={avgPrice} lots={lots}\n");
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"[GRID-MM-v6] ⚠️ /api/positions failed: {ex.Message}");
                        System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} /api/positions failed: {ex.Message}\n");
                    }
                }
                
                // Fallback: старый метод через брокера
                if (position == null && _broker.RestClient != null && !string.IsNullOrEmpty(_accountId))
            {
                try
                {
                    var acc = await _broker.RestClient.GetAccountAsync(_accountId);
                    var posRow = acc.Positions.FirstOrDefault(p => p.Symbol == _ticker);
                    if (posRow != null && posRow.Balance != 0)
                    {
                        int dir = posRow.Balance > 0 ? 1 : -1;
                        int lots = (int)Math.Abs(posRow.Balance);
                        
                        // AveragePrice может быть null — используем CurrentPrice как fallback
                        double avgPrice = posRow.AveragePrice ?? posRow.CurrentPrice ?? 0;
                        
                        // Создаём Position object для стратегии
                        position = new Position
                        {
                            Ticker = _ticker,
                            Direction = dir > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                            Entries = new List<PositionEntry>
                            {
                                new PositionEntry { Price = avgPrice, Volume = lots }
                            }
                        };
                        Console.WriteLine($"[GRID-MM-v6] 📌 REST позиция: {posRow.Balance}x @ {avgPrice:F0}");
                    }
                    else
                    {
                        Console.WriteLine("[GRID-MM-v6] ✅ Нет открытых позиций (REST)");
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[GRID-MM-v6] ⚠️ REST position failed: {ex.Message}");
                    System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} REST failed: {ex.Message}\n");
                }
            }
            
            System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} After REST, position={(position != null ? "FOUND" : "NULL")}\n");
            
            // Fallback: gRPC если REST недоступен или нет данных
            if (position == null)
            {
                var positions = await _broker.GetPositionsAsync();
                position = positions.FirstOrDefault(p => p.Ticker == _ticker);
                if (position != null)
                {
                    Console.WriteLine("[GRID-MM-v6] 📌 gRPC позиция (fallback): " +
                        $"{position.Direction} {position.Entries[0].Volume}x @ {position.Entries[0].Price:F0}");
                }
            }
            
            if (position != null)
            {
                // Позиция открыта — восстанавливаем
                int dir = position.Direction == SignalDirection.Buy ? 1 : -1;
                double entryPrice = position.Entries[0].Price;
                int lots = position.Entries[0].Volume;
                
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} Position found: dir={dir} entry={entryPrice} lots={lots}\n");
                
                // Восстанавливаем состояние стратегии (УБРАН ForceEntryOnStart check)
                _strategy.RestorePosition(dir, entryPrice, lots);
                
                // 2. Проверяем активные ордера
                List<Order> activeOrders;
                try
                {
                    activeOrders = (await _broker.GetActiveOrdersAsync()).ToList();
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[GRID-MM-v6] ⚠️ GetActiveOrders failed: {ex.Message}");
                    System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} GetActiveOrders failed: {ex.Message}\n");
                    activeOrders = new List<Order>(); // Пустой список — продолжим восстановление
                }
                
                lock (_orderLock)
                {
                    _trackedOrders.Clear();
                    foreach (var order in activeOrders)
                    {
                        bool isBuy = order.Direction == SignalDirection.Buy;
                        int levelIndex = CalculateGridLevel(entryPrice, order.Price, dir);
                        string orderType = DetermineOrderType(levelIndex, dir, isBuy);
                        _trackedOrders[order.BrokerOrderId] = new TrackedOrder
                        {
                            BrokerOrderId = order.BrokerOrderId,
                            Type = orderType,
                            LevelIndex = levelIndex,
                            Price = order.Price,
                            Volume = order.Volume,
                            IsBuy = isBuy
                        };
                    }
                }
                Console.WriteLine($"[GRID-MM-v6] 📊 Восстановлено {_trackedOrders.Count} ордеров из брокера");
                
                // 3. Расставляем недостающие лимитки грида
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} Calling PlaceMissingGridLimitsAsync\n");
                await PlaceMissingGridLimitsAsync(dir, entryPrice);
                System.IO.File.AppendAllText("/tmp/mm-debug.log", $"{DateTime.UtcNow:HH:mm:ss} PlaceMissingGridLimitsAsync done\n");
            }
            else
            {
                Console.WriteLine("[GRID-MM-v6] ✅ Нет открытых позиций");
                _strategy.ClearPosition();
                lock (_orderLock)
                {
                    _trackedOrders.Clear();
                }
            }
            
            Console.WriteLine("[GRID-MM-v6] ✅ Восстановление завершено");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[GRID-MM-v6] ❌ Ошибка восстановления: {ex.Message}");
        }
    }
    
    /// <summary>
    /// Рассчитывает уровень грида по цене
    /// </summary>
    private int CalculateGridLevel(double entryPrice, double orderPrice, int positionDir)
    {
        double step = _strategy.Params.GridStep;
        int levels = (int)Math.Round(Math.Abs(orderPrice - entryPrice) / step);
        return levels;
    }
    
    /// <summary>
    /// Определяет тип ордера
    /// </summary>
    private string DetermineOrderType(int levelIndex, int positionDir, bool isBuy)
    {
        if (levelIndex == 0)
            return "entry_tp";
        
        // Для лонга: grid_buy = против позиции (ниже), grid_tp = в нашу сторону (выше)
        // Для шорта: наоборот
        bool isGridLimit;
        if (positionDir == 1) // Long
            isGridLimit = isBuy;  // Buy = против позиции
        else // Short
            isGridLimit = !isBuy; // Sell = против позиции
        
        return isGridLimit ? "grid_buy" : "grid_tp";
    }
    
    /// <summary>
    /// Расставляет недостающие уровни грида
    /// </summary>
    private async Task PlaceMissingGridLimitsAsync(int positionDir, double entryPrice)
    {
        var cfg = _strategy.Params;
        int maxLevels = cfg.MaxGridLevels;
        double step = cfg.GridStep;
        
        // Проверяем какие уровни уже есть
        HashSet<int> existingLevels;
        lock (_orderLock)
        {
            existingLevels = _trackedOrders.Values
                .Where(o => o.Type == "grid_buy")
                .Select(o => o.LevelIndex)
                .ToHashSet();
        }
        
        int placed = 0;
        for (int i = 1; i <= maxLevels; i++)
        {
            if (existingLevels.Contains(i)) continue;
            
            // Рассчитываем цену уровня
            double levelPrice = positionDir == 1
                ? entryPrice - i * step  // Long: buy ниже
                : entryPrice + i * step; // Short: sell выше
            
            // Ставим лимитку
            var order = new Order
            {
                Ticker = _ticker,
                Direction = positionDir == 1 ? SignalDirection.Buy : SignalDirection.Sell,
                Type = OrderType.Limit,
                Price = levelPrice,
                Volume = _strategy.CurrentLotLevel,
                Comment = $"grid_level_{i}"
            };
            
            var result = await _broker.PlaceOrderAsync(order);
            
            lock (_orderLock)
            {
                _trackedOrders[result.BrokerOrderId] = new TrackedOrder
                {
                    BrokerOrderId = result.BrokerOrderId,
                    Type = "grid_buy",
                    LevelIndex = i,
                    Price = levelPrice,
                    Volume = _strategy.CurrentLotLevel,
                    IsBuy = positionDir == 1,
                    OriginalPrice = levelPrice
                };
            }
            
            placed++;
            Console.WriteLine($"[GRID-MM-v6] 📊 Восстановлен уровень {i}: {order.Direction} {cfg.MaxLots}x @ {levelPrice:F0}");
        }
        
        if (placed > 0)
            Console.WriteLine($"[GRID-MM-v6] ✅ Расставлено {placed} недостающих уровней");
    }
    
    /// <summary>
    /// Периодическая проверка: если отслеживаемый ордер отменён брокером — удаляем из tracking
    /// и если есть позиция — перевыставляем недостающие уровни грида
    /// </summary>
    private async Task VerifyAndRestoreLostOrdersAsync()
    {
        try
        {
            // Получаем активные ордера от брокера
            var activeOrders = await _broker.GetActiveOrdersAsync();
            var activeIds = new HashSet<string>(activeOrders.Select(o => o.BrokerOrderId));
            
            // Находим отменённые ордера в tracking
            List<string> lostOrders;
            List<TrackedOrder> trackedCopy;
            lock (_orderLock)
            {
                lostOrders = _trackedOrders
                    .Where(kvp => !activeIds.Contains(kvp.Key))
                    .Select(kvp => kvp.Key)
                    .ToList();
                trackedCopy = _trackedOrders.Values.ToList();
            }
            
            if (lostOrders.Count == 0) return; // Все ок
            
            Console.WriteLine($"[GRID-MM-v6] ⚠️ Обнаружено {lostOrders.Count} потерянных ордеров");
            
            // Удаляем отменённые из tracking
            lock (_orderLock)
            {
                foreach (var orderId in lostOrders)
                {
                    if (_trackedOrders.TryGetValue(orderId, out var tracked))
                    {
                        Console.WriteLine($"[GRID-MM-v6] 🗑️ Удалён из tracking: {tracked.Type} L{tracked.LevelIndex} @ {tracked.Price:F0}");
                        _trackedOrders.Remove(orderId);
                    }
                }
            }
            
            // Если есть позиция — восстанавливаем недостающие уровни
            if (_strategy.PositionDirection != 0)
            {
                // Получаем позицию от стратегии
                int dir = _strategy.PositionDirection;
                double entryPrice = _strategy.EntryPrice;
                
                Console.WriteLine($"[GRID-MM-v6] 🔄 Восстановление грида: dir={dir} entry={entryPrice:F0}");
                await PlaceMissingGridLimitsAsync(dir, entryPrice);
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[GRID-MM-v6] ❌ VerifyAndRestoreLostOrders: {ex.Message}");
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _broker.OnOrderUpdate -= OnOrderUpdate;
        _broker.Dispose();
    }
}
// DEBUG: temporarily log to file
