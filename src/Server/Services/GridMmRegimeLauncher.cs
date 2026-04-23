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

    public GridMmRegimeStrategy Strategy => _strategy;
    public bool IsConnected => _useQuikData || _broker.IsConnected;

    public GridMmRegimeLauncher(string finamToken, string ticker = "SiM6", string accountId = "", IHubContext<TradingHub>? hub = null, bool useQuikData = false)
    {
        _useQuikData = useQuikData;
        _ticker = ticker;
        _hub = hub;
        _broker = new FinamConnector();
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

        _ = ConnectAndWarm(finamToken, accountId);
    }

    private async Task ConnectAndWarm(string token, string accountId)
    {
        try
        {
            if (_useQuikData)
        {
            Console.WriteLine("[GRID-MM-v6] 📡 QUIK данные + Finam для ордеров...");
        }
        else
        {
            Console.WriteLine($"[GRID-MM-v6] Подключение к Финам...");
        }
        // Всегда подключаем Finam (ордера идут через gRPC)
        bool ok = await _broker.ConnectAsync(token, accountId);
        if (!ok)
        {
            Console.WriteLine("[GRID-MM-v6] ❌ Finam gRPC не подключился!");
            return;
        }
        Console.WriteLine($"[GRID-MM-v6] ✅ Finam подключён (ордера).");

        // Прогрев: 5-мин свечи
        Console.WriteLine($"[GRID-MM-v6] 📐 Прогрев индикаторов ({_ticker}, 5-мин)...");
        Candle[] history;
        
        if (_useQuikData && _quikProvider != null)
        {
            var quikCandles = await _quikProvider.GetHistoricalCandlesAsync();
            history = quikCandles.ToArray();
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
            try
            {
                for (int i = 0; i < history.Length; i++)
                {
                    _strategy.OnCandle(history[i], _ticker);
                    // Обрабатываем события прогрева (не торгуем)
                    while (_strategy.HasPendingEvents)
                        _strategy.GetNextEvent();
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[GRID-MM-v6] ❌ ОШИБКА ПРОГРЕВА: {ex.Message}");
            }
            Console.WriteLine($"[GRID-MM-v6] ✅ Прогрето. SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0}");
            _warmedUp = true;
            _warmupEndTime = DateTime.UtcNow;
            _lastProcessedCandle = DateTime.UtcNow;
            _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Running; // Автостарт после прогрева
            
            // Принудительный вход если включено
            if (_strategy.Params.ForceEntryOnStart)
            {
                var lastCandle = history[^1];
                Console.WriteLine("[GRID-MM-v6] 🚀 Force entry on start enabled...");
                _strategy.ForceEntry(lastCandle.Close);
                // Обрабатываем сгенерированное событие
                while (_strategy.HasPendingEvents)
                {
                    var evt = _strategy.GetNextEvent();
                    if (evt.Type == GridMmRegimeStrategy.StrategyEvent.EventType.EntryMarket)
                        _ = ExecuteEntryAsync(evt);
                }
            }
            
            // Восстанавливаем состояние (если есть открытая позиция)
            _ = RestoreStateAsync();
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
    }
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
            Direction = dir == 1 ? SignalDirection.Sell : SignalDirection.Buy, // Grid: ПРОТИВОПОЛОЖНОЕ направление (закрытие позиции)
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
        
        // Force entry если включено и ещё нет позиции
        if (_strategy.Params.ForceEntryOnStart && _strategy.PositionDirection == 0 && _broker != null)
        {
            _ = ForceEntryAsync();
        }
    }
    
    private async Task ForceEntryAsync()
    {
        try
        {
            // Получаем цену из Finam REST (QUIK может быть устаревшим)
            double price = 0;
            var jwtResp = await new HttpClient().PostAsync("https://api.finam.ru/v1/sessions",
                new StringContent($"{{\"secret\": \"{Environment.GetEnvironmentVariable("FINAM_API_KEY")}\"}}", System.Text.Encoding.UTF8, "application/json"));
            if (!jwtResp.IsSuccessStatusCode) { Console.WriteLine("[FORCE] ❌ Cannot get JWT"); return; }
            var jwtDoc = System.Text.Json.JsonDocument.Parse(await jwtResp.Content.ReadAsStringAsync());
            var token = jwtDoc.RootElement.GetProperty("token").GetString();
            
            using var quoteReq = new HttpRequestMessage(HttpMethod.Get, "https://api.finam.ru/v1/instruments/SiM6@RTSX/quotes/latest");
            quoteReq.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
            var quoteResp = await new HttpClient().SendAsync(quoteReq);
            if (quoteResp.IsSuccessStatusCode)
            {
                var qDoc = System.Text.Json.JsonDocument.Parse(await quoteResp.Content.ReadAsStringAsync());
                if (qDoc.RootElement.TryGetProperty("quote", out var q) && q.TryGetProperty("last", out var lEl))
                    price = double.Parse(lEl.GetProperty("value").GetString() ?? "0");
            }
            
            if (price <= 0)
            {
                Console.WriteLine("[GRID-MM-v6] ⚠️ Force entry: no price from Finam REST");
                return;
            }
            
            // Прогреваем индикаторы из Finam REST свечей если ещё нет
            if (_strategy.CurrentEma == 0 && _strategy.CurrentSar == 0)
            {
                Console.WriteLine("[FORCE] Warming indicators from REST candles...");
                try
                {
                    var from = DateTime.UtcNow.AddDays(-5);
                    var to = DateTime.UtcNow;
                    var barsReq = new HttpRequestMessage(HttpMethod.Get,
                        $"https://api.finam.ru/v1/instruments/SiM6@RTSX/bars?timeframe=TIME_FRAME_M5&interval.start_time={from:yyyy-MM-ddTHH:mm:ssZ}&interval.end_time={to:yyyy-MM-ddTHH:mm:ssZ}");
                    barsReq.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
                    var barsResp = await new HttpClient().SendAsync(barsReq);
                    if (barsResp.IsSuccessStatusCode)
                    {
                        var barsDoc = System.Text.Json.JsonDocument.Parse(await barsResp.Content.ReadAsStringAsync());
                        if (barsDoc.RootElement.TryGetProperty("bars", out var bars))
                        {
                            int count = 0;
                            foreach (var b in bars.EnumerateArray())
                            {
                                double Open(string n) => double.Parse(b.TryGetProperty(n, out var x) ? x.GetProperty("value").GetString() ?? "0" : "0");
                                var c = new Candle { Open = Open("open"), High = Open("high"), Low = Open("low"), Close = Open("close") };
                                _strategy.OnCandle(c, "SiM6");
                                count++;
                            }
                            Console.WriteLine($"[FORCE] Warmed with {count} candles: SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0}");
                        }
                    }
                }
                catch (Exception ex) { Console.WriteLine($"[FORCE] Warmup failed: {ex.Message}"); }
            }
            
            Console.WriteLine($"[GRID-MM-v6] 🚀 Force entry @ {price:F0} (SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0})");
            _strategy.ForceEntry(price);
            while (_strategy.HasPendingEvents)
            {
                var evt = _strategy.GetNextEvent();
                if (evt.Type == GridMmRegimeStrategy.StrategyEvent.EventType.EntryMarket)
                    await ExecuteEntryAsync(evt);
                else if (evt.Type == GridMmRegimeStrategy.StrategyEvent.EventType.GridLimitOrders)
                {
                    Console.WriteLine($"[FORCE ENTRY] 📊 Placing grid: {evt.GridPrices.Length} levels");
                    await PlaceGridLimitsAsync(evt);
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[GRID-MM-v6] ⚠️ Force entry failed: {ex.Message}");
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
            Console.WriteLine("[GRID-MM-v6] 🔄 Восстановление состояния...");
            
            // 1. Проверяем открытую позицию
            var positions = await _broker.GetPositionsAsync();
            var position = positions.FirstOrDefault(p => p.Ticker == _ticker);
            
            if (position != null)
            {
                // Позиция открыта — восстанавливаем
                int dir = position.Direction == SignalDirection.Buy ? 1 : -1;
                double entryPrice = position.Entries[0].Price;
                int lots = position.Entries[0].Volume;
                
                Console.WriteLine($"[GRID-MM-v6] 📌 Найдена позиция: {position.Direction} {lots}x @ {entryPrice:F0}");
                
                // Восстанавливаем состояние стратегии
                _strategy.RestorePosition(dir, entryPrice, lots);
                
                // 2. Проверяем активные ордера
                var activeOrders = await _broker.GetActiveOrdersAsync();
                lock (_orderLock)
                {
                    _trackedOrders.Clear();
                    foreach (var order in activeOrders)
                    {
                        // Определяем тип ордера по цене относительно позиции
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
                await PlaceMissingGridLimitsAsync(dir, entryPrice);
            }
            else
            {
                Console.WriteLine("[GRID-MM-v6] ✅ Нет открытых позиций");
                // Сбрасываем состояние стратегии
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

    public void Dispose()
    {
        _cts?.Cancel();
        _broker.OnOrderUpdate -= OnOrderUpdate;
        _broker.Dispose();
    }
}
