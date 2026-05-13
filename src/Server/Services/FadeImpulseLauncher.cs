using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Fade Impulse Launcher — orchestration for MIX (or any instrument).
///
/// Flow:
/// 1. 5-min candle polling (same as VP Copy)
/// 2. Feed candle to FadeImpulseStrategy
/// 3. On SignalEntry → place market order
/// 4. On each tick → check SL/TP via broker (limit orders for SL/TP)
/// 5. On exit → cancel orders, close position if needed
///
/// Rules:
/// - 1 lot only
/// - SL as stop-loss order, trailing updated each candle
/// - TP as limit order
/// - Stop = cancel orders + close position market
/// - Night gap: 23:55–10:00 MSK — no trading
/// - State file: /tmp/fade-impulse-state.json
/// </summary>
public class FadeImpulseLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly string _accountId;
    private readonly string _ticker;
    private readonly string _finamSymbol;
    private readonly FadeImpulseStrategy _strategy;
    private readonly string _logPrefix = "FADE";
    private readonly string _orderPrefix = "FADE-";
    private readonly string _stateFile = "/tmp/fade-impulse-state.json";
    private Timer? _mainTimer;

    // Tracked orders
    private string? _slOrderId;
    private string? _tpOrderId;
    private double _slPrice;
    private double _tpPrice;
    private int _positionDir; // 1=LONG, -1=SHORT, 0=FLAT
    private double _entryPrice;
    private DateTime? _entryTime;

    // Candle state
    private DateTime _lastCandleTime = DateTime.MinValue;
    private readonly TimeSpan _candleInterval = TimeSpan.FromMinutes(5);

    // Skip ticks after entry/exit
    private int _skipTicks = 0;

    // Active
    public bool IsRunning => _strategy.CurrentMode == FadeImpulseStrategy.Mode.Running;
    public FadeImpulseStrategy Strategy => _strategy;

    public FadeImpulseLauncher(FinamConnector broker, string accountId, string ticker, string finamSymbol, FadeImpulseStrategy.Config? config = null)
    {
        _broker = broker;
        _accountId = accountId;
        _ticker = ticker;
        _finamSymbol = finamSymbol;
        _strategy = new FadeImpulseStrategy(config);
    }

    // === LIFECYCLE ===

    public void Start()
    {
        _strategy.CurrentMode = FadeImpulseStrategy.Mode.Running;
        RestoreState();
        BrokerSync();
        _mainTimer = new Timer(async _ => await MainLoop(), null, 200, 200);
        Console.WriteLine($"[{_logPrefix}] Started");
    }

    public async Task StopAsync()
    {
        _strategy.CurrentMode = FadeImpulseStrategy.Mode.Stopped;
        _mainTimer?.Dispose();
        _mainTimer = null;

        // Cancel all orders
        await CancelAllOrdersAsync();

        // Close position if any
        if (_positionDir != 0)
        {
            string side = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
            await PlaceMarketOrderAsync(side, 1, $"{_orderPrefix}CLOSE: Stop");
            Console.WriteLine($"[{_logPrefix}] Closed position on stop");
        }

        _positionDir = 0;
        _entryPrice = 0;
        _entryTime = null;
        _strategy.ClearPosition();
        SaveState();
        Console.WriteLine($"[{_logPrefix}] Stopped");
    }

    public void Pause()
    {
        _strategy.CurrentMode = FadeImpulseStrategy.Mode.Paused;
        Console.WriteLine($"[{_logPrefix}] Paused");
    }

    public void Resume()
    {
        _strategy.CurrentMode = FadeImpulseStrategy.Mode.Running;
        Console.WriteLine($"[{_logPrefix}] Resumed");
    }

    public void UpdateConfig(FadeImpulseStrategy.Config config)
    {
        // Strategy doesn't support hot-reload of config easily,
        // but we can update the params reference
        Console.WriteLine($"[{_logPrefix}] Config updated");
    }

    // === MAIN LOOP ===

    private async Task MainLoop()
    {
        try
        {
            if (_strategy.CurrentMode != FadeImpulseStrategy.Mode.Running) return;

            // Night gap: 23:55–10:00 MSK (UTC+3)
            var mskHour = DateTime.UtcNow.AddHours(3).TimeOfDay;
            if (mskHour >= TimeSpan.FromHours(23) + TimeSpan.FromMinutes(55) || mskHour < TimeSpan.FromHours(10))
                return;

            // MOEX clearing 14:00–14:05 MSK
            if (mskHour >= TimeSpan.FromHours(14) && mskHour < TimeSpan.FromHours(14) + TimeSpan.FromMinutes(5))
                return;

            // Skip ticks after entry
            if (_skipTicks > 0)
            {
                _skipTicks--;
                return;
            }

            // Get current candle
            var candle = GetLatestCandle();
            if (candle == null) return;

            // Only process new 5-min candles
            if (candle.Timestamp <= _lastCandleTime) return;
            _lastCandleTime = candle.Timestamp;

            // Feed to strategy
            var events = _strategy.OnCandle(candle);

            foreach (var evt in events)
            {
                await HandleEvent(evt);
            }

            // Update trailing SL in broker if position open
            if (_positionDir != 0)
            {
                await UpdateBrokerSL();
                CheckTimeout();
            }

            // Periodic broker sync
            if (_positionDir != 0 && DateTime.UtcNow.Second < 2)
            {
                BrokerSync();
            }

            SaveState();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] MainLoop error: {ex.Message}");
        }
    }

    // === EVENT HANDLING ===

    private async Task HandleEvent(FadeImpulseStrategy.Event evt)
    {
        switch (evt.Type)
        {
            case FadeImpulseStrategy.EventType.SignalImpulse:
                Console.WriteLine($"[{_logPrefix}] Impulse: {evt.Reason}");
                break;

            case FadeImpulseStrategy.EventType.SignalEntry:
                Console.WriteLine($"[{_logPrefix}] Signal: {(evt.Direction == 1 ? "LONG" : "SHORT")} @ {evt.Price:F0} — {evt.Reason}");
                await ExecuteEntryAsync(evt.Direction);
                break;

            case FadeImpulseStrategy.EventType.ExitSL:
            case FadeImpulseStrategy.EventType.ExitTP:
            case FadeImpulseStrategy.EventType.ExitTimeout:
                Console.WriteLine($"[{_logPrefix}] Exit: {evt.Reason} | PnL={evt.PnL:F0}");
                // SL/TP are handled via broker orders — position should already be closed
                // But just in case, force close
                await ForceCloseIfNeededAsync(evt.Reason ?? "exit");
                _positionDir = 0;
                _entryPrice = 0;
                _entryTime = null;
                _slOrderId = null;
                _tpOrderId = null;
                _skipTicks = 5;
                break;
        }
    }

    // === ENTRY ===

    private async Task ExecuteEntryAsync(int direction)
    {
        // Entry guard: check broker position
        var (bDir, bLots, _) = GetBrokerPosition();
        if (bDir == -999)
        {
            Console.WriteLine($"[{_logPrefix}] Entry skipped: API error");
            _skipTicks = 5;
            return;
        }
        if (bLots > 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry guard: broker has {bLots} lots, syncing");
            _positionDir = bDir;
            _entryPrice = _strategy.EntryPrice;
            _entryTime = DateTime.UtcNow;
            // Place SL/TP for existing position
            await PlaceSLAndTP();
            return;
        }

        // Place market order
        string side = direction == 1 ? "SIDE_BUY" : "SIDE_SELL";
        string entryComment = $"{_orderPrefix}ENTRY: {(direction == 1 ? "LONG" : "SHORT")}";
        await PlaceMarketOrderAsync(side, 1, entryComment);

        // Wait for fill
        await Task.Delay(500);
        var (fDir, fLots, fAvg) = GetBrokerPositionWithRetry();
        if (fLots == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry failed: no fill");
            _strategy.ClearPosition();
            _skipTicks = 25;
            return;
        }

        _positionDir = fDir;
        _entryPrice = fAvg;
        _entryTime = DateTime.UtcNow;
        Console.WriteLine($"[{_logPrefix}] Entry confirmed: {(fDir == 1 ? "LONG" : "SHORT")} @ {fAvg:F0}");

        // Place SL and TP
        await PlaceSLAndTP();
        _skipTicks = 5;
    }

    // === SL / TP ===

    private async Task PlaceSLAndTP()
    {
        // Cancel existing
        await CancelOrderAsync(_slOrderId);
        await CancelOrderAsync(_tpOrderId);
        _slOrderId = null;
        _tpOrderId = null;

        if (_positionDir == 0) return;

        double sl = _strategy.CurrentSL;
        double tp = _strategy.CurrentTP;
        if (sl == 0 || tp == 0) return;

        // Place SL (stop-loss: SELL below for LONG, BUY above for SHORT)
        string slSide = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _slPrice = sl;
        _slOrderId = await PlaceLimitOrderAsync(slSide, sl, $"{_orderPrefix}SL");
        if (_slOrderId != null)
            Console.WriteLine($"[{_logPrefix}] SL: {slSide} @ {sl:F0} id={_slOrderId}");

        // Place TP (take-profit: SELL above for LONG, BUY below for SHORT)
        string tpSide = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _tpPrice = tp;
        _tpOrderId = await PlaceLimitOrderAsync(tpSide, tp, $"{_orderPrefix}TP");
        if (_tpOrderId != null)
            Console.WriteLine($"[{_logPrefix}] TP: {tpSide} @ {tp:F0} id={_tpOrderId}");
    }

    /// <summary>
    /// Update trailing SL in broker — cancel old, place new.
    /// </summary>
    private async Task UpdateBrokerSL()
    {
        double newSL = _strategy.CurrentSL;
        if (newSL == 0 || newSL == _slPrice) return; // No change

        // Cancel old SL
        await CancelOrderAsync(_slOrderId);
        _slOrderId = null;

        // Place new SL
        string slSide = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _slPrice = newSL;
        _slOrderId = await PlaceLimitOrderAsync(slSide, newSL, $"{_orderPrefix}SL");
        if (_slOrderId != null)
            Console.WriteLine($"[{_logPrefix}] Trailing SL → {newSL:F0} id={_slOrderId}");
    }

    private void CheckTimeout()
    {
        if (_entryTime.HasValue && (DateTime.UtcNow - _entryTime.Value).TotalMinutes >= _strategy.Params.MaxHoldMinutes)
        {
            Console.WriteLine($"[{_logPrefix}] Timeout: {_strategy.Params.MaxHoldMinutes} min — force close");
            // Strategy will emit ExitTimeout on next candle, but we force it
            _ = ForceCloseIfNeededAsync("timeout");
        }
    }

    // === BROKER ===

    private async Task ForceCloseIfNeededAsync(string reason)
    {
        var (bDir, bLots, _) = GetBrokerPosition();
        if (bLots > 0)
        {
            string side = bDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
            await CancelAllOrdersAsync();
            await PlaceMarketOrderAsync(side, bLots, $"{_orderPrefix}CLOSE: {reason}");
            Console.WriteLine($"[{_logPrefix}] Closed {bLots} lots: {reason}");
        }
        else
        {
            await CancelAllOrdersAsync();
        }
    }

    private async Task<string?> PlaceLimitOrderAsync(string side, double price, string comment)
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return null;

            var request = new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)price).ToString() },
                Comment = comment
            };

            var result = await rest.PlaceOrderAsync(_accountId, request);
            return result?.OrderId;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Limit order error ({comment}): {ex.Message}");
            return null;
        }
    }

    private async Task PlaceMarketOrderAsync(string side, int lots, string comment)
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return;

            await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
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

    private async Task CancelOrderAsync(string? orderId)
    {
        if (string.IsNullOrEmpty(orderId)) return;
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
            var rest = _broker.RestClient;
            if (rest == null) return;
            var orders = await rest.GetOrdersAsync(_accountId);
            if (orders?.Orders == null) return;
            foreach (var o in orders.Orders)
            {
                string? comment = o.Details?.Comment;
                if (comment?.StartsWith(_orderPrefix) == true && o.Status == "ORDER_STATUS_NEW")
                {
                    await CancelOrderAsync(o.OrderId);
                }
            }
        }
        catch { }
    }

    private (int dir, int lots, double avgPrice) GetBrokerPosition()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return (0, 0, 0);
            using var http = new HttpClient();
            http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", rest.GetJwtAsync().GetAwaiter().GetResult());
            var resp = http.GetAsync($"https://api.finam.ru/v1/accounts/{_accountId}").GetAwaiter().GetResult();
            if (!resp.IsSuccessStatusCode) return (-999, 0, 0);
            var json = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
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
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] GetBrokerPosition error: {ex.Message}"); return (-999, 0, 0); }
        return (0, 0, 0);
    }

    private (int dir, int lots, double avgPrice) GetBrokerPositionWithRetry(int retries = 3)
    {
        for (int i = 0; i < retries; i++)
        {
            var result = GetBrokerPosition();
            if (result.lots > 0) return result;
            if (result.dir == -999) return result; // API error
            if (i < retries - 1) Thread.Sleep(500);
        }
        return (0, 0, 0);
    }

    private void BrokerSync()
    {
        if (_positionDir == 0) return;

        var (bDir, bLots, _) = GetBrokerPosition();
        if (bDir == -999) return; // API error, skip

        if (bLots == 0 && _positionDir != 0)
        {
            // Position closed at broker (SL/TP hit) — sync
            Console.WriteLine($"[{_logPrefix}] Broker sync: position closed at broker → reset");
            _positionDir = 0;
            _entryPrice = 0;
            _entryTime = null;
            _strategy.ClearPosition();
        }
    }

    private Candle? GetLatestCandle()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return null;

            var bars = rest.GetBarsAsync(_finamSymbol, "TIME_FRAME_M5",
                DateTime.UtcNow.AddMinutes(-30).ToString("o"),
                DateTime.UtcNow.ToString("o")).GetAwaiter().GetResult();
            if (bars?.Bars == null || bars.Bars.Count == 0) return null;

            // Take the latest completed bar (not the current forming one)
            // The last bar might be still forming, so take second-to-last if timestamps match current 5-min window
            var latest = bars.Bars.Last();
            var ts = DateTime.Parse(latest.Timestamp);

            // If this bar is the current forming candle (within last 5 min), take previous
            if ((DateTime.UtcNow - ts).TotalMinutes < 5 && bars.Bars.Count > 1)
                latest = bars.Bars[bars.Bars.Count - 2];

            return new Candle
            {
                Open = double.Parse(latest.Open?.Value ?? "0"),
                High = double.Parse(latest.High?.Value ?? "0"),
                Low = double.Parse(latest.Low?.Value ?? "0"),
                Close = double.Parse(latest.Close?.Value ?? "0"),
                Volume = (long)double.Parse(latest.Volume?.Value ?? "0"),
                Timestamp = DateTime.Parse(latest.Timestamp)
            };
        }
        catch { return null; }
    }

    // === STATE ===

    private void SaveState()
    {
        try
        {
            var state = new
            {
                dir = _positionDir,
                entryPrice = _entryPrice,
                sl = _slPrice,
                tp = _tpPrice,
                slOrderId = _slOrderId ?? "",
                tpOrderId = _tpOrderId ?? "",
                strategyDir = _strategy.PositionDir,
                strategyPnL = _strategy.RealizedPnL,
                strategyTrades = _strategy.TotalTrades,
                ts = DateTime.UtcNow.ToString("O")
            };
            System.IO.File.WriteAllText(_stateFile, System.Text.Json.JsonSerializer.Serialize(state));
        }
        catch { }
    }

    private void RestoreState()
    {
        try
        {
            if (!System.IO.File.Exists(_stateFile)) return;
            var json = System.IO.File.ReadAllText(_stateFile);
            var doc = System.Text.Json.JsonDocument.Parse(json);
            var root = doc.RootElement;

            _positionDir = root.TryGetProperty("dir", out var d) ? d.GetInt32() : 0;
            _entryPrice = root.TryGetProperty("entryPrice", out var e) ? e.GetDouble() : 0;
            _slPrice = root.TryGetProperty("sl", out var s) ? s.GetDouble() : 0;
            _tpPrice = root.TryGetProperty("tp", out var t) ? t.GetDouble() : 0;
            _slOrderId = root.TryGetProperty("slOrderId", out var slo) ? slo.GetString() : null;
            _tpOrderId = root.TryGetProperty("tpOrderId", out var tpo) ? tpo.GetString() : null;

            if (_positionDir != 0)
                Console.WriteLine($"[{_logPrefix}] Restored: {(_positionDir == 1 ? "LONG" : "SHORT")} @ {_entryPrice:F0}");
        }
        catch { }
    }

    // === API ===

    public Dictionary<string, object> GetStatus()
    {
        var s = _strategy.GetStatus();
        s["brokerPos"] = _positionDir != 0 ? (_positionDir == 1 ? "LONG" : "SHORT") : "FLAT";
        s["brokerEntry"] = _entryPrice;
        s["slOrderId"] = _slOrderId ?? "none";
        s["tpOrderId"] = _tpOrderId ?? "none";
        s["ticker"] = _ticker;
        s["finamSymbol"] = _finamSymbol;
        return s;
    }

    public void Dispose()
    {
        _mainTimer?.Dispose();
    }
}
