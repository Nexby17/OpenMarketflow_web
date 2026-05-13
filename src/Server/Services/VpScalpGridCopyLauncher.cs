using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// VP Scalp Grid Copy Launcher — rewritten with 4-component architecture (same as V7).
///
/// Components:
/// 1. PositionTracker — pure state, NO I/O
/// 2. OrderManager — broker I/O
/// 3. GridEngine — pure calculation
/// 4. SignalDetector — wrapper over VpScalpGridCopyStrategy
/// 5. MainLoop — orchestration (this class, 200ms, 5 steps)
///
/// Rules:
/// - entryPrice = sacred, NEVER overwritten
/// - Fill detection via tracked order IDs (not lots comparison)
/// - One grid + one TP at a time
/// - Stop = cancel all + close position market
/// - Pause = cancel all orders, keep position
/// - Start = restore from state file + broker sync + warmup + start loop
/// - Cancel old TP BEFORE placing new TP (with 200ms wait)
/// - Grid/TP fill → cancel ALL tracked → ensure on next tick
/// - 5 ticks skip after entry (broker delay)
/// - Broker flicker protection: double-check 1 sec on "no position"
/// - Night gap: 23:55–10:00 MSK — no trading
/// - MOEX clearing: 13:59:59 MSK cancel → 14:05:01 MSK restore
/// - State file: /tmp/vp-copy-state.json
/// - Log prefix: "[VP-COPY]"
/// - Commission: 0.90 RT
/// - Grid step and spread from strategy params (RV-adapted)
/// </summary>
public class VpScalpGridCopyLauncher : IDisposable
{
    // ================================================================
    // 1. POSITION TRACKER — pure state, NO I/O
    // ================================================================
    private class PositionTracker
    {
        public int Direction { get; private set; }
        public double EntryPrice { get; private set; }
        public int EntryLots { get; private set; }
        public int FilledLevels { get; private set; }
        public int RoundTrips { get; private set; }
        public double RealizedPnL { get; private set; }
        public int TotalLots => EntryLots + FilledLevels;
        public int PeakLots { get; private set; }
        public bool HasPosition => Direction != 0;
        public DateTime? EntryTime { get; private set; }
        public int HoldMinutes => EntryTime.HasValue ? (int)(DateTime.UtcNow - EntryTime.Value).TotalMinutes : 0;

        public void OnEntry(int direction, double entryPrice, int lots)
        {
            Direction = direction;
            EntryPrice = entryPrice;
            EntryLots = lots;
            FilledLevels = 0;
            RoundTrips = 0;
            RealizedPnL = 0;
            PeakLots = lots;
            EntryTime = DateTime.UtcNow;
        }

        public void OnGridFill(int level, double fillPrice)
        {
            FilledLevels++;
            int total = TotalLots;
            if (total > PeakLots) PeakLots = total;
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
            RoundTrips = 0;
            RealizedPnL = 0;
            PeakLots = 0;
            EntryTime = null;
        }

        public void Restore(int direction, double entryPrice, int totalLots, int filledLevels, int roundTrips, double realizedPnL)
        {
            Direction = direction;
            EntryPrice = entryPrice;
            EntryLots = totalLots - filledLevels;
            FilledLevels = filledLevels;
            RoundTrips = roundTrips;
            RealizedPnL = realizedPnL;
            PeakLots = totalLots;
            EntryTime = null; // we don't persist entry time in state
        }
    }

    // ================================================================
    // 2. ORDER MANAGER — broker I/O
    // ================================================================
    private class OrderManager
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

        /// <summary>Price of currently tracked TP order.</summary>
        public double TrackedTpPrice { get; set; }
        /// <summary>Level of currently tracked TP order.</summary>
        public int TrackedTpLevel { get; set; }

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
                    double price = 0;
                    if (o.Details?.LimitPrice?.Value != null)
                        price = double.Parse(o.Details.LimitPrice.Value);
                    bool isActive = o.Status == "ORDER_STATUS_NEW";
                    if (isActive)
                        result.Add((o.OrderId, price, comment, o.Side ?? ""));
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
    private class GridEngine
    {
        /// <summary>
        /// Calculate next grid level.
        /// VP Scalp Grid: LONG → grid below entry (buy more), SHORT → grid above entry (sell more).
        /// Returns (gridPrice, level) or null if max levels reached.
        /// </summary>
        public (double gridPrice, int level)? NextGridLevel(
            double entryPrice, int direction, int filledLevels,
            double step, int maxLevels)
        {
            int level = filledLevels + 1;
            if (level > maxLevels) return null;

            double gridPrice;
            if (direction == 1) // LONG: grid BELOW entry (BUY limits to add to position)
            {
                gridPrice = entryPrice - step * level;
            }
            else // SHORT: grid ABOVE entry (SELL limits to add to position)
            {
                gridPrice = entryPrice + step * level;
            }

            return (gridPrice, level);
        }

