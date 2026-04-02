using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using HedgeFund.Core;
using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Quik;

/// <summary>
/// Коннектор к QUIK через файловый обмен (2 файла JSONL).
/// 
/// bridge/to_app.jsonl  — QUIK пишет (append), мы читаем и очищаем
/// bridge/to_quik.jsonl — мы пишем (append), QUIK читает и очищает
/// bridge/heartbeat     — QUIK обновляет timestamp каждые 3с
/// </summary>
public class QuikConnector : IBrokerConnector
{
    private CancellationTokenSource? _cts;
    private readonly string _bridgeDir;
    private string ToAppFile => Path.Combine(_bridgeDir, "to_app.jsonl");
    private string ToQuikFile => Path.Combine(_bridgeDir, "to_quik.jsonl");
    private string HeartbeatFile => Path.Combine(_bridgeDir, "heartbeat");

    // Подписки
    private readonly ConcurrentDictionary<string, Action<Candle>> _candleCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<double, double>> _level2Callbacks = new();
    private readonly ConcurrentDictionary<string, Action<OrderBookSnapshot>> _orderBookCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<QuoteData>> _quoteCallbacks = new();

    // Кэш
    private readonly ConcurrentDictionary<string, QuoteData> _lastQuotes = new();
    private readonly ConcurrentDictionary<string, Order> _activeOrders = new();
    private readonly ConcurrentDictionary<string, Position> _positions = new();
    private double _balance;
    private int _requestId;
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pendingRequests = new();

    // Fallback
    private readonly Finam.FinamConnector? _fallback;
    private bool _useFallback;
    private string _fallbackToken = string.Empty;
    private CancellationTokenSource? _fallbackCts;

    public string BrokerName => "QUIK (Финам)";
    public bool IsConnected { get; private set; }

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;
    public event Action<string, OrderBookSnapshot>? OnOrderBookUpdate;
    public event Action<string, QuoteData>? OnQuoteUpdate;

    /// <summary>
    /// bridgeDir — общая папка. По умолчанию: C:\OpenMarketflow\bridge
    /// Этот путь должен совпадать с BRIDGE_DIR в quik_bridge.lua
    /// </summary>
    public QuikConnector(string? bridgeDir = null, string? finamToken = null)
    {
        _bridgeDir = bridgeDir ?? @"C:\OpenMarketflow\bridge";
        if (!string.IsNullOrEmpty(finamToken))
        {
            _fallbackToken = finamToken;
            _fallback = new Finam.FinamConnector();
        }
    }

    public async Task<bool> ConnectAsync(string login = "", string password = "")
    {
        if (!string.IsNullOrEmpty(login) && string.IsNullOrEmpty(_fallbackToken))
            _fallbackToken = login;

        Directory.CreateDirectory(_bridgeDir);
        // Создаём пустые файлы
        if (!File.Exists(ToAppFile)) await File.WriteAllTextAsync(ToAppFile, "");
        if (!File.Exists(ToQuikFile)) await File.WriteAllTextAsync(ToQuikFile, "");

        _cts = new CancellationTokenSource();

        // Polling to_app.jsonl (каждые 200мс)
        _ = Task.Run(() => PollLoop(_cts.Token), _cts.Token);
        // Heartbeat мониторинг (каждые 3с)
        _ = Task.Run(() => HeartbeatMonitor(_cts.Token), _cts.Token);

        // Fallback
        if (_fallback != null && !string.IsNullOrEmpty(_fallbackToken))
            _ = Task.Run(async () => { try { await _fallback.ConnectAsync(_fallbackToken); } catch { } });

        OnError?.Invoke($"📂 Папка обмена: {_bridgeDir}");
        OnError?.Invoke("Запустите quik_bridge.lua в QUIK");
        return true;
    }

    public async Task DisconnectAsync()
    {
        _cts?.Cancel(); _fallbackCts?.Cancel(); _fallback?.Dispose();
        IsConnected = false; OnConnectionChanged?.Invoke(false);
        await Task.CompletedTask;
    }

    // === Чтение to_app.jsonl ===

    private long _lastReadPos;

