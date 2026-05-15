using HedgeFund.Brokers.Finam;
using HedgeFund.Core.Strategies;
using HedgeFund.Core.Connectors;

namespace HedgeFund.Server.Services;

/// <summary>
/// V8 Trail Launcher — Inverted PSAR×EMA с trailing SL.
/// 5-мин таймфрейм, REST-only, брокер=истина.
/// </summary>
public class V8TrailLauncher
{
    private readonly FinamConnector _broker;
    private readonly string _accountId;
    private readonly string _ticker;
    public string Ticker => _ticker;
    private readonly string _finamSymbol;
    private readonly double _stepPrice;
    private readonly string _logPrefix = "V8TR";

    public V8TrailStrategy Strategy { get; } = new();
    public bool IsConnected => _broker != null;

    private System.Threading.Timer? _mainTimer;
    private int _tickCount = 0;
    private DateTime _lastCandleTime = DateTime.MinValue;
    private bool _clearingPaused = false;
    private int _skipTicks = 0;

    // Order tracking
    private string? _entryOrderId = null;
    private string? _slOrderId = null;

    public V8TrailLauncher(FinamConnector broker, string accountId, string ticker, string finamSymbol, double stepPrice, V8TrailStrategy.V8Params? config = null)
    {
        _broker = broker;
        _accountId = accountId;
        _ticker = ticker;
        _finamSymbol = finamSymbol;
        _stepPrice = stepPrice;
        if (config != null) Strategy.Params = config;
    }

    public void Start()
    {
        Console.WriteLine($"[{_logPrefix}] ✅ Started on {_ticker} SL={Strategy.Params.SlPct}% EMA={Strategy.Params.EmaPeriod}");
        _mainTimer = new System.Threading.Timer(MainLoopTick, null, TimeSpan.FromSeconds(2), TimeSpan.FromMilliseconds(2000));
    }

    public async Task StopAsync()
    {
        _mainTimer?.Dispose();
        _mainTimer = null;
        Console.WriteLine($"[{_logPrefix}] ⏹ Stop");

        // Cancel any open orders
        await CancelAllOrdersAsync();

        // Close position if any
        if (Strategy.PositionDirection != 0)
        {
            int dir = Strategy.PositionDirection;
            int lots = 1;
            // Check broker for actual lots
            var (bDir, bLots, _) = await GetBrokerPositionAsync();
            if (bLots > 0) lots = bLots;

            await PlaceMarketOrderAsync(dir == 1 ? "SIDE_SELL" : "SIDE_BUY", lots, $"{_logPrefix}-CLOSE: Stop");
            Strategy.ClosePosition(Strategy.EntryPrice, _stepPrice);
        }
    }

    private async void MainLoopTick(object? state)
    {
        try { await MainLoopAsync(); }
        catch (Exception ex) { Console.WriteLine($"[{_logPrefix}] MainLoop error: {ex.Message}"); }
    }

    private async Task MainLoopAsync()
    {
        _tickCount++;

        if (_skipTicks > 0) { _skipTicks--; return; }

        var now = DateTime.UtcNow;
        var msk = now.AddHours(3);
        var mskTime = msk.TimeOfDay;

        // Clearing 14:00-14:05 MSK
        if (mskTime >= new TimeSpan(13, 59, 59) && mskTime <= new TimeSpan(14, 5, 1))
        {
            if (!_clearingPaused) { Console.WriteLine($"[{_logPrefix}] Clearing"); _clearingPaused = true; }
            return;
        }
        _clearingPaused = false;

        // Night: no trading 23:55-10:00 MSK
        if (mskTime >= new TimeSpan(23, 55, 0) || mskTime < new TimeSpan(7, 0, 0)) return;

        // Check broker position
        var (brokerDir, brokerLots, brokerAvg) = await GetBrokerPositionAsync();
        if (brokerDir == -999) return; // API error, skip

        bool brokerHasPos = brokerLots > 0;
        bool robotHasPos = Strategy.PositionDirection != 0;

        // API error guard
        if (brokerDir == -999 && robotHasPos) return;

        // No broker position → flicker check, then reset
        if (!brokerHasPos)
        {
            if (robotHasPos)
            {
                await Task.Delay(200);
                var (recheckDir, recheckLots, _) = await GetBrokerPositionAsync();
                if (recheckLots > 0 && recheckDir != -999) return; // flicker
                if (recheckDir == -999) return; // still error

                Console.WriteLine($"[{_logPrefix}] No broker position → reset");
                Strategy.ClearPosition();
                await CancelAllOrdersAsync();
            }
        }
        else if (!robotHasPos)
        {
            // Broker has position but robot doesn't — restore
            Console.WriteLine($"[{_logPrefix}] Restore from broker: dir={brokerDir} price={brokerAvg:F0}");
            Strategy.RestorePosition(brokerDir, brokerAvg);
        }

        // SL fill detection: check if SL limit order filled
        if (Strategy.PositionDirection != 0 && _slOrderId != null)
        {
            var orders = await GetBrokerOrdersAsync();
            var slOrder = orders.FirstOrDefault(o => o.id == _slOrderId);
            if (slOrder.id == null) // SL order no longer exists → filled or cancelled
            {
                Console.WriteLine($"[{_logPrefix}] SL fill detected");
                _slOrderId = null;
                // Check if broker still has position (partial fill?)
                if (brokerLots == 0)
                {
                    Strategy.ClearPosition();
                    await CancelAllOrdersAsync();
                }
                else
                {
                    // SL filled but position still exists → close remaining
                    await ClosePositionAsync("SL fill, closing remaining");
                }
                return;
            }
        }

        // Process candles
        await ProcessCandlesAsync();
    }

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

