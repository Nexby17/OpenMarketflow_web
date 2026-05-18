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
    private static readonly DataProviderClient _dpClient = new DataProviderClient("http://localhost:5060");
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
    private double _currentPrice = 0;
    private int _skipTicks = 0;
    private string? _pocOrderId;
    private double _pocPrice = 0;
    private int _brokerSyncTick = 0;

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
                    var (bDir, bLots, bAvg, _) = await GetBrokerPositionAsync();
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

                    // Warmup candles — try DP first, fallback to REST
                    bool warmupDone = false;
                    try
                    {
                        var dpCandles = await _dpClient.GetCandlesAsync(_finamSymbol, "TIME_FRAME_M1", 120);
                        if (dpCandles != null && dpCandles.Count > 0)
                        {
                            foreach (var c in dpCandles)
                            {
                                _strategy.OnBar(c.Close, c.Volume);
                                _lastCandleTime = DateTime.Parse(c.Timestamp);
                            }
                            Console.WriteLine($"[{_logPrefix}] Warmup (DP): {dpCandles.Count} candles, VAL={_strategy.VAL:F0} VAH={_strategy.VAH:F0} POC={_strategy.POC:F0}");
                            warmupDone = true;
                        }
                    }
                    catch { }
                    if (!warmupDone)
                    {
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
                            Console.WriteLine($"[{_logPrefix}] Warmup (REST): {bars.Bars.Count} candles, VAL={_strategy.VAL:F0} VAH={_strategy.VAH:F0} POC={_strategy.POC:F0}");
                        }
                    }
                    SaveState();
                }
                catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Warmup error: {ex.Message}"); }
            });
        }

        _mainTimer = new System.Threading.Timer(MainLoopTick, null, TimeSpan.FromMilliseconds(500), TimeSpan.FromMilliseconds(500));
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
        var (bDir, bLots, _, _) = await GetBrokerPositionAsync();
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
        // Get broker PnL
        double brokerPnL = 0;
        try
        {
            var rest = _broker.RestClient;
            if (rest != null)
            {
                var account = rest.GetAccountAsync(_accountId).GetAwaiter().GetResult();
                if (account?.Positions != null)
                {
                    foreach (var p in account.Positions)
                    {
                        var sym = p.Symbol?.Split('@')[0] ?? "";
                        if (sym == _ticker)
                            brokerPnL += p.UnrealizedProfit;
                    }
                }
            }
        }
        catch { }

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
            brokerPnL = Math.Round(brokerPnL, 1),
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

        // === BROKER = SOURCE OF TRUTH ===
        // Чередуем: чётный тик → позиция, нечётный → ордера
        _brokerSyncTick++;
        bool fetchPosition = _brokerSyncTick % 2 == 0;
        bool fetchOrders = _brokerSyncTick % 2 == 1;

        // Всегда запрашиваем и то и другое если нет позиции (для signal detection)
        if (_strategy.PositionDirection == 0) { fetchPosition = true; fetchOrders = true; }

        int brokerDir = 0, brokerLots = 0;
        double brokerAvg = 0;
        List<(string id, double price, string comment, bool isActive)> brokerOrders = new();

        if (fetchPosition)
            (brokerDir, brokerLots, brokerAvg, _currentPrice) = await GetBrokerPositionAsync();
        else
        {
            brokerDir = _strategy.PositionDirection;
            brokerLots = _strategy.TotalLots;
            brokerAvg = _lastEntryPrice;
        }

        if (fetchOrders)
            brokerOrders = await GetBrokerOrdersAsync();

        bool brokerHasPos = brokerLots > 0 && brokerDir != -999;
        bool brokerError = brokerDir == -999;
        bool robotHasPos = _strategy.PositionDirection != 0;

        // API error → skip this tick entirely, don't touch anything
        if (brokerError && robotHasPos) return;

        // 2. NO POSITION AT BROKER → flicker check, then reset
        if (!brokerHasPos)
        {
            if (robotHasPos || _gridOrderId != null || _tpOrderId != null)
            {
                // Flicker protection: recheck after 200ms
                if (fetchPosition)
                {
                    await Task.Delay(200);
                    var (recheckDir, recheckLots, _, _) = await GetBrokerPositionAsync();
                    if (recheckLots > 0 && recheckDir != -999)
                    {
                        Console.WriteLine($"[{_logPrefix}] Broker flicker — position exists ({recheckLots} lots)");
                        return;
                    }
                    if (recheckDir == -999) return; // API still erroring, don't reset
                }
                Console.WriteLine($"[{_logPrefix}] No broker position → cancel all, reset");
                await CancelAllOrdersAsync();
                // Delay before reset — TP/grid fills may still be processing
                await Task.Delay(1000);
                var (finalDir, finalLots, _, _) = await GetBrokerPositionAsync();
                if (finalLots > 0 && finalDir != -999)
                {
                    Console.WriteLine($"[{_logPrefix}] Late position appeared: {finalLots} lots dir={finalDir} — skip reset");
                    return;
                }
                _gridOrderId = null;
                _tpOrderId = null;
                _entryTpOrderId = null;
                _pocOrderId = null;
                _barsSinceReset = 0;
                _strategy.ClearPosition();
                SaveState();
            }
            else if (fetchOrders)
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

        // 3. BROKER HAS POSITION — restore if robot doesn't know
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
            SaveState();
            return;
        }

        // 4. EXIT — ПЕРЕД fill detection (приоритет выхода над grid/TP)
        if (_strategy.PositionDirection != 0 && _currentPrice > 0)
        {
            double poc = _strategy.CurrentPOC;
            double unrealized = _strategy.CalcUnrealizedPnL(_currentPrice);
            double perLot = _strategy.TotalLots > 0 ? unrealized / _strategy.TotalLots : 0;

            // 4a. 1 лот → POC hit при PnL/lot >= 0
            if (_strategy.TotalLots == 1 && poc > 0)
            {
                bool pocHit = (_strategy.PositionDirection == 1 && _currentPrice >= poc) ||
                              (_strategy.PositionDirection == -1 && _currentPrice <= poc);
                if (pocHit && perLot >= 0)
                {
                    Console.WriteLine($"[{_logPrefix}] Exit: POC hit {_strategy.DirStr}: {_currentPrice:F0} " +
                        $"{(_strategy.PositionDirection == 1 ? ">=" : "<=")} {poc:F0}");
                    await CloseAllAsync($"POC hit {_strategy.DirStr}: {_currentPrice:F0} " +
                        $"{(_strategy.PositionDirection == 1 ? ">=" : "<=")} {poc:F0}");
                    return;
                }
            }

            // 4b. 2+ лота → только PnL/lot >= MinProfitPerLot
            if (_strategy.TotalLots >= 2 && perLot >= _strategy.Params.MinProfitPerLot)
            {
                Console.WriteLine($"[{_logPrefix}] Exit: PnL/lot={perLot:F0} >= {_strategy.Params.MinProfitPerLot}");
                await CloseAllAsync($"PnL/lot={perLot:F0} >= {_strategy.Params.MinProfitPerLot}");
                return;
            }
        }

        // 5. DETECT FILLS — по tracked order IDs
        if (fetchOrders && !string.IsNullOrEmpty(_gridOrderId) && _gridOrderId != "pending")
        {
            var gridActive = brokerOrders.Any(o => o.id == _gridOrderId && o.isActive);
            if (!gridActive)
            {
                Console.WriteLine($"[{_logPrefix}] Grid fill: {_gridOrderId} @ {_gridPrice:F0} → broker lots={brokerLots}");
                _strategy.OnGridFill(_gridLevel, _gridPrice);
                // Cancel only tracked orders, not all
                await CancelTrackedOrdersAsync();
                _gridOrderId = null;
                _tpOrderId = null;
                _pocOrderId = null;
                await PlaceGridAsync();
                if (_strategy.FilledLevels > 0) await PlaceTpAsync();
                SaveState();
                return;
            }
        }

        if (fetchOrders && !string.IsNullOrEmpty(_tpOrderId) && _tpOrderId != "pending")
        {
            var tpActive = brokerOrders.Any(o => o.id == _tpOrderId && o.isActive);
            if (!tpActive)
            {
                Console.WriteLine($"[{_logPrefix}] TP fill: {_tpOrderId} @ {_tpPrice:F0} → broker lots={brokerLots}");
                double pnl = _tpPrice > 0 && _gridPrice > 0
                    ? Math.Abs(_tpPrice - _gridPrice) - _strategy.Params.Commission * 2
                    : 0;
                _strategy.OnGridTpDone(pnl);
                // Cancel only tracked orders, not all
                await CancelTrackedOrdersAsync();
                _gridOrderId = null;
                _tpOrderId = null;
                _pocOrderId = null;
                await PlaceGridAsync();
                if (_strategy.FilledLevels > 0) await PlaceTpAsync();
                SaveState();
                return;
            }
        }

        // 5. ENSURE orders — если grid/TP не стоят
        if (_strategy.FilledLevels > 0 && _tpOrderId == null)
            await PlaceTpAsync();
        if (_strategy.PositionDirection != 0 && _gridOrderId == null && _strategy.FilledLevels < _strategy.Params.MaxLevels)
            await PlaceGridAsync();

        // 5b. ENSURE entry TP + POC-TP
        if (_strategy.PositionDirection != 0 && _strategy.TotalLots == 1 && string.IsNullOrEmpty(_entryTpOrderId))
            await EnsurePocTpAsync();

        // 6. CANDLES — feed и timeout exit
        await ProcessCandlesAsync();
    }

    // === CANDLE PROCESSING ===

    private async Task ProcessCandlesAsync()
    {
        try
        {
            // Primary: DataProviderClient candles
            List<(DateTime ts, double open, double high, double low, double close, double vol)> candleData = null;

            try
            {
                var dpCandles = await _dpClient.GetCandlesAsync(_finamSymbol, "TIME_FRAME_M1", 10);
                if (dpCandles != null && dpCandles.Count > 0)
                {
                    candleData = dpCandles.Select(c => (
                        ts: DateTime.Parse(c.Timestamp),
                        open: c.Open,
                        high: c.High,
                        low: c.Low,
                        close: c.Close,
                        vol: c.Volume
                    )).ToList();
                }
            }
            catch { }

            // Fallback: Finam REST
            if (candleData == null)
            {
                var rest = _broker.RestClient;
                if (rest == null) return;
                var bars = await rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M1",
                    DateTime.UtcNow.AddMinutes(-10).ToString("o"),
                    DateTime.UtcNow.ToString("o"));
                if (bars?.Bars == null) return;
                candleData = bars.Bars.Select(b => (
                    ts: DateTime.Parse(b.Timestamp),
                    open: double.Parse(b.Open.Value),
                    high: b.High != null ? double.Parse(b.High.Value) : double.Parse(b.Close.Value),
                    low: b.Low != null ? double.Parse(b.Low.Value) : double.Parse(b.Close.Value),
                    close: double.Parse(b.Close.Value),
                    vol: double.Parse(b.Volume?.Value ?? "0")
                )).ToList();
            }

            foreach (var bar in candleData)
            {
                var ts = bar.ts;
                if (ts <= _lastCandleTime) continue;

                double close = bar.close;
                double high = bar.high;
                double low = bar.low;
                double vol = bar.vol;

                // Check timeout exit BEFORE feeding (POC exit handled in MainLoop by current_price)
                if (_strategy.PositionDirection != 0)
                {
                    // Only check timeout here
                    if (_strategy.HoldMinutes >= _strategy.Params.MaxHoldMinutes)
                    {
                        Console.WriteLine($"[{_logPrefix}] Exit: Timeout ({_strategy.HoldMinutes} min)");
                        await CloseAllAsync($"Timeout ({_strategy.HoldMinutes} min)");
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
                double currentPrice = candleData.Last().close;
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

        // Entry guard: check broker before placing order
        var (guardDir, guardLots, _, _) = await GetBrokerPositionAsync();
        if (guardDir == -999) { Console.WriteLine($"[{_logPrefix}] Entry skipped: API error"); _skipTicks = 10; return; }
        if (guardLots > 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry guard: broker already has {guardLots} lots (dir={guardDir}), syncing");
            double entry = signalPrice;
            _strategy.OnEntry(guardDir, entry);
            _lastEntryPrice = entry;
            _lastEntryDir = guardDir;
            await PlaceGridAsync();
            SaveState();
            return;
        }

        string side = direction == 1 ? "SIDE_BUY" : "SIDE_SELL";
        await PlaceMarketOrderAsync(side, 1, $"VPSG-ENTRY: {(direction == 1 ? "LONG" : "SHORT")}");
        await Task.Delay(1000);

        // Confirm fill via broker position (3 retries)
        double fillPrice = 0;
        int fillDir = 0;
        int fillLots = 0;
        for (int attempt = 0; attempt < 2; attempt++)
        {
            var (bDir, bLots, bAvg, _) = await GetBrokerPositionAsync();
            if (bLots > 0) { fillDir = bDir; fillLots = bLots; fillPrice = bAvg > 0 ? bAvg : signalPrice; break; }
            if (bDir == -999) break;
            await Task.Delay(500);
        }

        if (fillPrice == 0 || fillLots == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry failed: no fill confirmed. Anti-spam 50 ticks.");
            _skipTicks = 50; // ~10 sec cooldown
            return;
        }

        _strategy.OnEntry(fillDir, fillPrice);
        _lastEntryPrice = fillPrice;
        _lastEntryDir = fillDir;
        Console.WriteLine($"[{_logPrefix}] Entry {(fillDir == 1 ? "LONG" : "SHORT")} @ {fillPrice:F0}");
        _skipTicks = 5;
        await PlaceEntryTpAsync(fillPrice);
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
        // TP from last filled grid price, not _gridPrice (which is next grid level)
        double lastFilledPrice = _strategy.LastFilledGridPrice > 0 ? _strategy.LastFilledGridPrice : _gridPrice;
        double tpPrice = dir == 1 ? lastFilledPrice + spread : lastFilledPrice - spread;
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

        // Entry TP на POC, не на entry±spread
        double poc = _strategy.CurrentPOC;
        if (poc <= 0 || double.IsNaN(poc)) return;
        
        // Для LONG: POC должен быть выше entry, для SHORT — ниже
        if (dir == 1 && poc <= entryPrice) return;
        if (dir == -1 && poc >= entryPrice) return;
        
        _entryTpPrice = poc;
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
                Price = new() { Value = ((int)_entryTpPrice).ToString() },
                Comment = "VPSG-ENTRY-TP"
            });
            _entryTpOrderId = result?.OrderId ?? "";
            Console.WriteLine($"[{_logPrefix}] Entry-TP (POC): {side} @ {_entryTpPrice:F0}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Entry-TP error: {ex.Message}");
            _entryTpOrderId = null;
        }
    }

    // === BROKER QUERIES ===

    private async Task<(int dir, int lots, double avgPrice, double currentPrice)> GetBrokerPositionAsync()
    {
        try
        {
            // Primary: DataProvider gRPC GetAccount
            var pos = await _dpClient.GetPositionAsync(_accountId, _ticker);
            if (pos != null)
                return (pos.Dir, pos.Lots, pos.AvgPrice, pos.CurrentPrice);

            // Fallback: Finam REST
            var rest = _broker.RestClient;
            if (rest == null) return (0, 0, 0, 0);
            var account = await rest.GetAccountAsync(_accountId);
            if (account?.Positions == null) return (0, 0, 0, 0);
            foreach (var p in account.Positions)
            {
                var sym = (p.Symbol ?? "").Split('@')[0];
                if (sym != _ticker) continue;
                long qty = p.EffectiveQuantity;
                if (qty == 0) continue;
                double avg = p.AveragePrice ?? 0;
                double cp = p.CurrentPrice ?? 0;
                int d = qty > 0 ? 1 : -1;
                return (d, (int)Math.Abs(qty), avg, cp);
            }
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] GetBrokerPosition error: {ex.Message}"); return (-999, 0, 0, 0); }
        return (0, 0, 0, 0);
    }

    private async Task<List<(string id, double price, string comment, bool isActive)>> GetBrokerOrdersAsync()
    {
        var result = new List<(string, double, string, bool)>();
        try
        {
            // Primary: DataProviderClient
            var dpOrders = await _dpClient.GetOrdersAsync(_accountId);
            if (dpOrders != null && dpOrders.Count > 0)
            {
                foreach (var o in dpOrders)
                {
                    bool isActive = o.Status == "ORDER_STATUS_NEW" || o.Status == "active";
                    result.Add((o.Id ?? "", o.Price, o.Comment ?? "", isActive));
                }
                return result;
            }

            // Fallback: Finam REST
            Console.WriteLine($"[{_logPrefix}] GetBrokerOrders: DP empty, fallback to REST");
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

    private async Task CancelTrackedOrdersAsync()
    {
        // Cancel only tracked order IDs, not all — preserves other active orders
        if (!string.IsNullOrEmpty(_gridOrderId) && _gridOrderId != "pending")
            await CancelOrderAsync(_gridOrderId);
        if (!string.IsNullOrEmpty(_tpOrderId) && _tpOrderId != "pending")
            await CancelOrderAsync(_tpOrderId);
        if (!string.IsNullOrEmpty(_pocOrderId) && _pocOrderId != "pending")
            await CancelOrderAsync(_pocOrderId);
        if (!string.IsNullOrEmpty(_entryTpOrderId) && _entryTpOrderId != "pending")
            await CancelOrderAsync(_entryTpOrderId);
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
        // Guard: never restore with entry=0
        if (brokerAvg <= 0 && (_lastEntryPrice <= 0 || _lastEntryDir != brokerDir))
        {
            Console.WriteLine($"[{_logPrefix}] Restore skipped: avg={brokerAvg:F0}, lastEntry={_lastEntryPrice:F0}, lastDir={_lastEntryDir}, brokerDir={brokerDir}");
            return;
        }
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