    private async Task PollLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (File.Exists(ToAppFile))
                {
                    // Читаем новые строки с последней позиции
                    using var fs = new FileStream(ToAppFile, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite);
                    
                    if (fs.Length > _lastReadPos)
                    {
                        fs.Seek(_lastReadPos, SeekOrigin.Begin);
                        using var reader = new StreamReader(fs, Encoding.UTF8, leaveOpen: true);
                        
                        var lines = new List<string>();
                        string? line;
                        while ((line = await reader.ReadLineAsync(ct)) != null)
                        {
                            if (!string.IsNullOrWhiteSpace(line))
                                lines.Add(line);
                        }
                        
                        _lastReadPos = fs.Position;
                        
                        // Если файл большой (>100KB) — обрезаем
                        if (_lastReadPos > 100_000)
                        {
                            fs.SetLength(0);
                            _lastReadPos = 0;
                        }

                        foreach (var l in lines)
                        {
                            try
                            {
                                var doc = JsonDocument.Parse(l);
                                ProcessMessage(doc.RootElement);
                            }
                            catch { }
                        }
                    }
                }
            }
            catch (IOException) { } // файл занят — пропускаем
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                OnError?.Invoke($"Poll: {ex.Message}");
            }

            await Task.Delay(200, ct);
        }
    }

    // === Heartbeat ===

    private async Task HeartbeatMonitor(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                bool alive = false;
                if (File.Exists(HeartbeatFile))
                {
                    var age = DateTime.Now - File.GetLastWriteTime(HeartbeatFile);
                    alive = age.TotalSeconds < 10;
                }

                if (alive && !IsConnected)
                {
                    IsConnected = true; _useFallback = false;
                    StopFallbackPolling();
                    OnConnectionChanged?.Invoke(true);
                    OnError?.Invoke("✅ QUIK онлайн");
                    await ResubscribeAll();
                }
                else if (!alive && IsConnected)
                {
                    IsConnected = false;
                    OnConnectionChanged?.Invoke(false);
                    OnError?.Invoke("⚠️ QUIK офлайн → Finam REST fallback");
                    StartFallbackPolling();
                }
            }
            catch { }

            await Task.Delay(3000, ct);
        }
    }

    // === Отправка команд ===

    private readonly object _writeLock = new();

    private async Task<JsonElement> SendCommandAsync(string command, object parameters, int timeoutMs = 5000)
    {
        var id = Interlocked.Increment(ref _requestId);
        var tcs = new TaskCompletionSource<JsonElement>();
        _pendingRequests[id] = tcs;

        var msg = JsonSerializer.Serialize(new { request_id = id, command, @params = parameters });

        lock (_writeLock)
        {
            File.AppendAllText(ToQuikFile, msg + "\n");
        }

        var timeout = Task.Delay(timeoutMs);
        var completed = await Task.WhenAny(tcs.Task, timeout);
        if (completed == timeout)
        {
            _pendingRequests.TryRemove(id, out _);
            return default;
        }
        return await tcs.Task;
    }

    // === Маркетдата ===

    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        _candleCallbacks[ticker] = onCandle;
        await SendCommandAsync("subscribe_candles", new { class_code = GetClassCode(ticker), sec_code = ticker, interval = TfToQuik(timeframe) });
    }

    public async Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        _level2Callbacks[ticker] = onBidAsk;
        await SendCommandAsync("subscribe_orderbook", new { class_code = GetClassCode(ticker), sec_code = ticker });
    }

    public async Task SubscribeOrderBookAsync(string ticker, Action<OrderBookSnapshot> onOrderBook)
    {
        _orderBookCallbacks[ticker] = onOrderBook;
        await SendCommandAsync("subscribe_orderbook", new { class_code = GetClassCode(ticker), sec_code = ticker, depth = 20 });
    }

    public async Task SubscribeQuotesAsync(string ticker, Action<QuoteData> onQuote)
    {
        _quoteCallbacks[ticker] = onQuote;
        await SendCommandAsync("subscribe_quotes", new { class_code = GetClassCode(ticker), sec_code = ticker });
    }

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        if (!IsConnected && _fallback?.IsConnected == true)
            return await _fallback.GetHistoricalCandlesAsync(ticker, timeframe, from, to);

        var r = await SendCommandAsync("get_candles", new { class_code = GetClassCode(ticker), sec_code = ticker, interval = TfToQuik(timeframe), count = 200 });
        if (r.ValueKind != JsonValueKind.Undefined && r.TryGetProperty("candles", out var arr))
            return arr.EnumerateArray().Select(ParseCandle).Where(c => c.Timestamp >= from && c.Timestamp <= to).ToArray();
        return Array.Empty<Candle>();
    }

    // === Торговля ===

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        var r = await SendCommandAsync("place_order", new
        {
            class_code = GetClassCode(order.Ticker), sec_code = order.Ticker,
            action = order.Direction == SignalDirection.Buy ? "BUY" : "SELL",
            order_type = order.Type == OrderType.Market ? "MARKET" : "LIMIT",
            price = order.Price, quantity = order.Volume, comment = order.Comment
        });
        if (r.ValueKind != JsonValueKind.Undefined && r.TryGetProperty("order_id", out var oid))
        {
            order.BrokerOrderId = oid.GetString() ?? "";
            order.Status = OrderStatus.Active;
            _activeOrders[order.BrokerOrderId] = order;
        }
        OnOrderUpdate?.Invoke(order);
        return order;
    }

    public async Task<bool> CancelOrderAsync(string orderId)
    {
        var r = await SendCommandAsync("cancel_order", new { order_id = orderId });
        if (_activeOrders.TryRemove(orderId, out var order))
        {
            order.Status = OrderStatus.Cancelled;
            OnOrderUpdate?.Invoke(order);
        }
        return r.ValueKind != JsonValueKind.Undefined && r.TryGetProperty("success", out var s) && s.GetBoolean();
    }

    public Task<Order[]> GetActiveOrdersAsync() => Task.FromResult(_activeOrders.Values.ToArray());
    public Task<Position[]> GetPositionsAsync() => Task.FromResult(_positions.Values.ToArray());
    public Task<double> GetBalanceAsync() => Task.FromResult(_balance);

    // === Обработка сообщений ===

    private void ProcessMessage(JsonElement root)
    {
        var type = GS(root, "_type");
        switch (type)
        {
            case "quote": ProcessQuote(root); break;
            case "orderbook": ProcessOrderBook(root); break;
            case "trade":
                OnTrade?.Invoke(new Trade
                {
                    Ticker = GS(root, "sec_code"), Price = GD(root, "price"),
                    Volume = (int)GL(root, "quantity"),
                    Direction = GS(root, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
                    Timestamp = DateTime.Now, BrokerTradeId = GS(root, "trade_id")
                });
                break;
            case "candle":
                var ticker = GS(root, "sec_code");
                if (_candleCallbacks.TryGetValue(ticker, out var cb)) cb(ParseCandle(root));
                break;
            case "order": ProcessOrderUpdate(root); break;
            case "position": ProcessPositionUpdate(root); break;
            case "balance":
                if (root.TryGetProperty("value", out var bal)) _balance = bal.GetDouble();
                break;
            case "response":
                if (root.TryGetProperty("request_id", out var rid))
                    if (_pendingRequests.TryRemove(rid.GetInt32(), out var tcs)) tcs.SetResult(root);
                break;
        }
    }

    private void ProcessQuote(JsonElement e)
    {
        var t = GS(e, "sec_code");
        var q = new QuoteData
        {
            Ticker = t, Bid = GD(e, "bid"), Ask = GD(e, "ask"), Last = GD(e, "last"),
            Change = GD(e, "change"), ChangePercent = GD(e, "change_pct"),
            High = GD(e, "high"), Low = GD(e, "low"), Open = GD(e, "open"),
            PrevClose = GD(e, "prev_close"), Volume = GL(e, "volume"),
            OpenInterest = GL(e, "open_interest"), Time = DateTime.Now
        };
        _lastQuotes[t] = q;
        if (_quoteCallbacks.TryGetValue(t, out var cb)) cb(q);
        if (_level2Callbacks.TryGetValue(t, out var l2)) l2(q.Bid, q.Ask);
        OnQuoteUpdate?.Invoke(t, q);
    }

    private void ProcessOrderBook(JsonElement e)
    {
        var t = GS(e, "sec_code");
        var snap = new OrderBookSnapshot { Ticker = t, Time = DateTime.Now };
        var entries = new List<OrderBookEntry>();

        if (e.TryGetProperty("asks", out var asks))
            foreach (var a in asks.EnumerateArray())
                entries.Add(new OrderBookEntry { Price = a.GetProperty("price").GetDouble(), AskVolume = a.GetProperty("quantity").GetInt64() });
        if (e.TryGetProperty("bids", out var bids))
            foreach (var b in bids.EnumerateArray())
            {
                var p = b.GetProperty("price").GetDouble();
                var ex = entries.FirstOrDefault(x => Math.Abs(x.Price - p) < 0.001);
                if (ex != null) ex.BidVolume = b.GetProperty("quantity").GetInt64();
                else entries.Add(new OrderBookEntry { Price = p, BidVolume = b.GetProperty("quantity").GetInt64() });
            }

        snap.Entries = entries.OrderByDescending(x => x.Price).ToList();
        if (entries.Count > 0)
        {
            snap.BestBid = entries.Where(x => x.BidVolume > 0).MaxBy(x => x.Price)?.Price ?? 0;
            snap.BestAsk = entries.Where(x => x.AskVolume > 0).MinBy(x => x.Price)?.Price ?? 0;
            snap.LastPrice = _lastQuotes.TryGetValue(t, out var q) ? q.Last : (snap.BestBid + snap.BestAsk) / 2;
        }
        foreach (var o in _activeOrders.Values.Where(o => o.Ticker == t))
        {
            var en = snap.Entries.FirstOrDefault(x => Math.Abs(x.Price - o.Price) < 0.001);
            if (en != null) { en.OurOrderVolume = o.Volume; en.OurOrderDirection = o.Direction == SignalDirection.Buy ? "BUY" : "SELL"; }
        }
        if (_orderBookCallbacks.TryGetValue(t, out var cb)) cb(snap);
        OnOrderBookUpdate?.Invoke(t, snap);
    }

    private void ProcessOrderUpdate(JsonElement e)
    {
        var id = GS(e, "order_id");
        var order = new Order
        {
            BrokerOrderId = id, Ticker = GS(e, "sec_code"),
            Direction = GS(e, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Price = GD(e, "price"), Volume = (int)GL(e, "quantity"),
            FilledVolume = (int)GL(e, "filled"), Status = MapStatus(GS(e, "status"))
        };
        if (order.Status is OrderStatus.Active or OrderStatus.PartiallyFilled) _activeOrders[id] = order;
        else _activeOrders.TryRemove(id, out _);
        OnOrderUpdate?.Invoke(order);
    }

    private void ProcessPositionUpdate(JsonElement e)
    {
        var t = GS(e, "sec_code"); var vol = (int)GL(e, "current_net");
        if (vol == 0) _positions.TryRemove(t, out _);
        else _positions[t] = new Position
        {
            Ticker = t, Direction = vol > 0 ? SignalDirection.Buy : SignalDirection.Sell,
            Entries = new List<PositionEntry> { new() { Price = GD(e, "avg_price"), Volume = Math.Abs(vol) } }
        };
    }

    // === Fallback ===

    private async Task ResubscribeAll()
    {
        foreach (var t in _candleCallbacks.Keys) try { await SendCommandAsync("subscribe_candles", new { class_code = GetClassCode(t), sec_code = t, interval = 5 }); } catch { }
        foreach (var t in _level2Callbacks.Keys.Union(_orderBookCallbacks.Keys).Distinct()) try { await SendCommandAsync("subscribe_orderbook", new { class_code = GetClassCode(t), sec_code = t }); } catch { }
        foreach (var t in _quoteCallbacks.Keys) try { await SendCommandAsync("subscribe_quotes", new { class_code = GetClassCode(t), sec_code = t }); } catch { }
    }

    private void StartFallbackPolling()
    {
        if (_fallback == null || !_fallback.IsConnected || _useFallback) return;
        _useFallback = true; _fallbackCts?.Cancel(); _fallbackCts = new(); var ct = _fallbackCts.Token;
        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested && _useFallback)
            {
                try
                {
                    foreach (var (t, cb) in _candleCallbacks)
                    {
                        var c = await _fallback.GetHistoricalCandlesAsync(t, TimeSpan.FromMinutes(5), DateTime.UtcNow.AddMinutes(-10), DateTime.UtcNow);
                        if (c.Length > 0) cb(c[^1]);
                    }
                    _balance = await _fallback.GetBalanceAsync();
                }
                catch { }
                await Task.Delay(10_000, ct);
            }
        }, ct);
    }

    private void StopFallbackPolling() { _useFallback = false; _fallbackCts?.Cancel(); }

    // === Helpers ===

    private static Candle ParseCandle(JsonElement e) => new()
    {
        Timestamp = DateTime.TryParse(GS(e, "datetime"), out var dt) ? dt : DateTime.Now,
        Open = GD(e, "open"), High = GD(e, "high"), Low = GD(e, "low"), Close = GD(e, "close"), Volume = GL(e, "volume")
    };

    private static string GetClassCode(string t) =>
        (t.StartsWith("Si") || t.StartsWith("BR") || t.StartsWith("GD") || t.StartsWith("MX") || t.StartsWith("RI")) ? "SPBFUT" : "TQBR";

    private static int TfToQuik(TimeSpan tf) => (int)tf.TotalMinutes switch { 1 => 1, 5 => 5, 15 => 15, 30 => 30, 60 => 60, 1440 => 1440, _ => 5 };

    private static OrderStatus MapStatus(string s) => s switch
    { "active" => OrderStatus.Active, "filled" => OrderStatus.Filled, "partially_filled" => OrderStatus.PartiallyFilled, "cancelled" => OrderStatus.Cancelled, _ => OrderStatus.Pending };

    private static double GD(JsonElement e, string p) => e.TryGetProperty(p, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;
    private static long GL(JsonElement e, string p) => e.TryGetProperty(p, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : 0;
    private static string GS(JsonElement e, string p) => e.TryGetProperty(p, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    public void Dispose() { _cts?.Cancel(); _fallbackCts?.Cancel(); _fallback?.Dispose(); }
}