                double close = double.Parse(bar.Close.Value);
                double high = bar.High != null ? double.Parse(bar.High.Value) : close;
                double low = bar.Low != null ? double.Parse(bar.Low.Value) : close;
                double vol = double.Parse(bar.Volume?.Value ?? "0");

                // Update trailing SL
                if (Strategy.PositionDirection != 0)
                {
                    double prevSL = Strategy.CurrentSLPrice;
                    Strategy.UpdateTrailing(high, low);
                    double newSL = Strategy.CurrentSLPrice;
                    if (Math.Abs(newSL - prevSL) >= 1)
                    {
                        await UpdateSLOrderAsync(newSL);
                    }

                    // Timeout check
                    if (Strategy.HoldMinutes >= Strategy.Params.MaxHoldMinutes)
                    {
                        Console.WriteLine($"[{_logPrefix}] Exit: Timeout ({Strategy.HoldMinutes} min)");
                        await ClosePositionAsync($"Timeout ({Strategy.HoldMinutes} min)");
                        _lastCandleTime = ts;
                        continue;
                    }
                }

                // Feed bar → get signal
                int signal = Strategy.OnBar(close, high, low);
                _lastCandleTime = ts;

                // Entry signal (only if no position)
                if (Strategy.PositionDirection == 0 && signal != 0)
                {
                    // Entry guard: check broker
                    var (guardDir, guardLots, _) = await GetBrokerPositionAsync();
                    if (guardLots > 0)
                    {
                        Console.WriteLine($"[{_logPrefix}] Entry guard: broker has position, skipping");
                        continue;
                    }

                    Console.WriteLine($"[{_logPrefix}] Signal: {(signal == 1 ? "LONG" : "SHORT")} @ {close:F0} SAR={Strategy.CurrentSar:F0} EMA={Strategy.CurrentEma:F0}");
                    await ExecuteEntryAsync(signal, close);
                }
            }
        }
        catch { }
    }

    private async Task ExecuteEntryAsync(int dir, double price)
    {
        try
        {
            var rest = _broker.RestClient;
            var result = await rest.PlaceOrderAsync(_accountId, new PlaceOrderRequest
            {
                Symbol = _finamSymbol,
                Quantity = new() { Value = "1" },
                Side = dir == 1 ? "SIDE_BUY" : "SIDE_SELL",
                OrderType = "ORDER_TYPE_MARKET",
                Comment = $"{_logPrefix}-ENTRY: {(dir == 1 ? "LONG" : "SHORT")}"
            });

            if (!string.IsNullOrEmpty(result?.OrderId))
            {
                Strategy.OpenPosition(dir, price);
                Console.WriteLine($"[{_logPrefix}] Entry {(dir == 1 ? "LONG" : "SHORT")} @ {price:F0}");

                // Ставим SL limit ордер (округляем до minStep)
                int minStep = _finamSymbol.Contains("RI") ? 10 : 1;
                double slPrice = dir == 1 ? price * (1 - Strategy.Params.SlPct / 100) : price * (1 + Strategy.Params.SlPct / 100);
                slPrice = Math.Floor(slPrice / minStep) * minStep; // округляем ВНИЗ для LONG SL, ВВЕРХ для SHORT SL
                if (dir == -1) slPrice = Math.Ceiling(slPrice / minStep) * minStep;
                string slSide = dir == 1 ? "SIDE_SELL" : "SIDE_BUY";
                _slOrderId = await PlaceLimitOrderAsync(slSide, slPrice, $"{_logPrefix}-SL");
                if (_slOrderId != null)
                    Console.WriteLine($"[{_logPrefix}] SL limit: {slSide} @ {slPrice:F0}");
            }
            else
            {
                Console.WriteLine($"[{_logPrefix}] Entry failed");
                _skipTicks = 25;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Entry error: {ex.Message}");
            _skipTicks = 25;
        }
    }

    private async Task ClosePositionAsync(string reason)
    {
        try
        {
            // Cancel SL order first
            if (_slOrderId != null)
            {
                await CancelOrderAsync(_slOrderId);
                _slOrderId = null;
            }

            var (bDir, bLots, _) = await GetBrokerPositionAsync();
            int lots = bLots > 0 ? bLots : 1;
            int dir = Strategy.PositionDirection;

            await PlaceMarketOrderAsync(dir == 1 ? "SIDE_SELL" : "SIDE_BUY", lots, $"{_logPrefix}-CLOSE: {reason}");

            // Use SL price for PnL calc
            double exitPrice = Strategy.CurrentSLPrice > 0 ? Strategy.CurrentSLPrice : Strategy.EntryPrice;
            Strategy.ClosePosition(exitPrice, _stepPrice);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Close error: {ex.Message}");
        }
    }

    private async Task<(int dir, int lots, double avg)> GetBrokerPositionAsync()
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return (0, 0, 0);
            var account = await rest.GetAccountAsync(_accountId);
            if (account?.Positions == null) return (0, 0, 0);
            foreach (var p in account.Positions)
            {
                var sym = (p.Symbol ?? "").Split('@')[0];
                if (sym != _ticker) continue;
                long qty = p.EffectiveQuantity;
                if (qty == 0) continue;
                double avg = p.AveragePrice ?? 0;
                int d = qty > 0 ? 1 : -1;
                return (d, (int)Math.Abs(qty), avg);
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] GetBrokerPosition error: {ex.Message}");
            return (-999, 0, 0);
        }
        return (0, 0, 0);
    }

    private async Task<List<(string id, string comment)>> GetBrokerOrdersAsync()
    {
        var orders = new List<(string, string)>();
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return orders;
            var result = await rest.GetOrdersAsync(_accountId);
            if (result?.Orders == null) return orders;
            foreach (var o in result.Orders)
            {
                orders.Add((o.OrderId ?? "", ""));
            }
        }
        catch { }
        return orders;
    }

    private async Task CancelAllOrdersAsync()
    {
        try
        {
            var orders = await GetBrokerOrdersAsync();
            foreach (var (id, _) in orders)
            {
                if (!string.IsNullOrEmpty(id))
                {
                    try { await CancelOrderAsync(id); } catch { }
                }
            }
        }
        catch { }
    }

    private async Task CancelOrderAsync(string orderId)
    {
        try
        {
            var rest = _broker.RestClient;
            if (rest == null) return;
            await rest.CancelOrderAsync(_accountId, orderId);
        }
        catch { }
    }

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
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Market order error: {ex.Message}");
        }
    }

    private async Task<string?> PlaceLimitOrderAsync(string side, double price, string comment)
    {
        try
        {
            var rest = _broker.RestClient;
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
        catch (Exception ex)
        {
            Console.WriteLine($"[{_logPrefix}] Limit order error: {ex.Message}");
            return null;
        }
    }

    private async Task UpdateSLOrderAsync(double newSL)
    {
        if (Strategy.PositionDirection == 0) return;
        // Округляем до minStep
        int minStep = _finamSymbol.Contains("RI") ? 10 : 1;
        if (Strategy.PositionDirection == 1) newSL = Math.Floor(newSL / minStep) * minStep;
        else newSL = Math.Ceiling(newSL / minStep) * minStep;

        await CancelOrderAsync(_slOrderId ?? "");
        _slOrderId = null;

        string side = Strategy.PositionDirection == 1 ? "SIDE_SELL" : "SIDE_BUY";
        _slOrderId = await PlaceLimitOrderAsync(side, newSL, $"{_logPrefix}-SL");
        if (_slOrderId != null)
            Console.WriteLine($"[{_logPrefix}] Trailing SL → {newSL:F0}");
    }

    public string GetStatus()
    {
        var s = Strategy.GetStatus();
        return System.Text.Json.JsonSerializer.Serialize(s);
    }
}
