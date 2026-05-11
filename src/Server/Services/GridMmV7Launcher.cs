using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Grid MM v7 Launcher — rewritten with 4-component architecture.
///
/// Components:
/// 1. PositionTracker — pure state, NO I/O
/// 2. OrderManager — broker I/O
/// 3. GridEngine — pure calculation
/// 4. MainLoop — orchestration (this class)
///
/// Rules:
/// - One MainLoop every 200ms
/// - entryPrice = sacred, NEVER overwritten
/// - Fill detection via tracked order IDs (not lots comparison)
/// - One grid + one TP at a time
/// - V8Launcher inherits from this class
/// </summary>
public class GridMmV7Launcher : IDisposable
{
    // ================================================================
    // 1. POSITION TRACKER — pure state, NO I/O
    // ================================================================
    protected class PositionTracker
    {
        public int Direction { get; private set; }
        public double EntryPrice { get; private set; }
        public int EntryLots { get; private set; }
        public int FilledLevels { get; private set; }
        public int RoundTrips { get; private set; }
        public double RealizedPnL { get; private set; }
        public int TotalLots => EntryLots + FilledLevels;
        public bool HasPosition => Direction != 0;

        public void OnEntry(int direction, double entryPrice, int lots)
        {
            Direction = direction;
            EntryPrice = entryPrice;
            EntryLots = lots;
            FilledLevels = 0;
            RoundTrips = 0;
            RealizedPnL = 0;
        }

        public void OnGridFill(int level, double fillPrice)
        {
            FilledLevels++;
        }

        public void OnTpFill(double pnl)
        {
            RoundTrips++;
            RealizedPnL += pnl;
            if (FilledLevels > 0)
                FilledLevels--;
        }

        public void OnClose()
        {
            Direction = 0;
            EntryPrice = 0;
            EntryLots = 0;
            FilledLevels = 0;
        }

        /// <summary>Reset grid to level 1 after TP fill (mill logic)</summary>
        public void ResetGridToLevel1()
        {
            // After TP fill, we already decremented FilledLevels in OnTpFill
            // Just signal that grid level should reset to 1
        }

        public void Restore(int direction, double entryPrice, int totalLots, int filledLevels, int roundTrips, double realizedPnL)
        {
            Direction = direction;
            EntryPrice = entryPrice;
            EntryLots = totalLots - filledLevels;
            FilledLevels = filledLevels;
            RoundTrips = roundTrips;
            RealizedPnL = realizedPnL;
        }
    }

    // ================================================================
    // 2. ORDER MANAGER — broker I/O
    // ================================================================
    protected class OrderManager
    {
        private readonly FinamConnector _broker;
        private readonly string _accountId;
        private readonly string _logPrefix;
        private readonly string _orderPrefix;

        /// <summary>Tracked grid order ID. Null = no grid tracked.</summary>
        public string? TrackedGridId { get; set; }
        /// <summary>Tracked TP order ID. Null = no TP tracked.</summary>
        public string? TrackedTpId { get; set; }

        /// <summary>Price of currently tracked grid order.</summary>
        public double TrackedGridPrice { get; set; }
        /// <summary>Level of currently tracked grid order.</summary>
        public int TrackedGridLevel { get; set; }

        /// <summary>Set to true when WE cancel an order (to distinguish fill from cancel)</summary>
        private bool _gridCancelRequested;
        private bool _tpCancelRequested;

        public OrderManager(FinamConnector broker, string accountId, string logPrefix, string orderPrefix)
        {
            _broker = broker;
            _accountId = accountId;
            _logPrefix = logPrefix;
            _orderPrefix = orderPrefix;
        }

        public async Task<string?> PlaceOrder(string symbol, string side, string type, double price, int qty, string comment)
        {
            try
            {
                var rest = _broker.RestClient;
                if (rest == null) return null;

                var request = new PlaceOrderRequest
                {
                    Symbol = symbol,
                    Quantity = new() { Value = qty.ToString() },
                    Side = side,
                    OrderType = type,
                    Comment = comment
                };

                if (type == "ORDER_TYPE_LIMIT" && price > 0)
                    request.Price = new() { Value = ((int)price).ToString() };

                var result = await rest.PlaceOrderAsync(_accountId, request);
                return result?.OrderId;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[{_logPrefix}] PlaceOrder error ({comment}): {ex.Message}");
                return null;
            }
        }

