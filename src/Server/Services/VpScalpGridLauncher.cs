using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// VP Scalp Grid Launcher — копия V7 Launcher с заменой входа/выхода.
/// 
/// Правила (те же что V7):
/// - Один MainLoop каждые 200мс
/// - entryPrice = sacred, никогда не перезаписывается
/// - Grid: 1 ордер за раз (НЕ пачками)
/// - Брокер = source of truth для позиции
/// - state file сохраняет lastEntryPrice
/// 
/// Отличия от V7:
/// - Вход: VP signal (price < VAL → LONG, price > VAH → SHORT)
/// - Выход: POC hit / timeout(min_profit)
/// - Таймфрейм: 1 мин (VP нужен частый фид)
/// - Strategy: VpScalpGridStrategy (не V7)
/// </summary>
public class VpScalpGridLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly VpScalpGridStrategy _strategy;
    private readonly string _stateFile;
    private readonly string _finamSymbol;
    private readonly string _ticker;
    private readonly string _accountId;
    private readonly string _logPrefix = "VPSG";

    // Tracked order IDs — устанавливаются ТОЛЬКО при place
    private string? _gridOrderId;
    private double _gridPrice;
    private int _gridLevel;
    private double _tpPrice;
    private string? _tpOrderId;
    private string? _entryTpOrderId;
    private double _entryTpPrice;

    private DateTime _lastCandleTime = DateTime.MinValue;
    private System.Threading.Timer? _mainTimer;
    private bool _loopRunning = false;
    private bool _manualInProgress = false;
    private int _barsSinceReset = 0;
    private const int MIN_BARS_AFTER_RESET = 3;
    private double _lastCandlePrice = 0;
    private int _skipTicks = 0;
    private string? _pocOrderId;
    private double _pocPrice = 0;

    // Last entry для recovery
    private double _lastEntryPrice = 0;
    private int _lastEntryDir = 0;

    public VpScalpGridStrategy Strategy => _strategy;

    public VpScalpGridLauncher(FinamConnector broker, VpScalpGridStrategy strategy, string? stateFile = null)
    {
        _broker = broker;
        _strategy = strategy;
        _stateFile = stateFile ?? "/tmp/vp-scalp-grid-state.json";
        _finamSymbol = "SiM6@RTSX";
        _ticker = "SiM6";
        _accountId = "1225953";
    }

    // === PUBLIC API ===

    public async Task StartAsync()
    {
        if (_strategy.CurrentMode == VpScalpGridStrategy.Mode.Running) return;
        _strategy.CurrentMode = VpScalpGridStrategy.Mode.Running;

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
                    _strategy.RestorePosition(dir, entry, filled, rt, pnl, null);
                Console.WriteLine($"[{_logPrefix}] State restored: dir={dir} entry={entry:F0}");
            }
        }
        catch { }

        // Warmup + restore from broker + clean orphan orders
        var rest = _broker.RestClient;
        if (rest != null)
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    // Check broker position
                    var (bDir, bLots, bAvg) = await GetBrokerPositionAsync();
                    if (bLots > 0 && bDir != 0)
                    {
                        // Restore position from broker
                        double entry = (_lastEntryPrice > 0 && _lastEntryDir == bDir) ? _lastEntryPrice : bAvg;
                        int filled = bLots - 1;
                        if (filled < 0) filled = 0;
                        _strategy.RestorePosition(bDir, entry, filled, 0, 0, null);
                        _lastEntryPrice = entry;
                        _lastEntryDir = bDir;
                        Console.WriteLine($"[{_logPrefix}] Broker position restored: {bDir} entry={entry:F0} lots={bLots}");
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
                catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Warmup error: {ex.Message}"); }
            });
        }

        _mainTimer = new System.Threading.Timer(MainLoopTick, null, TimeSpan.FromMilliseconds(200), TimeSpan.FromMilliseconds(200));
        Console.WriteLine($"[{_logPrefix}] ✅ Started");
    }

    public async Task StopAsync()
    {
        Console.WriteLine($"[{_logPrefix}] ⏹ Stop");
        _strategy.CurrentMode = VpScalpGridStrategy.Mode.Stopped;
        _mainTimer?.Dispose();
        _mainTimer = null;
        await CancelAllOrdersAsync();
        // Также снимаем ВСЕ ордера (не только VPSG)
        try
        {
            var allOrders = await GetBrokerOrdersAsync();
            foreach (var o in allOrders)
            {
                if (o.isActive) await CancelOrderAsync(o.id);
            }
        } catch { }
        var (bDir, bLots, _) = await GetBrokerPositionAsync();
        if (bLots > 0 && bDir != 0)
        {
            await PlaceMarketOrderAsync(bDir == 1 ? "SIDE_SELL" : "SIDE_BUY", bLots, "VPSG-CLOSE: Stop");
            Console.WriteLine($"[{_logPrefix}] Closed {bLots} lots");
        }
        _gridOrderId = null;
        _tpOrderId = null;
        _entryTpOrderId = null;
        _pocOrderId = null;
        _strategy.ClearPosition();
        SaveState();
    }

    public async Task PauseAsync()
    {
        _strategy.CurrentMode = VpScalpGridStrategy.Mode.Paused;
        await CancelAllOrdersAsync();
        _gridOrderId = null;
        _tpOrderId = null;
        _entryTpOrderId = null;
        _pocOrderId = null;
        Console.WriteLine($"[{_logPrefix}] ⏸ Paused (orders cancelled, position kept)");
    }

    public async Task ResumeAsync()
    {
        _strategy.CurrentMode = VpScalpGridStrategy.Mode.Running;
        Console.WriteLine($"[{_logPrefix}] ▶ Resumed");
    }

    public object GetStatus()
    {
        var (step, spread) = _strategy.GetAdaptedParams();
        return new
        {
            status = _strategy.CurrentMode.ToString().ToLower(),
            direction = _strategy.PositionDirection,
            dirStr = _strategy.PositionDirection == 1 ? "LONG" : _strategy.PositionDirection == -1 ? "SHORT" : "FLAT",
            entryPrice = _strategy.EntryPrice,
            totalLots = _strategy.TotalLots,
            filledLevels = _strategy.FilledLevels,
            roundTrips = _strategy.RoundTrips,
            realizedPnL = Math.Round(_strategy.RealizedPnL, 1),
            poc = Math.Round(_strategy.POC, 0),
            vah = Math.Round(_strategy.VAH, 0),
            val = Math.Round(_strategy.VAL, 0),
            activeGridOrders = (_gridOrderId != null ? 1 : 0) + (_tpOrderId != null ? 1 : 0),
            holdMinutes = _strategy.HoldMinutes,
            maxHold = _strategy.Params.MaxHoldMinutes,
            step, spread,
            rvAdaptation = _strategy.Params.RvAdaptation,
            currentLevel = _gridLevel,
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

    // === MAIN LOOP — точная копия логики V7 ===

    private async void MainLoopTick(object? state)
    {
        if (_loopRunning || _manualInProgress) return;
        _loopRunning = true;
        try
        {
            if (_strategy.CurrentMode != VpScalpGridStrategy.Mode.Running) return;
            await MainLoopAsync();
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Loop error: {ex.Message}"); }
        finally { _loopRunning = false; }
    }

    private bool _clearingPaused = false;
    private bool _morningRestored = false;
    private string _lastClearingDate = "";

    private async Task MainLoopAsync()
    {
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
        var clearingStart = new TimeSpan(13, 59, 59); // 1 сек до
        var clearingEnd = new TimeSpan(14, 5, 1);     // 1 сек после

        if (mskTime >= clearingStart && mskTime <= clearingEnd)
        {
            if (!_clearingPaused)
            {
                Console.WriteLine($"[{_logPrefix}] Clearing: снимаем ордера");
                await CancelAllOrdersAsync();
                _gridOrderId = null;
                _tpOrderId = null;
                _clearingPaused = true;
            }
            return; // ждём окончания клиринга
        }

        if (_clearingPaused)
        {
            _clearingPaused = false;
            Console.WriteLine($"[{_logPrefix}] Clearing: восстанавливаем ордера");
            // Reset tracked orders — MainLoop ensure поставит заново
            _gridOrderId = null;
            _tpOrderId = null;
            _pocOrderId = null;
        }

        // === MORNING RESTORE: 10:00:01 МСК (07:00:01 UTC) ===
        if (mskTime >= new TimeSpan(10, 0, 1) && mskTime < new TimeSpan(10, 1, 0))
        {
            if (_morningRestored && _lastClearingDate != msk.ToString("yyyy-MM-dd"))
            {
                _morningRestored = false;
                _lastClearingDate = msk.ToString("yyyy-MM-dd");
            }
            if (!_morningRestored && _strategy.PositionDirection != 0)
            {
                Console.WriteLine($"[{_logPrefix}] Morning restore: позиция есть, ждём grid+TP ensure");
                _morningRestored = true;
            }
        }
        else
        {
            _morningRestored = false;
        }

        // === NIGHT: не торгуем 23:55–10:00 МСК ===
        if (mskTime >= new TimeSpan(23, 55, 0) || mskTime < new TimeSpan(7, 0, 0))
        {
            return;
        }

        // 1. Читаем позицию и ордера брокера (один раз за цикл)
        var (brokerDir, brokerLots, brokerAvg) = await GetBrokerPositionAsync();
        var brokerOrders = await GetBrokerOrdersAsync();

        bool brokerHasPos = brokerLots > 0;
        bool robotHasPos = _strategy.PositionDirection != 0;

        // 2. НЕТ ПОЗИЦИИ У БРОКЕРА → перепроверить (защита от мерцания)
        if (!brokerHasPos && robotHasPos)
        {
            await Task.Delay(1000);
            var (recheckDir, recheckLots, _) = await GetBrokerPositionAsync();
            if (recheckLots > 0)
            {
                Console.WriteLine($"[{_logPrefix}] Broker flicker — position still exists ({recheckLots} lots)");
                brokerHasPos = true;
                brokerDir = recheckDir;
                brokerLots = recheckLots;
            }
        }

        if (!brokerHasPos)
        {
            if (robotHasPos || _gridOrderId != null || _tpOrderId != null)
            {
                Console.WriteLine($"[{_logPrefix}] No broker position → cancel all, reset");
                await CancelAllOrdersAsync();
                _gridOrderId = null;
                _tpOrderId = null;
                _entryTpOrderId = null;
                _pocOrderId = null;
                _barsSinceReset = 0;
                _strategy.ClearPosition();
                SaveState();
            }
            else
            {
                // Orphan cleanup
                foreach (var o in brokerOrders)
                {
                    if (o.comment.StartsWith("VPSG-"))
                    {
                        try { await CancelOrderAsync(o.id); } catch { }
                    }
                }
            }

            // Feed candles + check signal
            await ProcessCandlesAsync();
            return;
        }

        // 3. БРОКЕР ИМЕЕТ ПОЗИЦИЮ — restore если нужно
        if (!robotHasPos)
        {
            await RestoreFromBrokerAsync(brokerDir, brokerLots, brokerAvg);
            return;
        }

        // Direction mismatch → broker wins
        if (brokerDir != _strategy.PositionDirection)
        {
            Console.WriteLine($"[{_logPrefix}] Dir mismatch! Broker={brokerDir} Robot={_strategy.PositionDirection}");
            await CancelAllOrdersAsync();
            _gridOrderId = null;
            _tpOrderId = null;
            double entry = _lastEntryPrice > 0 ? _lastEntryPrice : brokerAvg;
            _strategy.RestorePosition(brokerDir, entry, brokerLots - 1, 0, 0, null);
            _lastEntryPrice = entry;
            _lastEntryDir = brokerDir;
            // НЕ ставим grid — MainLoop сделает на следующем тике через ensure
            SaveState();
            return;
        }

        // 4. DETECT FILLS — broker lots vs strategy lots
        int expectedLots = _strategy.TotalLots;

        // Grid fill: broker has more lots
        if (brokerLots > expectedLots)
        {
            Console.WriteLine($"[{_logPrefix}] Grid fill: broker={brokerLots} robot={expectedLots}");
            _strategy.OnGridFill(_gridLevel, _gridPrice);
            // Cancel ALL, then re-place
            await CancelAllOrdersAsync();
            _gridOrderId = null;
            _tpOrderId = null;
            _pocOrderId = null;
            SaveState();
            return; // MainLoop ensure поставит grid + TP на следующем тике
        }

        // TP fill: broker has fewer lots
        if (brokerLots < expectedLots)
        {
            Console.WriteLine($"[{_logPrefix}] TP fill: broker={brokerLots} robot={expectedLots}");
            double pnl = _tpPrice > 0 && _gridPrice > 0
                ? Math.Abs(_tpPrice - _gridPrice) - _strategy.Params.Commission * 2
                : 0;
            _strategy.OnGridTpDone(pnl);
            // Cancel ALL, then re-place
            await CancelAllOrdersAsync();
            _gridOrderId = null;
            _tpOrderId = null;
            _pocOrderId = null;
            SaveState();
            return; // MainLoop ensure поставит grid + TP на следующем тике
        }

        // 5. SYNC tracked orders with broker
        var gridOrders = brokerOrders.Where(o => o.comment.StartsWith("VPSG-GRID") && o.isActive).ToList();
        var tpOrders = brokerOrders.Where(o => o.comment.StartsWith("VPSG-TP") && o.isActive).ToList();
        var entryTpOrders = brokerOrders.Where(o => o.comment == "VPSG-ENTRY-TP" && o.isActive).ToList();
        var pocOrders = brokerOrders.Where(o => o.comment == "VPSG-POC-TP" && o.isActive).ToList();

        // POC-TP DISABLED — cancel any leftover POC-TP orders
        if (pocOrders.Count > 0)
        {
            foreach (var o in pocOrders)
            {
                try { await CancelOrderAsync(o.id); } catch { }
            }
            _pocOrderId = null;
        }

        // Entry TP fill detection: order disappeared from active list
        if (!string.IsNullOrEmpty(_entryTpOrderId) && _entryTpOrderId != "pending" && entryTpOrders.Count == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry-TP fill @ {_entryTpPrice:F0}");
            _entryTpOrderId = null;
            // Entry lot closed — if no grid filled, whole position done
            if (_strategy.FilledLevels == 0)
            {
                // Only entry lot was open, now closed
                await CancelAllOrdersAsync();
                _gridOrderId = null;
                _tpOrderId = null;
                _strategy.ClearPosition();
                SaveState();
                return;
            }
        }

        if (_gridOrderId == null && gridOrders.Count > 0)
        {
            _gridOrderId = gridOrders[0].id;
            _gridPrice = gridOrders[0].price;
            if (int.TryParse(gridOrders[0].comment.Replace("VPSG-GRID-", ""), out int lvl))
                _gridLevel = lvl;
        }
        if (_tpOrderId == null && tpOrders.Count > 0)
        {
            _tpOrderId = tpOrders[0].id;
            _tpPrice = tpOrders[0].price;
        }

        // 6. ENSURE TP — grid НЕ переставляем
        if (_strategy.FilledLevels > 0 && _tpOrderId == null)
            await PlaceTpAsync();

        // 6a. ENSURE GRID — если позиция есть но grid не стоит
        if (_strategy.PositionDirection != 0 && _gridOrderId == null && _strategy.FilledLevels < _strategy.Params.MaxLevels)
            await PlaceGridAsync();

        // 6b. DISABLED: Entry-TP removed

        // 6c. DISABLED: POC-TP removed — exit by candle high/low check in ProcessCandlesAsync

        // 7. CANDLES — feed и проверяем exit
        await ProcessCandlesAsync();
    }

    // === CANDLE PROCESSING ===

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
                // Use high for LONG (price touched above), low for SHORT (price touched below)
                if (_strategy.PositionDirection != 0)
                {
                    double exitPrice = _strategy.PositionDirection == 1 ? high : low;
                    var (shouldClose, reason) = _strategy.CheckExit(exitPrice, ts.Hour);
                    if (shouldClose)
                    {
                        // Timeout: only close if profitable
                        if (reason.StartsWith("Timeout"))
                        {
                            double unrealized = _strategy.CalcUnrealizedPnL(close);
                            double perLot = _strategy.TotalLots > 0 ? unrealized / _strategy.TotalLots : 0;
                            if (unrealized <= 0 || perLot < _strategy.Params.MinProfitPerLot)
                            {
                                // Not profitable enough — skip, keep waiting
                                _strategy.OnBar(close, vol);
                                _lastCandleTime = ts;
                                continue;
                            }
                        }
                        Console.WriteLine($"[{_logPrefix}] Exit: {reason}");
                        await CloseAllAsync(reason);
                        return;
                    }
                }

                // Feed bar
                _strategy.OnBar(close, vol);
                _lastCandleTime = ts;
                _lastCandlePrice = close;
                _barsSinceReset++;
            }

            // Check entry signal (only if no position)
            if (_strategy.PositionDirection == 0 && _barsSinceReset >= MIN_BARS_AFTER_RESET)
            {
                double currentPrice = double.Parse(bars.Bars.Last().Close.Value);
                if (currentPrice > 0 && !double.IsNaN(_strategy.VAL) && !double.IsNaN(_strategy.VAH))
                {
                    if (currentPrice < _strategy.VAL)
                    {
                        Console.WriteLine($"[{_logPrefix}] Signal: LONG @ {currentPrice:F0} VAL={_strategy.VAL:F0}");
                        await ExecuteEntryAsync(1, currentPrice);
                    }
                    else if (currentPrice > _strategy.VAH)
                    {
                        Console.WriteLine($"[{_logPrefix}] Signal: SHORT @ {currentPrice:F0} VAH={_strategy.VAH:F0}");
                        await ExecuteEntryAsync(-1, currentPrice);
                    }
                }
            }
        }
        catch { }
    }

    // === ORDER EXECUTION ===

    private async Task ExecuteEntryAsync(int direction, double signalPrice)
    {
        if (_strategy.PositionDirection != 0) return;

        string side = direction == 1 ? "SIDE_BUY" : "SIDE_SELL";
        await PlaceMarketOrderAsync(side, 1, $"VPSG-ENTRY: {(direction == 1 ? "LONG" : "SHORT")}");
        await Task.Delay(3000);

        double fillPrice = await GetLastTradePriceAsync(direction == 1 ? "BUY" : "SELL");
        if (fillPrice == 0)
        {
            var (_, _, avg) = await GetBrokerPositionAsync();
            fillPrice = avg;
        }
        if (fillPrice == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry failed: no fill price");
            return;
        }

        _strategy.OnEntry(direction, fillPrice);
        _lastEntryPrice = fillPrice;
        _lastEntryDir = direction;
        Console.WriteLine($"[{_logPrefix}] Entry {(direction == 1 ? "LONG" : "SHORT")} @ {fillPrice:F0}");
        _skipTicks = 5; // Wait 5 ticks (1 sec) for broker to process
        await PlaceGridAsync();
        SaveState();
    }

    private async Task CloseAllAsync(string reason)
    {
        await CancelAllOrdersAsync();
        _gridOrderId = null;
        _tpOrderId = null;
        _entryTpOrderId = null;
        _pocOrderId = null;

        if (_strategy.PositionDirection != 0)
        {
            int dir = _strategy.PositionDirection;
            int lots = _strategy.TotalLots;
            await PlaceMarketOrderAsync(dir == 1 ? "SIDE_SELL" : "SIDE_BUY", lots, $"VPSG-CLOSE: {reason}");
        }
        _strategy.ClearPosition();
        SaveState();
    }

    /// <summary>
    /// Ensure POC-TP order. Repositions if POC changed. Separate from grid/TP.
    /// </summary>
    private async Task EnsurePocTpAsync()
    {
        double poc = _strategy.POC;
        int dir = _strategy.PositionDirection;
        int lots = _strategy.TotalLots;
        if (lots <= 0 || dir == 0) return;

        // If POC changed, cancel old and re-place
        if (!string.IsNullOrEmpty(_pocOrderId) && Math.Abs(poc - _pocPrice) > 1)
        {
            try { await CancelOrderAsync(_pocOrderId); } catch { }
            _pocOrderId = null;
            await Task.Delay(100);
        }

        if (!string.IsNullOrEmpty(_pocOrderId)) return; // already placed, POC same

        string side = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _pocPrice = poc;
        try
        {
            var rest = _broker.RestClient;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = lots.ToString() },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)poc).ToString() },
                Comment = "VPSG-POC-TP"
            });
            _pocOrderId = result?.OrderId ?? "";
            Console.WriteLine($"[{_logPrefix}] POC-TP: {side} {lots} @ {poc:F0} id={_pocOrderId}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] POC-TP error: {ex.Message}");
            _pocOrderId = null;
        }
    }

    /// Place ONE grid order at next level. Guard: _gridOrderId must be null.
    /// </summary>
    private async Task PlaceGridAsync()
    {
        int nextLevel = _strategy.FilledLevels + 1;
        if (nextLevel > _strategy.Params.MaxLevels) return;
        if (!string.IsNullOrEmpty(_gridOrderId)) return; // уже стоит

        int dir = _strategy.PositionDirection;
        if (dir == 0) return;

        var (step, _) = _strategy.GetAdaptedParams();
        double entry = _strategy.EntryPrice;
        double gridPrice = dir == 1 ? entry - step * nextLevel : entry + step * nextLevel;

        // Mark pending IMMEDIATELY
        _gridOrderId = "pending";
        _gridPrice = gridPrice;
        _gridLevel = nextLevel;

        string side = dir == 1 ? "SIDE_BUY" : "SIDE_SELL";
        try
        {
            var rest = _broker.RestClient;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)gridPrice).ToString() },
                Comment = $"VPSG-GRID-{nextLevel}"
            });
            _gridOrderId = result?.OrderId ?? "";
            Console.WriteLine($"[{_logPrefix}] Grid-{nextLevel}: {side} @ {gridPrice:F0} id={_gridOrderId}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Grid-{nextLevel} error: {ex.Message}");
            _gridOrderId = null;
        }
    }

    /// <summary>
    /// Place ONE TP order for last filled grid level. Guard: _tpOrderId must be null.
    /// </summary>
    private async Task PlaceTpAsync()
    {
        if (!string.IsNullOrEmpty(_tpOrderId)) return;
        if (_gridPrice == 0) return;

        int dir = _strategy.PositionDirection;
        if (dir == 0) return;

        var (_, spread) = _strategy.GetAdaptedParams();
        double tpPrice = dir == 1 ? _gridPrice + spread : _gridPrice - spread;
        _tpPrice = tpPrice;

        // Mark pending IMMEDIATELY
        _tpOrderId = "pending";

        string side = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        try
        {
            var rest = _broker.RestClient;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)tpPrice).ToString() },
                Comment = $"VPSG-TP-{_strategy.FilledLevels}"
            });
            _tpOrderId = result?.OrderId ?? "";
            Console.WriteLine($"[{_logPrefix}] TP-{_strategy.FilledLevels}: {side} @ {tpPrice:F0}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] TP error: {ex.Message}");
            _tpOrderId = null;
        }
    }

    /// <summary>
    /// Place entry TP: 1 лот по entry ± spread. Guard: _entryTpOrderId must be null.
    /// </summary>
    private async Task PlaceEntryTpAsync(double entryPrice)
    {
        if (!string.IsNullOrEmpty(_entryTpOrderId)) return;

        int dir = _strategy.PositionDirection;
        if (dir == 0) return;

        var (_, spread) = _strategy.GetAdaptedParams();
        double tpPrice = dir == 1 ? entryPrice + spread : entryPrice - spread;
        _entryTpPrice = tpPrice;
        _entryTpOrderId = "pending";

        string side = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        try
        {
            var rest = _broker.RestClient;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)tpPrice).ToString() },
                Comment = "VPSG-ENTRY-TP"
            });
            _entryTpOrderId = result?.OrderId ?? "";
            Console.WriteLine($"[{_logPrefix}] Entry-TP: {side} @ {tpPrice:F0}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Entry-TP error: {ex.Message}");
            _entryTpOrderId = null;
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
        catch { }
        return (0, 0, 0);
    }

    private async Task<List<(string id, double price, string comment, bool isActive)>> GetBrokerOrdersAsync()
    {
        var result = new List<(string, double, string, bool)>();
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
                result.Add((o.OrderId, price, comment, isActive));
            }
        }
        catch { }
        return result;
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

    // === ORDER HELPERS ===

    private async Task PlaceMarketOrderAsync(string side, int lots, string comment)
    {
        try
        {
            var rest = _broker.RestClient;
            await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = lots.ToString() },
                Side = side,
                OrderType = "ORDER_TYPE_MARKET",
                Comment = comment
            });
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Market order error: {ex.Message}"); }
    }

    private async Task CancelOrderAsync(string orderId)
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest != null) await rest.CancelOrderAsync(_accountId, orderId);
        }
        catch { }
    }

    private async Task CancelAllOrdersAsync()
    {
        try
        {
            var orders = await GetBrokerOrdersAsync();
            foreach (var o in orders)
            {
                if (o.isActive && o.comment.StartsWith("VPSG-"))
                    await CancelOrderAsync(o.id);
            }
        }
        catch { }
    }

    // === STATE PERSISTENCE ===

    private void SaveState()
    {
        try
        {
            var state = new
            {
                dir = _strategy.PositionDirection,
                entryPrice = _strategy.EntryPrice,
                filledLevels = _strategy.FilledLevels,
                roundTrips = _strategy.RoundTrips,
                realizedPnL = _strategy.RealizedPnL,
                lastEntryPrice = _lastEntryPrice,
                lastDir = _lastEntryDir,
                gridOrderId = _gridOrderId ?? "",
                tpOrderId = _tpOrderId ?? "",
                entryTpOrderId = _entryTpOrderId ?? "",
                gridPrice = _gridPrice,
                tpPrice = _tpPrice,
                entryTpPrice = _entryTpPrice,
                gridLevel = _gridLevel,
                ts = DateTime.UtcNow.ToString("O")
            };
            System.IO.File.WriteAllText(_stateFile, System.Text.Json.JsonSerializer.Serialize(state));
        }
        catch { }
    }

    private async Task RestoreFromBrokerAsync(int brokerDir, int brokerLots, double brokerAvg)
    {
        double entry = (_lastEntryPrice > 0 && _lastEntryDir == brokerDir) ? _lastEntryPrice : brokerAvg;
        int filled = brokerLots - 1;
        if (filled < 0) filled = 0;
        _strategy.RestorePosition(brokerDir, entry, filled, 0, 0, null);
        _lastEntryPrice = entry;
        _lastEntryDir = brokerDir;
        _gridOrderId = null;
        _tpOrderId = null;
        Console.WriteLine($"[{_logPrefix}] Restored from broker: {brokerDir} entry={entry:F0} lots={brokerLots}");
        // НЕ ставим grid/TP — MainLoop сделает на следующем тике
        SaveState();
    }

    public void Dispose() { _mainTimer?.Dispose(); }
}