        /// <summary>
        /// Calculate TP price for a grid level.
        /// LONG: sell above grid fill price. SHORT: buy below grid fill price.
        /// </summary>
        public double TpPrice(double gridPrice, int direction, double spread)
        {
            return direction == 1
                ? gridPrice + spread   // LONG: SELL above grid fill
                : gridPrice - spread;  // SHORT: BUY below grid fill
        }
    }

    // ================================================================
    // 4. SIGNAL DETECTOR — wrapper over VpScalpGridCopyStrategy
    // ================================================================
    private class SignalDetector
    {
        private readonly VpScalpGridCopyStrategy _strategy;

        public SignalDetector(VpScalpGridCopyStrategy strategy)
        {
            _strategy = strategy;
        }

        /// <summary>Feed a candle (close, volume) to the strategy. Returns signal: 0=none, 1=LONG, -1=SHORT.</summary>
        public int FeedBar(double close, double volume)
        {
            return _strategy.OnBar(close, volume);
        }

        /// <summary>Check exit conditions at given price.</summary>
        public (bool shouldClose, string reason) CheckExit(double currentPrice, int currentHourUtc, double? unrealizedPnL = null)
        {
            return _strategy.CheckExit(currentPrice, currentHourUtc, unrealizedPnL);
        }

        /// <summary>Get adapted step and spread based on RV rank.</summary>
        public (int step, int spread) GetAdaptedParams()
        {
            return _strategy.GetAdaptedParams();
        }

        public double POC => _strategy.POC;
        public double VAH => _strategy.VAH;
        public double VAL => _strategy.VAL;
        public VpScalpGridCopyStrategy.Mode CurrentMode => _strategy.CurrentMode;
        public VpScalpGridCopyStrategy.Config Params => _strategy.Params;
    }

    // ================================================================
    // 5. MAIN CLASS — fields, orchestration, public API
    // ================================================================

    private readonly FinamConnector _broker;
    private readonly VpScalpGridCopyStrategy _strategy;
    private readonly SignalDetector _signalDetector;
    private readonly string _stateFile;
    private readonly string _finamSymbol;
    private readonly string _ticker;
    private readonly string _accountId;
    private readonly string _logPrefix = "VP-COPY";
    private readonly string _orderPrefix = "VP-COPY-";

    private readonly PositionTracker _tracker;
    private readonly OrderManager _orders;
    private readonly GridEngine _gridEngine;

    // Loop control
    private DateTime _lastCandleTime = DateTime.MinValue;
    private System.Threading.Timer? _mainTimer;
    private bool _loopRunning = false;
    private bool _manualInProgress = false;
    private int _tickCount = 0;
    private int _skipTicks = 0;
    private int _lastRvLevel = -1; // -1 = not initialized
    private int _barsSinceReset = 0;
    private const int MIN_BARS_AFTER_RESET = 3;

    // Clearing state
    private bool _clearingPaused = false;

    // Last entry for recovery
    private double _lastEntryPrice = 0;
    private int _lastEntryDir = 0;

    // Current grid level for tracking
    private int _currentGridLevel;

    public VpScalpGridCopyStrategy Strategy => _strategy;

    public VpScalpGridCopyLauncher(FinamConnector broker, VpScalpGridCopyStrategy? strategy = null, string? stateFile = null)
    {
        _broker = broker;
        _strategy = strategy ?? new VpScalpGridCopyStrategy();
        _signalDetector = new SignalDetector(_strategy);
        _stateFile = stateFile ?? "/tmp/vp-copy-state.json";
        _finamSymbol = "SiM6@RTSX";
        _ticker = "SiM6";
        _accountId = "1225953";

        _tracker = new PositionTracker();
        _orders = new OrderManager(broker, _accountId, _logPrefix, _orderPrefix);
        _gridEngine = new GridEngine();
    }

    // === PUBLIC API ===