        /// <summary>Cancel order and wait for confirmation (200ms delay + verify).</summary>
        public async Task<bool> CancelAndWait(string? orderId)
        {
            if (string.IsNullOrEmpty(orderId) || orderId == "pending") return false;

            try
            {
                var rest = _broker.RestClient;
                if (rest == null) return false;

                await rest.CancelOrderAsync(_accountId, orderId);
                await Task.Delay(200);

                // Verify it's gone
                var active = await GetActiveOrders();
                if (active.Any(o => o.orderId == orderId))
                {
                    Console.WriteLine($"[{_logPrefix}] CancelAndWait: {orderId} still active after cancel");
                    return false;
                }
                return true;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[{_logPrefix}] CancelAndWait error: {ex.Message}");
                return false;
            }
        }

        /// <summary>Cancel a single order (fire-and-forget).</summary>
        public async Task CancelOrder(string? orderId)
        {
            if (string.IsNullOrEmpty(orderId) || orderId == "pending") return;
            try
            {
                var rest = _broker.RestClient;
                if (rest != null) await rest.CancelOrderAsync(_accountId, orderId);
            }
            catch { }
        }

        /// <summary>Cancel all orders matching the prefix.</summary>
        public async Task CancelAllByPrefix()
        {
            try
            {
                var orders = await GetActiveOrders();
                foreach (var o in orders)
                {
                    if (o.comment.StartsWith(_orderPrefix))
                    {
                        await CancelOrder(o.orderId);
                    }
                }
            }
            catch { }
        }

        /// <summary>Get active orders from broker.</summary>
        public async Task<List<(string orderId, double price, string comment, string side)>> GetActiveOrders()
        {
            var result = new List<(string, double, string, string)>();
            try
            {
                var rest = _broker.RestClient;
                if (rest == null) return result;
                var orders = await rest.GetOrdersAsync(_accountId);
                if (orders?.Orders == null) return result;
                foreach (var o in orders.Orders)
                {
                    string comment = o.Details?.Comment ?? "";
                    double price = o.Price;
                    bool isActive = o.Status == "ORDER_STATUS_NEW";
                    if (isActive)
                        result.Add((o.OrderId, price, comment, o.Side));
                }
            }
            catch { }
            return result;
        }

        /// <summary>Mark that WE are cancelling grid (to not confuse with fill)</summary>
        public void MarkGridCancelRequested() { _gridCancelRequested = true; }
        public void MarkTpCancelRequested() { _tpCancelRequested = true; }
        public bool ConsumeGridCancelFlag()
        {
            if (_gridCancelRequested) { _gridCancelRequested = false; return true; }
            return false;
        }
        public bool ConsumeTpCancelFlag()
        {
            if (_tpCancelRequested) { _tpCancelRequested = false; return true; }
            return false;
        }

