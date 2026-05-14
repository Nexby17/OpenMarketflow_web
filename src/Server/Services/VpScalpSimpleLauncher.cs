using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// VP Scalp Simple Launcher — 5-min candle polling, trailing SL%, POC exit.
/// No grid, no TP. 1 lot.
/// </summary>
public class VpScalpSimpleLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly string _accountId;
    private readonly string _ticker;
    private readonly string _finamSymbol;
    private readonly VpScalpSimpleStrategy _strategy;
    private readonly string _logPrefix = "VP-SIMPLE";
    private readonly string _orderPrefix = "VPS-";
    private readonly string _stateFile = "/tmp/vp-simple-state.json";
    private Timer? _mainTimer;

    // Position tracking
    private int _positionDir; // 1=LONG, -1=SHORT, 0=FLAT
    private double _entryPrice;
    private double _currentSL;
    private DateTime? _entryTime;
    private string? _slOrderId;

    // Candle state
    private DateTime _lastCandleTime = DateTime.MinValue;

    // VP data buffer
    private readonly List<double> _vpCloses = new();
    private readonly List<double> _vpVolumes = new();

    public bool IsRunning => _strategy.CurrentMode == VpScalpSimpleStrategy.Mode.Running;
    public VpScalpSimpleStrategy Strategy => _strategy;

    public VpScalpSimpleLauncher(FinamConnector broker, string accountId, string ticker, string finamSymbol, VpScalpSimpleStrategy.Config? config = null)
    {
        _broker = broker;
        _accountId = accountId;
        _ticker = ticker;
        _finamSymbol = finamSymbol;
        _strategy = new VpScalpSimpleStrategy(config);
    }

    // === LIFECYCLE ===

    public void Start()
    {
        _strategy.CurrentMode = VpScalpSimpleStrategy.Mode.Running;
        RestoreState();
        BrokerSync();
        _mainTimer = new Timer(async _ => await MainLoop(), null, 2000, 500);
        Console.WriteLine($"[{_logPrefix}] Started on {_ticker}");
    }

    public async Task StopAsync()
    {
        _strategy.CurrentMode = VpScalpSimpleStrategy.Mode.Stopped;
        _mainTimer?.Dispose();
        _mainTimer = null;

        await CancelAllOrdersAsync();

        if (_positionDir != 0)
        {
            string side = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
            await PlaceMarketOrderAsync(side, 1, $"{_orderPrefix}CLOSE: Stop");
            Console.WriteLine($"[{_logPrefix}] Closed position on stop");
        }

        _positionDir = 0;
        _entryPrice = 0;
        _currentSL = 0;
        _entryTime = null;
        _strategy.ClearPosition();
        SaveState();
        Console.WriteLine($"[{_logPrefix}] Stopped");
    }

    public void Pause() { _strategy.CurrentMode = VpScalpSimpleStrategy.Mode.Paused; Console.WriteLine($"[{_logPrefix}] Paused"); }
    public void Resume() { _strategy.CurrentMode = VpScalpSimpleStrategy.Mode.Running; Console.WriteLine($"[{_logPrefix}] Resumed"); }

    // === MAIN LOOP ===

    private async Task MainLoop()
    {
        try
        {
            if (_strategy.CurrentMode != VpScalpSimpleStrategy.Mode.Running) return;

            // Night gap: 23:55–10:00 MSK
            var msk = DateTime.UtcNow.AddHours(3).TimeOfDay;
            if (msk >= TimeSpan.FromHours(23) + TimeSpan.FromMinutes(55) || msk < TimeSpan.FromHours(10))
                return;

            // Clearing 14:00–14:05 MSK
            if (msk >= TimeSpan.FromHours(14) && msk < TimeSpan.FromHours(14) + TimeSpan.FromMinutes(5))
                return;

            // === FAST TICK: SL + POC по current_price (каждые 500мс) ===
            if (_positionDir != 0)
            {
                var (bDir, bLots, bCurPrice) = GetBrokerPosition();
                if (bDir == -999) return;
                if (bLots == 0 && _positionDir != 0)
                {
                    // Flicker check
                    await Task.Delay(200);
                    var (rDir, rLots, _) = GetBrokerPosition();
                    if (rLots == 0 && rDir != -999)
                    {
                        Console.WriteLine($"[{_logPrefix}] No broker position → reset");
                        _positionDir = 0; _entryPrice = 0; _currentSL = 0; _entryTime = null;
                        await CancelAllOrdersAsync();
                        SaveState();
                        return;
                    }
                }

                if (bCurPrice > 0)
                {
                    // POC exit
                    if (_strategy.POC > 0)
                    {
                        bool pocHit = (_positionDir == 1 && bCurPrice >= _strategy.POC) ||
                                      (_positionDir == -1 && bCurPrice <= _strategy.POC);
                        if (pocHit)
                        {
                            Console.WriteLine($"[{_logPrefix}] Exit: POC hit @ {bCurPrice:F0} POC={_strategy.POC:F0}");
                            await ForceCloseAsync($"POC hit @ {bCurPrice:F0}");
                            _positionDir = 0; _entryPrice = 0; _currentSL = 0; _entryTime = null;
                            SaveState(); return;
                        }
                    }

                    // Trailing SL check по current_price
                    if (_currentSL > 0)
                    {
                        bool slHit = (_positionDir == 1 && bCurPrice <= _currentSL) ||
                                     (_positionDir == -1 && bCurPrice >= _currentSL);
                        if (slHit)
                        {
                            Console.WriteLine($"[{_logPrefix}] Exit: SL hit @ {bCurPrice:F0} SL={_currentSL:F0}");
                            await ForceCloseAsync($"SL hit @ {bCurPrice:F0}");
                            _positionDir = 0; _entryPrice = 0; _currentSL = 0; _entryTime = null;
                            SaveState(); return;
                        }
                    }
                }
            }

            // === CANDLE TICK: VP + signals (только при новой свече) ===
            var candle = GetLatestCandle();
            if (candle == null) return;
            if (candle.Timestamp <= _lastCandleTime) return;
            _lastCandleTime = candle.Timestamp;
            Console.WriteLine($"[{_logPrefix}] Candle: {candle.Timestamp:HH:mm} C={candle.Close:F0} V={candle.Volume} VPcnt={_vpCloses.Count}");

            // Update VP buffer
            _vpCloses.Add(candle.Close);
            _vpVolumes.Add(candle.Volume);
            if (_vpCloses.Count > _strategy.Params.VpLookback)
            {
                _vpCloses.RemoveAt(0);
                _vpVolumes.RemoveAt(0);
            }

            // Update VP
            if (_vpCloses.Count >= 20)
                _strategy.UpdateVP(_vpCloses.ToArray(), _vpVolumes.ToArray());

            // If in position — update trailing + check timeout
            if (_positionDir != 0)
            {
                // Update trailing SL от свечи
                _strategy.UpdateTrailingSL(candle.Close);
                double newSL = _strategy.CurrentSL;

                // Update SL order if changed
                if (Math.Abs(newSL - _currentSL) > 1)
                {
                    await UpdateSLOrder(newSL);
                    _currentSL = newSL;
                }

                // Check timeout only (SL and POC handled above by current_price)
                if (_entryTime.HasValue)
                {
                    int holdMin = (int)(DateTime.UtcNow - _entryTime.Value).TotalMinutes;
                    if (holdMin >= _strategy.Params.MaxHoldMinutes)
                    {
                        var (bDir2, bLots2, bAvg2) = GetBrokerPosition();
                        if (bLots2 > 0)
                        {
                            double unrealized = (bAvg2 - _entryPrice) * _positionDir;
                            double perLot = unrealized;
                            if (perLot > 0)
                            {
                                Console.WriteLine($"[{_logPrefix}] Exit: Timeout {holdMin}min");
                                await ForceCloseAsync($"Timeout {holdMin}min");
                                _positionDir = 0; _entryPrice = 0; _currentSL = 0; _entryTime = null;
                                SaveState(); return;
                            }
                        }
                    }
                }
            }

            // If flat — check signal
            if (_positionDir == 0)
            {
                int signal = _strategy.CheckSignal(candle.Close);
                if (signal != 0)
                {
                    await ExecuteEntryAsync(signal, candle.Close);
                }
            }

            // Periodic broker sync
            if (DateTime.UtcNow.Second < 2 && _positionDir != 0)
                BrokerSync();

            SaveState();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] MainLoop error: {ex.Message}");
        }
    }

    // === ENTRY ===

    private async Task ExecuteEntryAsync(int direction, double signalPrice)
    {
        // Entry guard: check broker
        var (bDir, bLots, _) = GetBrokerPosition();
        if (bDir == -999) { Console.WriteLine($"[{_logPrefix}] Entry skipped: API error"); return; }

        if (bLots > 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry guard: broker has {bLots} lots, syncing");
            _positionDir = bDir;
            _entryPrice = signalPrice;
            _entryTime = DateTime.UtcNow;
            _strategy.OnEntry(bDir, signalPrice);
            _currentSL = _strategy.CurrentSL;
            await PlaceSLOrder();
            return;
        }

        // Market order
        string side = direction == 1 ? "SIDE_BUY" : "SIDE_SELL";
        await PlaceMarketOrderAsync(side, 1, $"{_orderPrefix}ENTRY: {(direction == 1 ? "LONG" : "SHORT")}");

        // Confirm fill
        await Task.Delay(500);
        var (fDir, fLots, fAvg) = GetBrokerPositionWithRetry();
        if (fLots == 0)
        {
            Console.WriteLine($"[{_logPrefix}] Entry failed: no fill");
            _strategy.ClearPosition();
            return;
        }

        _positionDir = fDir;
        _entryPrice = fAvg;
        _entryTime = DateTime.UtcNow;
        _strategy.OnEntry(fDir, fAvg);
        _currentSL = _strategy.CurrentSL;

        Console.WriteLine($"[{_logPrefix}] Entry {(fDir == 1 ? "LONG" : "SHORT")} @ {fAvg:F0} | SL={_currentSL:F0} | VAL={_strategy.VAL:F0} VAH={_strategy.VAH:F0} POC={_strategy.POC:F0}");

        await PlaceSLOrder();
        SaveState();
    }

    // === SL ORDER ===

    private async Task PlaceSLOrder()
    {
        if (_positionDir == 0 || _currentSL == 0) return;

        await CancelOrderAsync(_slOrderId);
        _slOrderId = null;

        string side = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _slOrderId = await PlaceLimitOrderAsync(side, _currentSL, $"{_orderPrefix}SL");
        if (_slOrderId != null)
            Console.WriteLine($"[{_logPrefix}] SL: {side} @ {_currentSL:F0}");
    }

    private async Task UpdateSLOrder(double newSL)
    {
        if (_positionDir == 0) return;
        await CancelOrderAsync(_slOrderId);
        _slOrderId = null;

        string side = _positionDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _slOrderId = await PlaceLimitOrderAsync(side, newSL, $"{_orderPrefix}SL");
        if (_slOrderId != null)
            Console.WriteLine($"[{_logPrefix}] Trailing SL → {newSL:F0}");
    }

    // === FORCE CLOSE ===

    private async Task ForceCloseAsync(string reason)
    {
        var (bDir, bLots, _) = GetBrokerPosition();
        if (bLots > 0)
        {
            string side = bDir == 1 ? "SIDE_SELL" : "SIDE_BUY";
            await CancelAllOrdersAsync();
            await PlaceMarketOrderAsync(side, bLots, $"{_orderPrefix}CLOSE: {reason}");
            double pnl = _strategy.OnExit(0, reason); // simplified
            Console.WriteLine($"[{_logPrefix}] Closed {bLots} lots: {reason} | PnL={_strategy.RealizedPnL:F0}");
        }
        else
        {
            await CancelAllOrdersAsync();
            _strategy.OnExit(0, reason);
        }
    }

    // === BROKER ===

    private (int dir, int lots, double avg) GetBrokerPosition()
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

    private (int dir, int lots, double avg) GetBrokerPositionWithRetry(int retries = 3)
    {
        for (int i = 0; i < retries; i++)
        {
            var r = GetBrokerPosition();
            if (r.lots > 0) return r;
            if (r.dir == -999) return r;
            if (i < retries - 1) Thread.Sleep(500);
        }
        return (0, 0, 0);
    }

    private void BrokerSync()
    {
        if (_positionDir == 0) return;
        var (bDir, bLots, _) = GetBrokerPosition();
        if (bDir == -999) return;
        if (bLots == 0 && _positionDir != 0)
        {
            Console.WriteLine($"[{_logPrefix}] Broker sync: position closed at broker → reset");
            _positionDir = 0;
            _entryPrice = 0;
            _currentSL = 0;
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

            var latest = bars.Bars.Last();
            var ts = DateTime.Parse(latest.Timestamp);
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
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] GetLatestCandle error: {ex.Message}"); return null; }
    }

    // === ORDER HELPERS ===

    private async Task<string?> PlaceLimitOrderAsync(string side, double price, string comment)
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return null;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = side,
                OrderType = "ORDER_TYPE_LIMIT",
                Price = new() { Value = ((int)price).ToString() },
                Comment = comment
            });
            return result?.OrderId;
        }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Limit order error ({comment}): {ex.Message}"); return null; }
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
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] Market order error: {ex.Message}"); }
    }

    private async Task CancelOrderAsync(string? orderId)
    {
        if (string.IsNullOrEmpty(orderId)) return;
        try { var rest = _broker.RestClient; if (rest != null) await rest.CancelOrderAsync(_accountId, orderId); }
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
                if (o.Details?.Comment?.StartsWith(_orderPrefix) == true && o.Status == "ORDER_STATUS_NEW")
                    await CancelOrderAsync(o.OrderId);
            }
        }
        catch { }
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
                sl = _currentSL,
                slOrderId = _slOrderId ?? "",
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
            _currentSL = root.TryGetProperty("sl", out var s) ? s.GetDouble() : 0;
            _slOrderId = root.TryGetProperty("slOrderId", out var slo) ? slo.GetString() : null;
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
        s["ticker"] = _ticker;
        s["finamSymbol"] = _finamSymbol;
        return s;
    }

    public void Dispose() { _mainTimer?.Dispose(); }
}