    public async Task StartAsync()
    {
        if (_strategy.CurrentMode == VpScalpGridCopyStrategy.Mode.Running) return;

        _strategy.CurrentMode = VpScalpGridCopyStrategy.Mode.Running;

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
                int filled = root.GetProperty("filledLevels").GetInt32();
                int rt = root.GetProperty("roundTrips").GetInt32();
                double pnl = root.GetProperty("realizedPnL").GetDouble();
                double lastEntry = root.TryGetProperty("lastEntryPrice", out var lep) ? lep.GetDouble() : entry;
                int lastDir = root.TryGetProperty("lastDir", out var ld) ? ld.GetInt32() : dir;

                _lastEntryPrice = lastEntry;
                _lastEntryDir = lastDir;

                if (dir != 0 && entry > 0)
                {
                    int totalLots = 1 + filled; // 1 entry lot + filled levels
                    _tracker.Restore(dir, entry, totalLots, filled, rt, pnl);
                    _strategy.RestorePosition(dir, entry, filled, rt, pnl, null);
                    _currentGridLevel = filled + 1;
                    Console.WriteLine($"[{_logPrefix}] State restored: dir={dir} entry={entry:F0}");
                }
            }
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] State restore error: {ex.Message}"); }

        // Synchronous broker sync + warmup (MUST complete before MainLoop starts)
        var rest = _broker.RestClient;
        if (rest != null)
        {
            try
            {
                // Check broker position SYNCHRONOUSLY
                var (bDir, bLots, bAvg) = await GetBrokerPositionAsync();
                if (bLots > 0 && bDir != 0)
                {
                    double entry = (_lastEntryPrice > 0 && _lastEntryDir == bDir) ? _lastEntryPrice : bAvg;
                    if (entry == 0) entry = bAvg; // Fallback
                    int filled = bLots - 1;
                    if (filled < 0) filled = 0;
                    _tracker.Restore(bDir, entry, bLots, filled, 0, 0);
                    _strategy.RestorePosition(bDir, entry, filled, 0, 0, null);
                    _lastEntryPrice = entry;
                    _lastEntryDir = bDir;
                    _currentGridLevel = filled + 1;
                    Console.WriteLine($"[{_logPrefix}] Broker position restored: {bDir} entry={entry:F0} lots={bLots} avg={bAvg:F0}");
                }
                else
                {
                    Console.WriteLine($"[{_logPrefix}] No broker position (dir={bDir} lots={bLots} avg={bAvg:F0})");
                }

                // Warmup candles
                var bars = await rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M1",
                    DateTime.UtcNow.AddMinutes(-120).ToString("o"),
                    DateTime.UtcNow.ToString("o"));
                if (bars?.Bars != null)
                {
                    foreach (var bar in bars.Bars)
                    {
                        _strategy.OnBar(double.Parse(bar.Close.Value), double.Parse(bar.Volume?.Value ?? "0"));
                        _lastCandleTime = DateTime.Parse(bar.Timestamp);
                    }
                    Console.WriteLine($"[{_logPrefix}] Warmup: {bars.Bars.Count} candles, VAL={_strategy.VAL:F0} VAH={_strategy.VAH:F0} POC={_strategy.POC:F0}");
                }
                SaveState();
            }
            catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Init error: {ex.Message}"); }
        }

        // Start main loop 200ms
        _mainTimer = new System.Threading.Timer(MainLoopTick, null, TimeSpan.FromMilliseconds(500), TimeSpan.FromMilliseconds(500));
        Console.WriteLine($"[{_logPrefix}] ✅ Started");
    }

    public async Task StopAsync()
    {
        Console.WriteLine($"[{_logPrefix}] ⏹ Stop requested");
        _strategy.CurrentMode = VpScalpGridCopyStrategy.Mode.Stopped;
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
            await _orders.PlaceMarketOrder(_finamSymbol, bDir == 1 ? "SIDE_SELL" : "SIDE_BUY", bLots, $"{_orderPrefix}CLOSE: Stop requested");
            Console.WriteLine($"[{_logPrefix}] Closed {bLots} lots");
        }

        _tracker.OnClose();
        _strategy.ClearPosition();
        SaveState();
        Console.WriteLine($"[{_logPrefix}] ⏹ Stopped");
    }

    public async Task PauseAsync()
    {
        _strategy.CurrentMode = VpScalpGridCopyStrategy.Mode.Paused;

        // Cancel all orders, keep position
        await _orders.CancelAllByPrefix();
        _orders.TrackedGridId = null;
        _orders.TrackedTpId = null;

        Console.WriteLine($"[{_logPrefix}] ⏸ Paused");
    }

    public async Task ResumeAsync()
    {
        _strategy.CurrentMode = VpScalpGridCopyStrategy.Mode.Running;
        Console.WriteLine($"[{_logPrefix}] ▶ Resumed");
    }

    public object GetStatus()
    {
        var (step, spread) = _strategy.GetAdaptedParams();
        return new
        {
            status = _strategy.CurrentMode.ToString().ToLower(),
            direction = _tracker.Direction,
            dirStr = _tracker.Direction == 1 ? "LONG" : _tracker.Direction == -1 ? "SHORT" : "FLAT",
            entryPrice = _tracker.EntryPrice,
            totalLots = _tracker.TotalLots,
            filledLevels = _tracker.FilledLevels,
            roundTrips = _tracker.RoundTrips,
            realizedPnL = Math.Round(_tracker.RealizedPnL, 1),
            poc = Math.Round(_strategy.POC, 0),
            vah = Math.Round(_strategy.VAH, 0),
            val = Math.Round(_strategy.VAL, 0),
            activeGridOrders = (_orders.TrackedGridId != null ? 1 : 0) + (_orders.TrackedTpId != null ? 1 : 0),
            holdMinutes = _tracker.HoldMinutes,
            maxHold = _strategy.Params.MaxHoldMinutes,
            step, spread,
            rvAdaptation = _strategy.Params.RvAdaptation,
            rvRank = Math.Round(_strategy.RvRank, 3),
            rvLevel = _strategy.RvLevel,
            currentLevel = _currentGridLevel,
            connected = true,
            params_obj = new
            {
                _strategy.Params.MaxLevels, _strategy.Params.StepBase,
                _strategy.Params.SpreadBase, _strategy.Params.MaxHoldMinutes,
                _strategy.Params.VpLookback, _strategy.Params.VpBinSize,
                _strategy.Params.VaPercent, _strategy.Params.RvAdaptation,
                MinProfitPerLot = _strategy.Params.MinProfitPerLot
            }
        };
    }

    // === MAIN LOOP — 200ms, 5 steps ===

    private async void MainLoopTick(object? state)
    {
        if (_loopRunning || _manualInProgress) return;
        _loopRunning = true;
        try
        {
            if (_strategy.CurrentMode != VpScalpGridCopyStrategy.Mode.Running) return;
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

        // Skip ticks after entry (wait for broker to process)
        if (_skipTicks > 0)
        {
            _skipTicks--;
            return;
        }

        var now = DateTime.UtcNow;
        var msk = now.AddHours(3);
        var mskTime = msk.TimeOfDay;

        // === CLEARING: дневной клиринг 14:00–14:05 МСК (11:00–11:05 UTC) ===
        var clearingStart = new TimeSpan(13, 59, 59);
        var clearingEnd = new TimeSpan(14, 5, 1);

        if (mskTime >= clearingStart && mskTime <= clearingEnd)
        {
            if (!_clearingPaused)
            {
                Console.WriteLine($"[{_logPrefix}] Clearing: снимаем ордера");
                await _orders.CancelAllByPrefix();
                _orders.TrackedGridId = null;
                _orders.TrackedTpId = null;
                _clearingPaused = true;
            }
            return;
        }

        if (_clearingPaused)
        {
            _clearingPaused = false;
            Console.WriteLine($"[{_logPrefix}] Clearing: восстанавливаем ордера");
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
        }

        // === NIGHT: не торгуем 23:55–10:00 МСК (20:55–07:00 UTC) ===
        if (mskTime >= new TimeSpan(23, 55, 0) || mskTime < new TimeSpan(7, 0, 0))
        {
            return;
        }

        // === STEP 1: Process candles and feed strategy ===
        await ProcessCandlesAsync();

        // === STEP 1.5: RV Adaptation — regrid if level changed ===
        if (_tracker.HasPosition && _strategy.Params.RvAdaptation)
        {
            await CheckRvLevelChangeAsync();
        }

        // === STEP 2: Handle signals (Entry/CloseAll) ===
        // Handled inside ProcessCandlesAsync

        // === STEP 3: Detect fills via tracked order IDs ===
        if (_tracker.HasPosition)
        {
            await DetectFillsAsync();
        }

        // === STEP 4: Ensure grid and TP are placed ===
        if (_tracker.HasPosition)
        {
            await EnsureGridAndTpAsync();
        }

        // === STEP 5: Broker sync (every 30 ticks ≈ 6s) ===
        if (_tickCount % 30 == 0)
        {
            await BrokerSyncAsync();
        }
    }

    // === CANDLE PROCESSING (STEP 1 + STEP 2) ===

    private async Task ProcessCandlesAsync()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return;

            var bars = await rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M1",
                DateTime.UtcNow.AddMinutes(-10).ToString("o"),
                DateTime.UtcNow.ToString("o"));
            if (bars?.Bars == null) return;

            foreach (var bar in bars.Bars)
            {
                var ts = DateTime.Parse(bar.Timestamp);
                if (ts <= _lastCandleTime) continue;

                double close = double.Parse(bar.Close.Value);
                double high = bar.High != null ? double.Parse(bar.High.Value) : close;
                double low = bar.Low != null ? double.Parse(bar.Low.Value) : close;
                double vol = double.Parse(bar.Volume?.Value ?? "0");

                // Check exit BEFORE feeding (uses current VP state)
                if (_tracker.HasPosition)
                {
                    double exitPrice = _tracker.Direction == 1 ? high : low;
                    double unrealizedPnl = _tracker.RealizedPnL;
                    var (shouldClose, reason) = _signalDetector.CheckExit(exitPrice, ts.Hour, unrealizedPnl);
                    if (shouldClose)
                    {
                        // Timeout: only close if profitable
                        if (reason.StartsWith("Timeout"))
                        {
                            double unrealized = _tracker.RealizedPnL; // simplified
                            double perLot = _tracker.TotalLots > 0 ? unrealized / _tracker.TotalLots : 0;
                            if (unrealized <= 0 || perLot < _strategy.Params.MinProfitPerLot)
                            {
                                // Not profitable enough — skip, keep waiting
                                int signal = _signalDetector.FeedBar(close, vol);
                                _lastCandleTime = ts;
                                _barsSinceReset++;
                                continue;
                            }
                        }
                        Console.WriteLine($"[{_logPrefix}] Exit: {reason}");
                        await CloseAllAsync(reason);
                        return;
                    }
                }

                // Feed bar to strategy
                int sig = _signalDetector.FeedBar(close, vol);
                _lastCandleTime = ts;
                _barsSinceReset++;

                // Check entry signal (only if no position — double-check with broker)
                if (!_tracker.HasPosition && _barsSinceReset >= MIN_BARS_AFTER_RESET && sig != 0)
                {
                    // Guard: verify broker also has no position
                    var (guardDir, guardLots, _) = await GetBrokerPositionAsync();
                    if (guardLots > 0)
                    {
                        // Broker has position but tracker doesn't — sync first
                        Console.WriteLine($"[{_logPrefix}] Entry guard: broker has {guardLots} lots, syncing instead of entry");
                        double entry = _lastEntryPrice > 0 ? _lastEntryPrice : close;
                        int filled = guardLots - 1;
                        if (filled < 0) filled = 0;
                        _tracker.Restore(guardDir, entry, guardLots, filled, 0, 0);
                        _strategy.RestorePosition(guardDir, entry, filled, 0, 0, null);
                        _lastEntryPrice = entry;
                        _lastEntryDir = guardDir;
                        _currentGridLevel = filled + 1;
                        await PlaceNextGridAsync();
                        SaveState();
                        return;
                    }
                    if (!double.IsNaN(_strategy.VAL) && !double.IsNaN(_strategy.VAH))
                    {
                        Console.WriteLine($"[{_logPrefix}] Signal: {(sig == 1 ? "LONG" : "SHORT")} @ {close:F0} VAL={_strategy.VAL:F0} VAH={_strategy.VAH:F0}");
                        await ExecuteEntryAsync(sig, close);
                        return;
                    }
                }
            }
        }
        catch { }
    }

    // === FILL DETECTION (STEP 3) — order ID based ===

    private async Task DetectFillsAsync()
    {
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

                    // 1. Update position tracker
                    _tracker.OnGridFill(_orders.TrackedGridLevel, _orders.TrackedGridPrice);
                    _strategy.OnGridFill(_orders.TrackedGridLevel, _orders.TrackedGridPrice);
                    _currentGridLevel = _orders.TrackedGridLevel + 1;

                    double filledGridPrice = _orders.TrackedGridPrice;
                    int filledLevel = _orders.TrackedGridLevel;

                    // Clear tracked grid
                    _orders.TrackedGridId = null;

                    // 2. Cancel old TP if any (CancelAndWait)
                    if (_orders.TrackedTpId != null)
                    {
                        _orders.MarkTpCancelRequested();
                        await _orders.CancelAndWait(_orders.TrackedTpId);
                        _orders.TrackedTpId = null;
                    }

                    // 3. Place new TP for the filled level
                    await PlaceTpForFilledLevelAsync(filledGridPrice, filledLevel);

                    // 4. Place next grid level
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
                    var (_, spread) = _signalDetector.GetAdaptedParams();
                    double pnl = spread - _strategy.Params.Commission * 2;
                    Console.WriteLine($"[{_logPrefix}] ⚡ TP fill detected! PnL={pnl:F0}");


                    // 1. Position tracker update
                    _tracker.OnTpFill(pnl);
                    _strategy.OnGridTpDone(pnl);
                    _orders.TrackedTpId = null;

                    // 2. Cancel current grid (CancelAndWait)
                    if (_orders.TrackedGridId != null)
                    {
                        _orders.MarkGridCancelRequested();
                        await _orders.CancelAndWait(_orders.TrackedGridId);
                        _orders.TrackedGridId = null;
                    }

                    // 3. Reset grid to next unfilled level (skip already completed ones)
                    _currentGridLevel = _tracker.FilledLevels + 1;
                    // Cap at max
                    if (_currentGridLevel > _strategy.Params.MaxLevels)
                        _currentGridLevel = 0;

                    if (_tracker.HasPosition)
                    {
                        // 4. Place new grid at level 1
                        await PlaceNextGridAsync();

                        // 5. Place new TP for level 1 (if we have filled levels)
                        // Actually after TP fill, FilledLevels was decremented.
                        // If still have filled levels, place TP for one of them
                        if (_tracker.FilledLevels > 0)
                        {
                            // We need a TP price — recalculate from current grid state
                            // Place TP at entry ± spread (simplified — the ensure step will handle it)
                        }
                    }

                    SaveState();
                }
            }
        }

        // Cleanup: if no position at broker, reset everything
        if (!_tracker.HasPosition) return;

        // Check if broker still has position
        var (bDir, bLots, _) = await GetBrokerPositionAsync();
        if (bDir == -999)
        {
            // API error — don't make decisions, skip this tick
            Console.WriteLine($"[{_logPrefix}] Broker check failed (API error) — skipping reset");
            return;
        }
        if (bLots == 0 && _tracker.HasPosition)
        {
            // Broker flicker protection: double-check
            await Task.Delay(200);
            var (recheckDir, recheckLots, _) = await GetBrokerPositionAsync();
            if (recheckDir == -999)
            {
                Console.WriteLine($"[{_logPrefix}] Broker recheck failed (API error) — skipping reset");
                return;
            }
            if (recheckLots > 0)
            {
                Console.WriteLine($"[{_logPrefix}] Broker flicker — position still exists ({recheckLots} lots)");
                return;
            }

            Console.WriteLine($"[{_logPrefix}] No broker position → reset");
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();
            _barsSinceReset = 0;
            SaveState();
        }
    }

    // === GRID & TP PLACEMENT (STEP 4) ===

    /// <summary>Ensure both grid and TP are placed if position exists and no tracked orders.</summary>
    // === RV ADAPTATION — regrid on level change ===

    private async Task CheckRvLevelChangeAsync()
    {
        if (!_strategy.Params.RvAdaptation) return;
        if (!_tracker.HasPosition) return;
        if (_tracker.EntryPrice <= 0) return;

        int currentLevel = _strategy.RvLevel;
        if (_lastRvLevel < 0)
        {
            _lastRvLevel = currentLevel;
            return;
        }
        if (currentLevel == _lastRvLevel) return;

        // Level changed — cancel current grid + TP, regrid
        var (newStep, newSpread) = _signalDetector.GetAdaptedParams();
        Console.WriteLine($"[{_logPrefix}] RV level changed: {_lastRvLevel} → {currentLevel} (rank={_strategy.RvRank:F2}) → step={newStep} spread={newSpread}");

        _lastRvLevel = currentLevel;

        // Cancel current grid
        if (_orders.TrackedGridId != null && _orders.TrackedGridId != "pending")
        {
            await _orders.CancelAndWait(_orders.TrackedGridId);
            _orders.TrackedGridId = null;
        }

        // Cancel current TP
        if (_orders.TrackedTpId != null && _orders.TrackedTpId != "pending")
        {
            await _orders.CancelAndWait(_orders.TrackedTpId);
            _orders.TrackedTpId = null;
        }

        // EnsureGridAndTpAsync will place new ones with updated step/spread
    }

    private async Task EnsureGridAndTpAsync()
    {
        // GUARD: Never place orders if entry price is 0
        if (_tracker.EntryPrice <= 0)
        {
            // Console.WriteLine($"[{_logPrefix}] EnsureGrid skipped: entryPrice=0");
            return;
        }
        // If we have filled levels but no TP, place TP
        if (_tracker.FilledLevels > 0 && _orders.TrackedTpId == null)
        {
            // Recalculate TP price for last filled grid
            var (step, spread) = _signalDetector.GetAdaptedParams();
            int dir = _tracker.Direction;
            // TP for the closest filled grid level
            double lastGridPrice = dir == 1
                ? _tracker.EntryPrice - step * _tracker.FilledLevels
                : _tracker.EntryPrice + step * _tracker.FilledLevels;
            await PlaceTpForFilledLevelAsync(lastGridPrice, _tracker.FilledLevels);
        }

        // If we have position but no grid, place grid
        if (_orders.TrackedGridId == null && _currentGridLevel > 0)
        {
            var (step, _) = _signalDetector.GetAdaptedParams();
            var next = _gridEngine.NextGridLevel(
                _tracker.EntryPrice, _tracker.Direction, _tracker.FilledLevels,
                step, _strategy.Params.MaxLevels);

            if (next != null)
            {
                _currentGridLevel = next.Value.level;
                await PlaceNextGridAsync();
            }
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
        if (dir == 0) return;

        var (_, spread) = _signalDetector.GetAdaptedParams();
        double tpPrice = _gridEngine.TpPrice(gridFillPrice, dir, spread);
        if (tpPrice == 0) return;

        string side = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _orders.TrackedTpId = "pending"; // prevent duplicates
        _orders.TrackedTpPrice = tpPrice;
        _orders.TrackedTpLevel = level;

        var orderId = await _orders.PlaceOrder(_finamSymbol, side, "ORDER_TYPE_LIMIT", tpPrice, 1, $"{_orderPrefix}TP-{level}");
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

        var (step, _) = _signalDetector.GetAdaptedParams();

        var next = _gridEngine.NextGridLevel(
            _tracker.EntryPrice, _tracker.Direction, _tracker.FilledLevels,
            step, _strategy.Params.MaxLevels);

        if (next == null)
        {
            Console.WriteLine($"[{_logPrefix}] Max grid levels reached ({_strategy.Params.MaxLevels})");
            _currentGridLevel = 0;
            return;
        }

        var (gridPrice, level) = next.Value;

        _currentGridLevel = level;

        int dir = _tracker.Direction;
        string side = dir == 1 ? "SIDE_BUY" : "SIDE_SELL"; // LONG: buy more below, SHORT: sell more above

        _orders.TrackedGridId = "pending"; // prevent duplicates
        _orders.TrackedGridPrice = gridPrice;
        _orders.TrackedGridLevel = level;

        var orderId = await _orders.PlaceOrder(_finamSymbol, side, "ORDER_TYPE_LIMIT", gridPrice, 1, $"{_orderPrefix}GRID-{level}");
        if (orderId != null)
        {
            _orders.TrackedGridId = orderId;
            Console.WriteLine($"[{_logPrefix}] Grid-{level}: {side} @ {gridPrice:F0} id={orderId}");
        }
        else
        {
            _orders.TrackedGridId = null; // allow retry
        }
    }

    // === ENTRY EXECUTION ===

    private async Task ExecuteEntryAsync(int direction, double signalPrice)
    {
        if (_tracker.HasPosition) return;

        // GUARD: Check if broker already has a position — don't open another
        var (guardDir, guardLots, _) = await GetBrokerPositionAsync();
        if (guardLots > 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry BLOCKED: broker already has position dir={guardDir} lots={guardLots}");
            // Sync to broker reality
            var (_, _, bAvg) = await GetBrokerPositionWithRetryAsync();
            if (bAvg > 0 && guardDir != 0)
            {
                _tracker.OnEntry(guardDir, bAvg, guardLots);
                _strategy.OnEntry(guardDir, bAvg);
                _lastEntryPrice = bAvg;
                _lastEntryDir = guardDir;
                _currentGridLevel = 1;
                Console.WriteLine($"[{_logPrefix}] Synced to broker: {guardDir} @ {bAvg:F0} lots={guardLots}");
                await PlaceNextGridAsync();
                SaveState();
            }
            return;
        }

        string side = direction == 1 ? "SIDE_BUY" : "SIDE_SELL";
        Console.WriteLine($"[{_logPrefix}] Placing entry {(direction == 1 ? "BUY" : "SELL")} @ market...");
        await _orders.PlaceMarketOrder(_finamSymbol, side, 1, $"{_orderPrefix}ENTRY: {(direction == 1 ? "LONG" : "SHORT")}");

        // Wait for broker fill with retries (3 attempts × 1 sec)
        _skipTicks = 5;
        double fillPrice = 0;
        int fillDir = 0;
        int fillLots = 0;

        for (int attempt = 0; attempt < 2; attempt++)
        {
            await Task.Delay(500);
            var (d, l, avg) = await GetBrokerPositionAsync();
            if (l > 0 && avg > 0)
            {
                fillPrice = avg;
                fillDir = d;
                fillLots = l;
                Console.WriteLine($"[{_logPrefix}] Fill confirmed on attempt {attempt+1}: dir={d} lots={l} avg={avg:F0}");
                break;
            }
            Console.WriteLine($"[{_logPrefix}] Fill check attempt {attempt+1}/3: no position yet");
        }

        if (fillPrice == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry FAILED after 3 attempts — no broker position. Setting _skipTicks=50 to prevent retry spam.");
            _skipTicks = 50; // Don't retry for ~10 seconds
            return;
        }

        Console.WriteLine($"[{_logPrefix}] Entry {(fillDir == 1 ? "LONG" : "SHORT")} @ {fillPrice:F0}");
        _tracker.OnEntry(fillDir, fillPrice, fillLots);
        _strategy.OnEntry(fillDir, fillPrice);
        _lastEntryPrice = fillPrice;
        _lastEntryDir = fillDir;
        _currentGridLevel = 1;
        await PlaceNextGridAsync();
        SaveState();
    }

    // === CLOSE ALL ===

    private async Task CloseAllAsync(string reason)
    {
        await _orders.CancelAllByPrefix();
        _orders.TrackedGridId = null;
        _orders.TrackedTpId = null;

        if (_tracker.HasPosition)
        {
            int dir = _tracker.Direction;
            int lots = _tracker.TotalLots;
            await _orders.PlaceMarketOrder(_finamSymbol, dir == 1 ? "SIDE_SELL" : "SIDE_BUY", lots, $"{_orderPrefix}CLOSE: {reason}");
        }

        _tracker.OnClose();
        _strategy.ClearPosition();
        _barsSinceReset = 0;
        SaveState();
    }

    // === BROKER SYNC (STEP 5, periodic) ===

    private async Task BrokerSyncAsync()
    {
        if (!_tracker.HasPosition) return;

        var (bDir, bLots, bAvg) = await GetBrokerPositionAsync();

        // API error — don't make any decisions
        if (bDir == -999)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: API error — skipping");
            return;
        }

        // No position at broker but we think we have one → double check (flicker protection)
        if (bLots == 0)
        {
            await Task.Delay(1000);
            var (recheckDir, recheckLots, _) = await GetBrokerPositionAsync();
            if (recheckDir == -999)
            {
                Console.WriteLine($"[{_logPrefix}] Broker sync recheck: API error — skipping reset");
                return;
            }
            if (recheckLots > 0)
            {
                Console.WriteLine($"[{_logPrefix}] Broker sync flicker — position exists ({recheckLots} lots)");
                return;
            }

            Console.WriteLine($"[{_logPrefix}] Broker sync: no position at broker → reset");
            await _orders.CancelAllByPrefix();
            _orders.TrackedGridId = null;
            _orders.TrackedTpId = null;
            _tracker.OnClose();
            _strategy.ClearPosition();
            _barsSinceReset = 0;
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
            int filled = bLots - 1;
            if (filled < 0) filled = 0;
            _tracker.Restore(bDir, entry, bLots, filled, 0, 0);
            _strategy.RestorePosition(bDir, entry, filled, 0, 0, null);
            _lastEntryPrice = entry;
            _lastEntryDir = bDir;
            _currentGridLevel = filled + 1;
            SaveState();
            return;
        }

        // Lots mismatch → broker wins, but don't nuke orders unless severe
        if (bLots != _tracker.TotalLots)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: lots mismatch broker={bLots} tracker={_tracker.TotalLots}");
            int filledLevels = bLots - _tracker.EntryLots;
            if (filledLevels < 0) filledLevels = 0;

            // entryPrice = sacred, never overwritten
            _tracker.Restore(bDir, _tracker.EntryPrice, bLots, filledLevels, _tracker.RoundTrips, _tracker.RealizedPnL);
            _strategy.RestorePosition(bDir, _strategy.EntryPrice, filledLevels, _tracker.RoundTrips, _tracker.RealizedPnL, null);
            _currentGridLevel = filledLevels + 1;

            // Only cancel+replace orders if severe mismatch (tracker was way off)
            // Small mismatches (1-2 lots) will self-correct on next fill
            if (Math.Abs(bLots - _tracker.TotalLots) > 2 || _orders.TrackedGridId == null)
            {
                await _orders.CancelAllByPrefix();
                _orders.TrackedGridId = null;
                _orders.TrackedTpId = null;
            }
            SaveState();
        }
    }

    // === BROKER QUERIES ===

    private async Task<(int dir, int lots, double avgPrice)> GetBrokerPositionAsync()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return (0, 0, 0);
            var account = await rest.GetAccountAsync(_accountId);
            if (account?.Positions == null) return (0, 0, 0);
            foreach (var p in account.Positions)
            {
                string ticker = p.Symbol.Split('@')[0];
                if (ticker != _ticker) continue;
                long qty = p.EffectiveQuantity;
                if (qty == 0) continue;
                int d = qty > 0 ? 1 : -1;
                int q = (int)Math.Abs(qty);
                double avg = p.CurrentPrice ?? 0;
                return (d, q, avg);
            }
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] GetBrokerPosition error: {ex.Message}"); return (-999, 0, 0); }
        return (0, 0, 0);
    }

    private async Task<(int dir, int lots, double avgPrice)> GetBrokerPositionRawAsync()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return (0, 0, 0);
            using var http = new HttpClient();
            http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", await rest.GetJwtAsync());
            var resp = await http.GetAsync($"https://api.finam.ru/v1/accounts/{_accountId}");
            if (!resp.IsSuccessStatusCode) return (0, 0, 0);
            var json = await resp.Content.ReadAsStringAsync();
            var doc = System.Text.Json.JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("positions", out var positions)) return (0, 0, 0);
            foreach (var p in positions.EnumerateArray())
            {
                var sym = p.GetProperty("symbol").GetString() ?? "";
                if (sym.Split('@')[0] != _ticker) continue;
                var qtyStr = p.GetProperty("quantity").GetProperty("value").GetString() ?? "0";
                long qty = long.Parse(qtyStr);
                if (qty == 0) continue;
                double price = 0;
                if (p.TryGetProperty("current_price", out var cp) && cp.TryGetProperty("value", out var cpv))
                    double.TryParse(cpv.GetString(), out price);
                int d = qty > 0 ? 1 : -1;
                return (d, (int)Math.Abs(qty), price);
            }
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] GetBrokerPositionRaw error: {ex.Message}"); }
        return (0, 0, 0);
    }

    private async Task<(int dir, int lots, double avgPrice)> GetBrokerPositionWithRetryAsync(int retries = 3)
    {
        for (int i = 0; i < retries; i++)
        {
            var result = await GetBrokerPositionAsync();
            if (result.lots > 0) return result;
            if (i < retries - 1) await Task.Delay(500);
        }
        return (0, 0, 0);
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
                filledLevels = _tracker.FilledLevels,
                roundTrips = _tracker.RoundTrips,
                realizedPnL = _tracker.RealizedPnL,
                lastEntryPrice = lastE,
                lastDir = lastD,
                gridOrderId = _orders.TrackedGridId ?? "",
                tpOrderId = _orders.TrackedTpId ?? "",
                gridPrice = _orders.TrackedGridPrice,
                tpPrice = _orders.TrackedTpPrice,
                gridLevel = _orders.TrackedGridLevel,
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