        /// <summary>Place market order.</summary>
        public async Task PlaceMarketOrder(string symbol, string side, int lots, string comment)
        {
            try
            {
                var rest = _broker.RestClient;
                if (rest == null) return;
                await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
                {
                    Symbol = symbol,
                    Quantity = new() { Value = lots.ToString() },
                    Side = side,
                    OrderType = "ORDER_TYPE_MARKET",
                    Comment = comment
                });
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[{_logPrefix}] Market order error: {ex.Message}");
            }
        }
    }

    // ================================================================
    // 3. GRID ENGINE — pure calculation, no broker knowledge
    // ================================================================
    protected class GridEngine
    {
        /// <summary>
        /// Calculate next grid level prices.
        /// Returns (gridPrice, tpPrice, level) or null if max levels reached.
        /// </summary>
        public (double gridPrice, double tpPrice, int level)? NextLevel(
            double entryPrice, int direction, int filledLevels,
            double gridStep, double gridSpread, int maxLevels)
        {
            int level = filledLevels + 1;
            if (level > maxLevels) return null;

            double gridPrice, tpPrice;
            if (direction == 1) // LONG: grid below, TP above grid
            {
                gridPrice = entryPrice - level * gridStep;
                tpPrice = gridPrice + gridSpread;
            }
            else // SHORT: grid above, TP below grid
            {
                gridPrice = entryPrice + level * gridStep;
                tpPrice = gridPrice - gridSpread;
            }

            return (gridPrice, tpPrice, level);
        }

        /// <summary>
        /// Calculate TP price for a filled grid level.
        /// </summary>
        public double TpPriceForLevel(double gridPrice, int direction, double gridSpread)
        {
            return direction == 1
                ? gridPrice + gridSpread   // LONG: sell above grid fill
                : gridPrice - gridSpread;  // SHORT: buy below grid fill
        }
    }

    // ================================================================
    // 4. MAIN CLASS — fields, orchestration, public API
    // ================================================================

    protected readonly FinamConnector _broker;
    protected readonly GridMmV7Strategy _strategy;
    protected readonly bool _isV8;
    protected readonly string _stateFile;
    protected string _finamSymbol;
    protected string _ticker;
    protected readonly string _accountId;
    protected readonly string _logPrefix;

    protected readonly PositionTracker _tracker;
    protected readonly OrderManager _orders;
    protected readonly GridEngine _gridEngine;

    // Loop control
    private DateTime _lastCandleTime = DateTime.MinValue;
    private System.Threading.Timer? _mainTimer;
    private bool _loopRunning = false;
    private bool _manualInProgress = false;
    private int _tickCount = 0;

    // Last entry for recovery
    protected double _lastEntryPrice = 0;
    protected int _lastEntryDir = 0;

    // Strategy sync: remember current level for grid engine
    private int _currentGridLevel;

    public string Ticker => _ticker;
    public GridMmV7Strategy Strategy => _strategy;
    public string LogPrefix => _logPrefix;

    public GridMmV7Launcher(FinamConnector broker, GridMmV7Strategy strategy, bool useV8 = false, string? stateFile = null)
    {
        _broker = broker;
        _strategy = strategy;
        _isV8 = useV8;
        _stateFile = stateFile ?? (useV8 ? "/tmp/v8-state.json" : "/tmp/v7-state.json");
        _finamSymbol = "SiM6@RTSX";
        _ticker = "SiM6";
        _accountId = "";
        _logPrefix = useV8 ? "V8" : "V7";

        _tracker = new PositionTracker();
        string prefix = useV8 ? "V8-" : "V7-";
        _orders = new OrderManager(broker, _accountId, _logPrefix, prefix);
        _gridEngine = new GridEngine();
    }

    public void SetInstrument(string ticker)
    {
        _ticker = ticker;
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SiM6"] = "SiM6@RTSX", ["RIM6"] = "RIM6@RTSX",
            ["GDM6"] = "GDM6@RTSX", ["MXM6"] = "MXM6@RTSX",
            ["SPM6"] = "SPM6@RTSX", ["MMM6"] = "MMM6@RTSX",
        };
        _finamSymbol = map.TryGetValue(ticker, out var s) ? s : ticker + "@RTSX";
    }

    // === PUBLIC API ===

    public async Task StartAsync()
    {
        if (_strategy.CurrentMode == GridMmV7Strategy.Mode.Running) return;

        _strategy.InitIndicators();
        _strategy.CurrentMode = GridMmV7Strategy.Mode.Running;

        // Restore state from file
        try
        {
            if (System.IO.File.Exists(_stateFile))
            {
                var json = System.IO.File.ReadAllText(_stateFile);
                var doc = System.Text.Json.JsonDocument.Parse(json);
                var root = doc.RootElement;

                int dir = root.GetProperty("dir").GetInt32();
                double entry = root.GetProperty("entryPrice").GetDouble();
                int entryLots = root.GetProperty("entryLots").GetInt32();
                int filled = root.GetProperty("filledLevels").GetInt32();
                int rt = root.GetProperty("roundTrips").GetInt32();
                double pnl = root.GetProperty("realizedPnL").GetDouble();
                double lastEntry = root.TryGetProperty("lastEntryPrice", out var lep) ? lep.GetDouble() : entry;
                int lastDir = root.TryGetProperty("lastDir", out var ld) ? ld.GetInt32() : dir;

                _lastEntryPrice = lastEntry;
                _lastEntryDir = lastDir;

                if (dir != 0 && entry > 0)
                {
                    _tracker.Restore(dir, entry, entryLots + filled, filled, rt, pnl);
                    _currentGridLevel = filled + 1;
                    _strategy.RestorePosition(dir, entry, entryLots + filled, filled, rt, pnl);
                    Console.WriteLine($"[{_logPrefix}] State restored: dir={dir} entry={entry:F0}");
                }
            }
        }
        catch { }

        // Warmup in background
        var rest = _broker.RestClient;
        if (rest != null)
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    var bars = await rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M5",
                        DateTime.UtcNow.AddMinutes(-30).ToString("o"),
                        DateTime.UtcNow.ToString("o"));
                    if (bars?.Bars != null)
                    {
                        foreach (var bar in bars.Bars)
                        {
                            _strategy.OnCandle(new Candle
                            {
                                Timestamp = DateTime.Parse(bar.Timestamp),
                                Open = double.Parse(bar.Open.Value),
                                High = double.Parse(bar.High.Value),
                                Low = double.Parse(bar.Low.Value),
                                Close = double.Parse(bar.Close.Value),
                                Volume = (long)double.Parse(bar.Volume?.Value ?? "0")
                            });
                        }
                        while (_strategy.HasPendingEvents) _strategy.GetNextEvent();
                        Console.WriteLine($"[{_logPrefix}] Warmup: {bars.Bars.Count} candles");
                    }
                }
                catch { }
            });
        }

        // Start main loop 200ms
        _mainTimer = new System.Threading.Timer(MainLoopTick, null, TimeSpan.FromMilliseconds(200), TimeSpan.FromMilliseconds(200));
        Console.WriteLine($"[{_logPrefix}] ✅ Started");
    }

    public async Task StopAsync()
    {
        Console.WriteLine($"[{_logPrefix}] ⏹ Stop requested");
        _strategy.CurrentMode = GridMmV7Strategy.Mode.Stopped;
        _mainTimer?.Dispose();
        _mainTimer = null;

        // Cancel all orders
        await _orders.CancelAllByPrefix();
        _orders.TrackedGridId = null;
        _orders.TrackedTpId = null;
        Console.WriteLine($"[{_logPrefix}] Orders cancelled");

        // Close position from broker
        var (bDir, bLots, _) = await GetBrokerPositionAsync();
        if (bLots > 0 && bDir != 0)
        {
            await _orders.PlaceMarketOrder(_finamSymbol, bDir == 1 ? "SIDE_SELL" : "SIDE_BUY", bLots, $"{_logPrefix}-CLOSE: Stop requested");
            Console.WriteLine($"[{_logPrefix}] Closed {bLots} lots");
        }

        _tracker.OnClose();
        _strategy.ClearPosition();
        SaveState();
        Console.WriteLine($"[{_logPrefix}] ⏹ Stopped");
    }

    public async Task PauseAsync()
    {
        _strategy.CurrentMode = GridMmV7Strategy.Mode.Paused;

        // Cancel all orders, keep position
        await _orders.CancelAllByPrefix();
        _orders.TrackedGridId = null;
        _orders.TrackedTpId = null;

        Console.WriteLine($"[{_logPrefix}] ⏸ Paused");
    }

    public async Task ResumeAsync()
    {
        _strategy.CurrentMode = GridMmV7Strategy.Mode.Running;
        Console.WriteLine($"[{_logPrefix}] ▶ Resumed");
    }

    public async Task ForceEntryAsync()
    {
        if (_tracker.HasPosition) return;
        _strategy.ForceEntry(_strategy.CurrentSar);
        if (_strategy.HasPendingEvents)
        {
            var evt = _strategy.GetNextEvent();
            if (evt != null) await ExecuteEntryAsync(evt);
        }
    }

    public async Task MarketBuyAsync()
    {
        _manualInProgress = true;
        try
        {
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();

            // Place market buy
            var orderId = await _orders.PlaceOrder(_finamSymbol, "SIDE_BUY", "ORDER_TYPE_MARKET", 0, 1, $"{_logPrefix}-MANUAL-BUY");
            await Task.Delay(3000);

            double fillPrice = await GetLastTradePriceAsync("BUY");
            if (fillPrice == 0) fillPrice = (await GetBrokerPositionAsync()).avgPrice;
            if (fillPrice == 0) { Console.WriteLine($"[{_logPrefix}] Manual BUY: no fill price"); return; }

            Console.WriteLine($"[{_logPrefix}] Manual BUY fill={fillPrice:F0}");
            _tracker.OnEntry(1, fillPrice, 1);
            _strategy.OnEntryFilled(1, fillPrice, 1);
            _lastEntryPrice = fillPrice;
            _lastEntryDir = 1;
            _currentGridLevel = 1;

            await EnsureGridAndTpAsync();
            SaveState();
        }
        finally
        {
            _manualInProgress = false;
        }
    }

    public async Task MarketSellAsync()
    {
        _manualInProgress = true;
        try
        {
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();

            // Place market sell
            var orderId = await _orders.PlaceOrder(_finamSymbol, "SIDE_SELL", "ORDER_TYPE_MARKET", 0, 1, $"{_logPrefix}-MANUAL-SELL");
            await Task.Delay(3000);

            double fillPrice = await GetLastTradePriceAsync("SELL");
            if (fillPrice == 0) fillPrice = (await GetBrokerPositionAsync()).avgPrice;
            if (fillPrice == 0) { Console.WriteLine($"[{_logPrefix}] Manual SELL: no fill price"); return; }

            Console.WriteLine($"[{_logPrefix}] Manual SELL fill={fillPrice:F0}");
            _tracker.OnEntry(-1, fillPrice, 1);
            _strategy.OnEntryFilled(-1, fillPrice, 1);
            _lastEntryPrice = fillPrice;
            _lastEntryDir = -1;
            _currentGridLevel = 1;

            await EnsureGridAndTpAsync();
            SaveState();
        }
        finally
        {
            _manualInProgress = false;
        }
    }

    public object GetStatus()
    {
        return new
        {
            status = _strategy.CurrentMode.ToString().ToLower(),
            direction = _tracker.Direction,
            dirStr = _tracker.Direction == 1 ? "LONG" : _tracker.Direction == -1 ? "SHORT" : "FLAT",
            entryPrice = _tracker.EntryPrice,
            totalLots = _tracker.TotalLots,
            filledLevels = _tracker.FilledLevels,
            roundTrips = _tracker.RoundTrips,
            totalPnL = Math.Round(_tracker.RealizedPnL, 1),
            currentLevel = _currentGridLevel,
            sar = _strategy.CurrentSar,
            ema = _strategy.CurrentEma,
            connected = true,
            detail = _strategy.GetStatus() +
                $" | Grid={(_orders.TrackedGridId != null ? "active" : "none")}" +
                $" TP={(_orders.TrackedTpId != null ? "active" : "none")}"
        };
    }

    // === MAIN LOOP — один линейный цикл ===

    private async void MainLoopTick(object? state)
    {
        if (_loopRunning || _manualInProgress) return;
        _loopRunning = true;
        try
        {
            if (_strategy.CurrentMode != GridMmV7Strategy.Mode.Running) return;
            await MainLoopAsync();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Loop error: {ex.Message}");
        }
        finally
        {
            _loopRunning = false;
        }
    }

    private async Task MainLoopAsync()
    {
        _tickCount++;

        // === STEP 1: Feed candles to strategy ===
        if ((DateTime.UtcNow - _lastCandleTime).TotalSeconds >= 5)
        {
            _lastCandleTime = DateTime.UtcNow;
            await ProcessCandlesAsync();
        }

        // === STEP 2: Check strategy events (Entry, CloseAll) ===
        // Already handled inside ProcessCandlesAsync

        // === STEP 3: Check fills via tracked order IDs ===
        await DetectFillsAsync();

        // === STEP 4: Ensure grid and TP are placed ===
        if (_tracker.HasPosition)
        {
            await EnsureGridAndTpAsync();
        }

        // === STEP 5: Broker sync (every 30 ticks = ~6s) ===
        if (_tickCount % 30 == 0)
        {
            await BrokerSyncAsync();
        }
    }

    // === FILL DETECTION — order ID based ===

    private async Task DetectFillsAsync()
    {
        if (!_tracker.HasPosition) return;

        var activeOrders = await _orders.GetActiveOrders();
        var activeIds = activeOrders.Select(o => o.orderId).ToHashSet();

        // Check grid fill
        if (_orders.TrackedGridId != null && _orders.TrackedGridId != "pending")
        {
            if (!activeIds.Contains(_orders.TrackedGridId))
            {
                // Order disappeared — was it a fill or our cancel?
                if (_orders.ConsumeGridCancelFlag())
                {
                    // We cancelled it — clear tracking
                    Console.WriteLine($"[{_logPrefix}] Grid cancel confirmed: {_orders.TrackedGridId}");
                    _orders.TrackedGridId = null;
                }
                else
                {
                    // Not our cancel → FILL!
                    Console.WriteLine($"[{_logPrefix}] ⚡ Grid fill detected! Level={_orders.TrackedGridLevel} @{_orders.TrackedGridPrice:F0}");

                    // Update position tracker
                    _tracker.OnGridFill(_orders.TrackedGridLevel, _orders.TrackedGridPrice);
                    _strategy.OnGridLevelFilled(_orders.TrackedGridLevel, _orders.TrackedGridPrice);
                    _currentGridLevel = _orders.TrackedGridLevel + 1;

                    // Clear tracked grid
                    _orders.TrackedGridId = null;

                    // Place TP for the filled level
                    await PlaceTpForFilledLevelAsync(_orders.TrackedGridPrice, _orders.TrackedGridLevel);

                    // Place next grid
                    await PlaceNextGridAsync();

                    SaveState();
                }
            }
        }

        // Check TP fill
        if (_orders.TrackedTpId != null && _orders.TrackedTpId != "pending")
        {
            if (!activeIds.Contains(_orders.TrackedTpId))
            {
                if (_orders.ConsumeTpCancelFlag())
                {
                    // We cancelled it — clear tracking
                    Console.WriteLine($"[{_logPrefix}] TP cancel confirmed: {_orders.TrackedTpId}");
                    _orders.TrackedTpId = null;
                }
                else
                {
                    // FILL!
                    double pnl = _strategy.Params.GridSpread - _strategy.Params.Commission;
                    Console.WriteLine($"[{_logPrefix}] ⚡ TP fill detected! PnL={pnl:F0}");

                    _tracker.OnTpFill(pnl);
                    _strategy.OnRoundTrip(pnl);
                    _strategy.OnTpFilled();
                    _orders.TrackedTpId = null;

                    // Cancel current grid
                    if (_orders.TrackedGridId != null)
                    {
                        _orders.MarkGridCancelRequested();
                        await _orders.CancelAndWait(_orders.TrackedGridId);
                        _orders.TrackedGridId = null;
                    }

                    // Reset grid to level 1 (mill logic)
                    _currentGridLevel = 1;
                    _strategy.ResetGridToLevel1();

                    if (_tracker.HasPosition)
                    {
                        // Place new grid at level 1
                        await PlaceNextGridAsync();
                    }

                    SaveState();
                }
            }
        }

        // Cleanup: if no position at broker, reset everything
        if (!_tracker.HasPosition) return;

        // Check if broker still has position
        var (bDir, bLots, _) = await GetBrokerPositionAsync();
        if (bLots == 0 && _tracker.HasPosition)
        {
            Console.WriteLine($"[{_logPrefix}] No broker position → reset");
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();
            SaveState();
        }
    }

    // === GRID & TP PLACEMENT ===

    /// <summary>Ensure both grid and TP are placed if position exists and no tracked orders.</summary>
    private async Task EnsureGridAndTpAsync()
    {
        // If we have filled levels but no TP, place TP
        if (_tracker.FilledLevels > 0 && _orders.TrackedTpId == null)
        {
            // We need to figure out the TP price for the last filled grid
            // Use current tracked grid price if available, or recalculate
            if (_orders.TrackedGridPrice > 0)
            {
                await PlaceTpForFilledLevelAsync(_orders.TrackedGridPrice, _orders.TrackedGridLevel);
            }
        }

        // If we have position but no grid, place grid
        if (_orders.TrackedGridId == null && _currentGridLevel > 0 && _currentGridLevel <= _strategy.Params.MaxGridLevels)
        {
            await PlaceNextGridAsync();
        }
    }

    /// <summary>Place TP for a filled grid level.</summary>
    private async Task PlaceTpForFilledLevelAsync(double gridFillPrice, int level)
    {
        if (_orders.TrackedTpId != null)
        {
            // Cancel old TP first (with 200ms wait)
            _orders.MarkTpCancelRequested();
            await _orders.CancelAndWait(_orders.TrackedTpId);
            _orders.TrackedTpId = null;
        }

        int dir = _tracker.Direction;
        double tpPrice = _gridEngine.TpPriceForLevel(gridFillPrice, dir, _strategy.Params.GridSpread);
        if (tpPrice == 0) return;

        string side = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _orders.TrackedTpId = "pending"; // prevent duplicates

        var orderId = await _orders.PlaceOrder(_finamSymbol, side, "ORDER_TYPE_LIMIT", tpPrice, 1, $"{_logPrefix}-TP-{level}");
        if (orderId != null)
        {
            _orders.TrackedTpId = orderId;
            Console.WriteLine($"[{_logPrefix}] TP-{level}: {side} @ {tpPrice:F0} id={orderId}");
        }
        else
        {
            _orders.TrackedTpId = null; // allow retry
        }
    }

    /// <summary>Place next grid level order.</summary>
    private async Task PlaceNextGridAsync()
    {
        if (_orders.TrackedGridId != null) return; // already have one
        if (!_tracker.HasPosition) return;

        var next = _gridEngine.NextLevel(
            _tracker.EntryPrice, _tracker.Direction, _tracker.FilledLevels,
            _strategy.Params.GridStep, _strategy.Params.GridSpread, _strategy.Params.MaxGridLevels);

        if (next == null)
        {
            Console.WriteLine($"[{_logPrefix}] Max grid levels reached ({_strategy.Params.MaxGridLevels})");
            _currentGridLevel = 0;
            return;
        }

        var (gridPrice, tpPrice, level) = next.Value;
        _currentGridLevel = level;

        int dir = _tracker.Direction;
        string side = dir == 1 ? "SIDE_BUY" : "SIDE_SELL"; // LONG: buy more below, SHORT: sell more above

        _orders.TrackedGridId = "pending"; // prevent duplicates
        _orders.TrackedGridPrice = gridPrice;
        _orders.TrackedGridLevel = level;

        var orderId = await _orders.PlaceOrder(_finamSymbol, side, "ORDER_TYPE_LIMIT", gridPrice, 1, $"{_logPrefix}-GRID-{level}");
        if (orderId != null)
        {
            _orders.TrackedGridId = orderId;
            Console.WriteLine($"[{_logPrefix}] Grid-{level}: {side} @ {gridPrice:F0} (TP @ {tpPrice:F0}) id={orderId}");
        }
        else
        {
            _orders.TrackedGridId = null; // allow retry
        }
    }

    // === CANDLE PROCESSING ===

    private async Task ProcessCandlesAsync()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return;

            var bars = await rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M5",
                DateTime.UtcNow.AddMinutes(-30).ToString("o"),
                DateTime.UtcNow.ToString("o"));

            if (bars?.Bars == null) return;

            foreach (var bar in bars.Bars)
            {
                var ts = DateTime.Parse(bar.Timestamp);
                if (ts <= _lastCandleTime) continue;
                if (ts.AddMinutes(5) > DateTime.UtcNow) continue; // incomplete candle

                var candle = new Candle
                {
                    Timestamp = ts,
                    Open = double.Parse(bar.Open.Value),
                    High = double.Parse(bar.High.Value),
                    Low = double.Parse(bar.Low.Value),
                    Close = double.Parse(bar.Close.Value),
                    Volume = (long)double.Parse(bar.Volume?.Value ?? "0")
                };

                _strategy.OnCandle(candle);
                _lastCandleTime = ts;

                // Process signals
                while (_strategy.HasPendingEvents)
                {
                    var evt = _strategy.GetNextEvent();
                    if (evt == null) break;

                    if (evt.Type == GridMmV7Strategy.EventType.EntryMarket)
                    {
                        if (!_tracker.HasPosition)
                            await ExecuteEntryAsync(evt);
                    }
                    else if (evt.Type == GridMmV7Strategy.EventType.CloseAllMarket)
                    {
                        Console.WriteLine($"[{_logPrefix}] Close signal: {evt.Reason}");
                        await _orders.CancelAllByPrefix();
                        _orders.TrackedGridId = null;
                        _orders.TrackedTpId = null;

                        if (_tracker.HasPosition)
                        {
                            int dir = _tracker.Direction;
                            int lots = _tracker.TotalLots;
                            await _orders.PlaceMarketOrder(_finamSymbol, dir == 1 ? "SIDE_SELL" : "SIDE_BUY", lots, $"{_logPrefix}-CLOSE: {evt.Reason}");
                        }

                        _tracker.OnClose();
                        _strategy.ClearPosition();
                        SaveState();
                    }
                }
            }
        }
        catch { }
    }

    // === ENTRY EXECUTION ===

    private async Task ExecuteEntryAsync(GridMmV7Strategy.Event evt)
    {
        if (_tracker.HasPosition) return;

        int dir = evt.Direction;
        await _orders.PlaceMarketOrder(_finamSymbol, dir == 1 ? "SIDE_BUY" : "SIDE_SELL", 1, $"{_logPrefix}-ENTRY: {evt.Reason}");
        await Task.Delay(2000);

        double fillPrice = await GetLastTradePriceAsync(dir == 1 ? "BUY" : "SELL");
        if (fillPrice == 0) fillPrice = (await GetBrokerPositionAsync()).avgPrice;
        if (fillPrice == 0) { Console.WriteLine($"[{_logPrefix}] Entry: no fill price"); return; }

        Console.WriteLine($"[{_logPrefix}] Entry {dir}: fill={fillPrice:F0}");
        _tracker.OnEntry(dir, fillPrice, 1);
        _strategy.OnEntryFilled(dir, fillPrice, 1);
        _lastEntryPrice = fillPrice;
        _lastEntryDir = dir;
        _currentGridLevel = 1;

        await PlaceNextGridAsync();
        SaveState();
    }

    // === BROKER SYNC (periodic) ===

    private async Task BrokerSyncAsync()
    {
        if (!_tracker.HasPosition) return;

        var (bDir, bLots, bAvg) = await GetBrokerPositionAsync();

        // No position at broker but we think we have one → reset
        if (bLots == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: no position at broker → reset");
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();
            SaveState();
            return;
        }

        // Direction mismatch → broker wins
        if (bDir != _tracker.Direction)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: direction mismatch! Broker={bDir} Tracker={_tracker.Direction}");
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;

            double entry = _lastEntryPrice > 0 ? _lastEntryPrice : bAvg;
            _tracker.Restore(bDir, entry, bLots, bLots - 1, 0, 0);
            _strategy.RestorePosition(bDir, entry, bLots, bLots - 1, 0, 0);
            _lastEntryPrice = entry;
            _lastEntryDir = bDir;
            _currentGridLevel = bLots; // next grid level
            SaveState();
            return;
        }

        // Lots mismatch → broker wins
        if (bLots != _tracker.TotalLots)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: lots mismatch broker={bLots} tracker={_tracker.TotalLots}");
            int filledLevels = bLots - _tracker.EntryLots;
            if (filledLevels < 0) filledLevels = 0;

            // entryPrice = sacred, never overwritten
            _tracker.Restore(bDir, _tracker.EntryPrice, bLots, filledLevels, _tracker.RoundTrips, _tracker.RealizedPnL);
            _strategy.RestorePosition(bDir, _strategy.EntryPrice, bLots, filledLevels, _tracker.RoundTrips, _tracker.RealizedPnL);
            _currentGridLevel = filledLevels + 1;

            // Reset tracked orders — they may be stale
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            SaveState();
        }
    }

    // === BROKER QUERIES ===

    private async Task<(int dir, int lots, double avgPrice)> GetBrokerPositionAsync()
    {
        try
        {
            using var http = new HttpClient { BaseAddress = new Uri("http://localhost:5050"), Timeout = TimeSpan.FromSeconds(5) };
            var resp = await http.GetAsync("/api/positions");
            if (!resp.IsSuccessStatusCode) return (0, 0, 0);
            var json = await resp.Content.ReadAsStringAsync();
            var doc = System.Text.Json.JsonDocument.Parse(json);
            foreach (var p in doc.RootElement.EnumerateArray())
            {
                if (p.GetProperty("ticker").GetString() == _ticker)
                {
                    int d = p.GetProperty("dir").GetString() == "Buy" ? 1 : -1;
                    int q = p.GetProperty("qty").GetInt32();
                    double avg = p.GetProperty("avgPrice").GetDouble();
                    return (d, q, avg);
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] GetBrokerPosition error: {ex.Message}");
        }
        return (0, 0, 0);
    }

    private async Task<double> GetLastTradePriceAsync(string side)
    {
        try
        {
            using var http = new HttpClient { BaseAddress = new Uri("http://localhost:5050") };
            var resp = await http.GetAsync($"/api/trades?date={DateTime.UtcNow:yyyy-MM-dd}");
            if (!resp.IsSuccessStatusCode) return 0;
            var json = await resp.Content.ReadAsStringAsync();
            var doc = System.Text.Json.JsonDocument.Parse(json);
            var trades = doc.RootElement.TryGetProperty("trades", out var t) ? t : doc.RootElement;
            double lastPrice = 0;
            foreach (var tr in trades.EnumerateArray())
            {
                var s = tr.GetProperty("side").GetString() ?? "";
                if (s.Contains(side))
                {
                    var p = tr.GetProperty("price");
                    lastPrice = p.TryGetProperty("value", out var pv) ? pv.GetDouble() : p.GetDouble();
                }
            }
            return lastPrice > 0 ? Math.Round(lastPrice) : 0;
        }
        catch { }
        return 0;
    }

    // === STATE PERSISTENCE ===

    private void SaveState()
    {
        try
        {
            double lastE = _tracker.EntryPrice > 0 ? _tracker.EntryPrice : _lastEntryPrice;
            int lastD = _tracker.Direction != 0 ? _tracker.Direction : _lastEntryDir;
            _lastEntryPrice = lastE;
            _lastEntryDir = lastD;
            var state = new
            {
                dir = _tracker.Direction,
                entryPrice = _tracker.EntryPrice,
                entryLots = _tracker.EntryLots,
                filledLevels = _tracker.FilledLevels,
                roundTrips = _tracker.RoundTrips,
                realizedPnL = _tracker.RealizedPnL,
                lastEntryPrice = lastE,
                lastDir = lastD,
                gridOrderId = _orders.TrackedGridId ?? "",
                tpOrderId = _orders.TrackedTpId ?? "",
                currentGridLevel = _currentGridLevel,
                ts = DateTime.UtcNow.ToString("O")
            };
            System.IO.File.WriteAllText(_stateFile, System.Text.Json.JsonSerializer.Serialize(state));
        }
        catch { }
    }

    public void Dispose()
    {
        _mainTimer?.Dispose();
    }
}
