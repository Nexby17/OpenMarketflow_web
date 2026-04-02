using System.Collections.Concurrent;
using System.Text.Json;
using HedgeFund.Core;
using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Quik;

/// <summary>
/// Коннектор к QUIK через файловый обмен (без внешних зависимостей в Lua).
/// 
/// Архитектура:
/// 1. Общая папка bridge/ рядом с Lua-скриптом
/// 2. QUIK пишет в bridge/outbox/*.json (стакан, котировки, сделки)
/// 3. Приложение пишет в bridge/inbox/*.json (команды)
/// 4. Атомарность: write .tmp → rename .json
/// 5. Heartbeat: QUIK пишет bridge/status/heartbeat.json каждые 5с
/// </summary>
public class QuikConnector : IBrokerConnector
{
    private CancellationTokenSource? _cts;

    private readonly string _bridgeDir;
    private string _outboxDir => Path.Combine(_bridgeDir, "outbox");
    private string _inboxDir => Path.Combine(_bridgeDir, "inbox");
    private string _statusDir => Path.Combine(_bridgeDir, "status");

    // Подписки
    private readonly ConcurrentDictionary<string, Action<Candle>> _candleCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<double, double>> _level2Callbacks = new();
    private readonly ConcurrentDictionary<string, Action<OrderBookSnapshot>> _orderBookCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<QuoteData>> _quoteCallbacks = new();

    // Кэш данных
    private readonly ConcurrentDictionary<string, QuoteData> _lastQuotes = new();
    private readonly ConcurrentDictionary<string, Order> _activeOrders = new();
    private readonly ConcurrentDictionary<string, Position> _positions = new();
    private double _balance;
    private int _requestId;

    // Ожидание ответов на команды
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pendingRequests = new();

    // Состояние
    private DateTime _lastHeartbeat;
    private bool _autoReconnect = true;

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

    // Дополнительные события
    public event Action<string, OrderBookSnapshot>? OnOrderBookUpdate;
    public event Action<string, QuoteData>? OnQuoteUpdate;
    public event Action<Trade>? OnAllTrades;

    /// <summary>
    /// bridgeDir — общая папка для обмена файлами с QUIK Lua-скриптом.
    /// По умолчанию: рядом с exe → bridge/
    /// finamToken — для fallback когда QUIK офлайн.
    /// </summary>
    public QuikConnector(string? bridgeDir = null, string? finamToken = null)
    {
        _bridgeDir = bridgeDir ?? Path.Combine(AppContext.BaseDirectory, "bridge");

        if (!string.IsNullOrEmpty(finamToken))
        {
            _fallbackToken = finamToken;
            _fallback = new Finam.FinamConnector();
        }
    }

    // === Подключение ===

    public async Task<bool> ConnectAsync(string login = "", string password = "")
    {
        if (!string.IsNullOrEmpty(login) && string.IsNullOrEmpty(_fallbackToken))
            _fallbackToken = login;

        // Создаём папки
        Directory.CreateDirectory(_outboxDir);
        Directory.CreateDirectory(_inboxDir);
        Directory.CreateDirectory(_statusDir);

        _cts = new CancellationTokenSource();
        _autoReconnect = true;

        // Запускаем polling outbox
        _ = Task.Run(() => PollLoop(_cts.Token), _cts.Token);

        // Запускаем мониторинг heartbeat
        _ = Task.Run(() => HeartbeatMonitor(_cts.Token), _cts.Token);

        // Fallback
        if (_fallback != null && !string.IsNullOrEmpty(_fallbackToken))
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    await _fallback.ConnectAsync(_fallbackToken);
                    OnError?.Invoke("🔄 Finam REST fallback подключён");
                }
                catch { }
            });
        }

        OnError?.Invoke($"📂 Ожидаю QUIK. Папка обмена: {_bridgeDir}");
        OnError?.Invoke("Запустите quik_bridge.lua в QUIK → появится bridge/status/heartbeat.json");

        return true;
    }

    public async Task DisconnectAsync()
    {
        _autoReconnect = false;
        _cts?.Cancel();
        _fallbackCts?.Cancel();
        _fallback?.Dispose();
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        await Task.CompletedTask;
    }

    // === Основной цикл чтения ===

    private async Task PollLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var files = Directory.GetFiles(_outboxDir, "*.json")
                    .OrderBy(f => f)
                    .ToArray();

                foreach (var file in files)
                {
                    if (ct.IsCancellationRequested) break;

                    try
                    {
                        var content = await File.ReadAllTextAsync(file, ct);
                        File.Delete(file);

                        if (!string.IsNullOrWhiteSpace(content))
                        {
                            var doc = JsonDocument.Parse(content);
                            ProcessMessage(doc.RootElement);
                        }
                    }
                    catch (IOException)
                    {
                        // Файл ещё пишется, пропускаем
                        await Task.Delay(10, ct);
                    }
                    catch (JsonException ex)
                    {
                        OnError?.Invoke($"JSON ошибка: {ex.Message}");
                        try { File.Delete(file); } catch { }
                    }
                }
            }
            catch (DirectoryNotFoundException)
            {
                Directory.CreateDirectory(_outboxDir);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                OnError?.Invoke($"Poll ошибка: {ex.Message}");
            }

            await Task.Delay(50, ct); // 50мс — быстрый polling
        }
    }

    // === Мониторинг heartbeat ===

    private async Task HeartbeatMonitor(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var hbFile = Path.Combine(_statusDir, "heartbeat.json");
                bool quikAlive = false;

                if (File.Exists(hbFile))
                {
                    var info = new FileInfo(hbFile);
                    var age = DateTime.Now - info.LastWriteTime;

                    if (age.TotalSeconds < 15) // heartbeat свежий (QUIK пишет каждые 5с)
                    {
                        quikAlive = true;
                        _lastHeartbeat = info.LastWriteTime;
                    }
                }

                if (quikAlive && !IsConnected)
                {
                    IsConnected = true;
                    _useFallback = false;
                    StopFallbackPolling();
                    OnConnectionChanged?.Invoke(true);
                    OnError?.Invoke("✅ QUIK онлайн — реалтайм данные активны");

                    // Отправляем подписки
                    await ResubscribeAll();
                }
                else if (!quikAlive && IsConnected)
                {
                    IsConnected = false;
                    OnConnectionChanged?.Invoke(false);
                    OnError?.Invoke("⚠️ QUIK офлайн — переключаю на Finam REST");
                    StartFallbackPolling();
                }
                else if (!quikAlive && !IsConnected && !_useFallback)
                {
                    StartFallbackPolling();
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                OnError?.Invoke($"Heartbeat ошибка: {ex.Message}");
            }

            await Task.Delay(3000, ct); // проверяем каждые 3с
        }
    }

    // === Маркетдата ===

    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        _candleCallbacks[ticker] = onCandle;
        await SendCommandAsync("subscribe_candles", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker,
            interval = TimeframeToQuikInterval(timeframe)
        });
    }

    public async Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        _level2Callbacks[ticker] = onBidAsk;
        await SendCommandAsync("subscribe_orderbook", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker
        });
    }

    public async Task SubscribeOrderBookAsync(string ticker, Action<OrderBookSnapshot> onOrderBook)
    {
        _orderBookCallbacks[ticker] = onOrderBook;
        await SendCommandAsync("subscribe_orderbook", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker,
            depth = 20
        });
    }

    public async Task SubscribeQuotesAsync(string ticker, Action<QuoteData> onQuote)
    {
        _quoteCallbacks[ticker] = onQuote;
        await SendCommandAsync("subscribe_quotes", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker
        });
    }

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        // Если QUIK офлайн — через fallback
        if (!IsConnected && _fallback?.IsConnected == true)
            return await _fallback.GetHistoricalCandlesAsync(ticker, timeframe, from, to);

        var response = await SendCommandAsync("get_candles", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker,
            interval = TimeframeToQuikInterval(timeframe),
            count = 200
        });

        if (response.TryGetProperty("candles", out var arr))
        {
            return arr.EnumerateArray()
                .Select(ParseCandle)
                .Where(c => c.Timestamp >= from && c.Timestamp <= to)
                .ToArray();
        }

        return Array.Empty<Candle>();
    }

    // === Торговля ===

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        var response = await SendCommandAsync("place_order", new
        {
            class_code = GetClassCode(order.Ticker),
            sec_code = order.Ticker,
            action = order.Direction == SignalDirection.Buy ? "BUY" : "SELL",
            order_type = order.Type == OrderType.Market ? "MARKET" : "LIMIT",
            price = order.Price,
            quantity = order.Volume,
            comment = order.Comment
        });

        if (response.TryGetProperty("order_id", out var oid))
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
        var response = await SendCommandAsync("cancel_order", new { order_id = orderId });

        if (_activeOrders.TryRemove(orderId, out var order))
        {
            order.Status = OrderStatus.Cancelled;
            OnOrderUpdate?.Invoke(order);
        }

        return response.TryGetProperty("success", out var s) && s.GetBoolean();
    }

    public Task<Order[]> GetActiveOrdersAsync() => Task.FromResult(_activeOrders.Values.ToArray());
    public Task<Position[]> GetPositionsAsync() => Task.FromResult(_positions.Values.ToArray());
    public Task<double> GetBalanceAsync() => Task.FromResult(_balance);

    // === Отправка команд (файл в inbox) ===

    private async Task<JsonElement> SendCommandAsync(string command, object parameters, int timeoutMs = 5000)
    {
        var id = Interlocked.Increment(ref _requestId);
        var tcs = new TaskCompletionSource<JsonElement>();
        _pendingRequests[id] = tcs;

        var msg = new { request_id = id, command, @params = parameters };
        var json = JsonSerializer.Serialize(msg);

        // Атомарная запись: .tmp → .json
        var filename = $"{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}_{id}_{command}.json";
        var tmpPath = Path.Combine(_inboxDir, filename + ".tmp");
        var finalPath = Path.Combine(_inboxDir, filename);

        await File.WriteAllTextAsync(tmpPath, json);
        File.Move(tmpPath, finalPath);

        // Ждём ответ
        var timeout = Task.Delay(timeoutMs);
        var completed = await Task.WhenAny(tcs.Task, timeout);

        if (completed == timeout)
        {
            _pendingRequests.TryRemove(id, out _);
            // Не бросаем исключение — команда может быть fire-and-forget
            return default;
        }

        return await tcs.Task;
    }

    // === Обработка сообщений от QUIK ===

    private void ProcessMessage(JsonElement root)
    {
        var type = GetString(root, "_type");

        switch (type)
        {
            case "quote": ProcessQuote(root); break;
            case "orderbook": ProcessOrderBook(root); break;
            case "trade": ProcessTrade(root); break;
            case "allTrades": ProcessAllTrades(root); break;
            case "candle": ProcessCandleUpdate(root); break;
            case "order": ProcessOrderUpdate(root); break;
            case "position": ProcessPositionUpdate(root); break;
            case "balance":
                if (root.TryGetProperty("value", out var bal))
                    _balance = bal.GetDouble();
                break;
            case "response": ProcessResponse(root); break;
        }
    }

    private void ProcessQuote(JsonElement e)
    {
        var ticker = GetString(e, "sec_code");
        var quote = new QuoteData
        {
            Ticker = ticker,
            Bid = GetDouble(e, "bid"), Ask = GetDouble(e, "ask"), Last = GetDouble(e, "last"),
            Change = GetDouble(e, "change"), ChangePercent = GetDouble(e, "change_pct"),
            High = GetDouble(e, "high"), Low = GetDouble(e, "low"),
            Open = GetDouble(e, "open"), PrevClose = GetDouble(e, "prev_close"),
            Volume = GetLong(e, "volume"), OpenInterest = GetLong(e, "open_interest"),
            Time = DateTime.Now
        };

        _lastQuotes[ticker] = quote;
        if (_quoteCallbacks.TryGetValue(ticker, out var cb)) cb(quote);
        if (_level2Callbacks.TryGetValue(ticker, out var l2)) l2(quote.Bid, quote.Ask);
        OnQuoteUpdate?.Invoke(ticker, quote);
    }

    private void ProcessOrderBook(JsonElement e)
    {
        var ticker = GetString(e, "sec_code");
        var snapshot = new OrderBookSnapshot { Ticker = ticker, Time = DateTime.Now };
        var entries = new List<OrderBookEntry>();

        if (e.TryGetProperty("asks", out var asks))
            foreach (var ask in asks.EnumerateArray())
                entries.Add(new OrderBookEntry { Price = ask.GetProperty("price").GetDouble(), AskVolume = ask.GetProperty("quantity").GetInt64() });

        if (e.TryGetProperty("bids", out var bids))
            foreach (var bid in bids.EnumerateArray())
            {
                var price = bid.GetProperty("price").GetDouble();
                var existing = entries.FirstOrDefault(x => Math.Abs(x.Price - price) < 0.001);
                if (existing != null) existing.BidVolume = bid.GetProperty("quantity").GetInt64();
                else entries.Add(new OrderBookEntry { Price = price, BidVolume = bid.GetProperty("quantity").GetInt64() });
            }

        snapshot.Entries = entries.OrderByDescending(x => x.Price).ToList();
        if (entries.Count > 0)
        {
            snapshot.BestBid = entries.Where(x => x.BidVolume > 0).MaxBy(x => x.Price)?.Price ?? 0;
            snapshot.BestAsk = entries.Where(x => x.AskVolume > 0).MinBy(x => x.Price)?.Price ?? 0;
            snapshot.LastPrice = _lastQuotes.TryGetValue(ticker, out var q) ? q.Last : (snapshot.BestBid + snapshot.BestAsk) / 2;
        }

        // Помечаем наши ордера
        foreach (var order in _activeOrders.Values.Where(o => o.Ticker == ticker))
        {
            var entry = snapshot.Entries.FirstOrDefault(x => Math.Abs(x.Price - order.Price) < 0.001);
            if (entry != null)
            {
                entry.OurOrderVolume = order.Volume;
                entry.OurOrderDirection = order.Direction == SignalDirection.Buy ? "BUY" : "SELL";
            }
        }

        if (_orderBookCallbacks.TryGetValue(ticker, out var cb)) cb(snapshot);
        OnOrderBookUpdate?.Invoke(ticker, snapshot);
    }

    private void ProcessTrade(JsonElement e)
    {
        var trade = new Trade
        {
            Ticker = GetString(e, "sec_code"), Price = GetDouble(e, "price"),
            Volume = (int)GetLong(e, "quantity"),
            Direction = GetString(e, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Timestamp = DateTime.Now, BrokerTradeId = GetString(e, "trade_id")
        };
        OnTrade?.Invoke(trade);
    }

    private void ProcessAllTrades(JsonElement e)
    {
        var trade = new Trade
        {
            Ticker = GetString(e, "sec_code"), Price = GetDouble(e, "price"),
            Volume = (int)GetLong(e, "quantity"),
            Direction = GetString(e, "flags") == "1" ? SignalDirection.Sell : SignalDirection.Buy,
            Timestamp = DateTime.Now, BrokerTradeId = GetString(e, "trade_num")
        };
        OnAllTrades?.Invoke(trade);
    }

    private void ProcessCandleUpdate(JsonElement e)
    {
        var ticker = GetString(e, "sec_code");
        var candle = ParseCandle(e);
        if (_candleCallbacks.TryGetValue(ticker, out var cb)) cb(candle);
    }

    private void ProcessOrderUpdate(JsonElement e)
    {
        var orderId = GetString(e, "order_id");
        var order = new Order
        {
            BrokerOrderId = orderId, Ticker = GetString(e, "sec_code"),
            Direction = GetString(e, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Price = GetDouble(e, "price"), Volume = (int)GetLong(e, "quantity"),
            FilledVolume = (int)GetLong(e, "filled"), Status = MapOrderStatus(GetString(e, "status"))
        };

        if (order.Status is OrderStatus.Active or OrderStatus.PartiallyFilled)
            _activeOrders[orderId] = order;
        else
            _activeOrders.TryRemove(orderId, out _);

        OnOrderUpdate?.Invoke(order);
    }

    private void ProcessPositionUpdate(JsonElement e)
    {
        var ticker = GetString(e, "sec_code");
        var volume = (int)GetLong(e, "current_net");

        if (volume == 0)
            _positions.TryRemove(ticker, out _);
        else
            _positions[ticker] = new Position
            {
                Ticker = ticker,
                Direction = volume > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                Entries = new List<PositionEntry> { new() { Price = GetDouble(e, "avg_price"), Volume = Math.Abs(volume), Comment = "QUIK" } }
            };
    }

    private void ProcessResponse(JsonElement e)
    {
        if (e.TryGetProperty("request_id", out var rid))
        {
            int id = rid.GetInt32();
            if (_pendingRequests.TryRemove(id, out var tcs))
                tcs.SetResult(e);
        }
    }

    // === Fallback ===

    private async Task ResubscribeAll()
    {
        foreach (var ticker in _candleCallbacks.Keys)
            try { await SendCommandAsync("subscribe_candles", new { class_code = GetClassCode(ticker), sec_code = ticker, interval = 5 }); } catch { }
        foreach (var ticker in _level2Callbacks.Keys.Union(_orderBookCallbacks.Keys).Distinct())
            try { await SendCommandAsync("subscribe_orderbook", new { class_code = GetClassCode(ticker), sec_code = ticker, depth = 20 }); } catch { }
        foreach (var ticker in _quoteCallbacks.Keys)
            try { await SendCommandAsync("subscribe_quotes", new { class_code = GetClassCode(ticker), sec_code = ticker }); } catch { }

        OnError?.Invoke($"🔄 Подписки восстановлены");
    }

    private void StartFallbackPolling()
    {
        if (_fallback == null || !_fallback.IsConnected || _useFallback) return;
        _useFallback = true;
        _fallbackCts?.Cancel();
        _fallbackCts = new CancellationTokenSource();
        var ct = _fallbackCts.Token;

        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested && _useFallback)
            {
                try
                {
                    foreach (var (ticker, cb) in _candleCallbacks)
                    {
                        var candles = await _fallback.GetHistoricalCandlesAsync(ticker, TimeSpan.FromMinutes(5), DateTime.UtcNow.AddMinutes(-10), DateTime.UtcNow);
                        if (candles.Length > 0) cb(candles[^1]);
                    }
                    _balance = await _fallback.GetBalanceAsync();
                }
                catch { }
                await Task.Delay(10_000, ct);
            }
        }, ct);
    }

    private void StopFallbackPolling()
    {
        _useFallback = false;
        _fallbackCts?.Cancel();
    }

    // === Helpers ===

    private static Candle ParseCandle(JsonElement e) => new()
    {
        Timestamp = DateTime.TryParse(GetString(e, "datetime"), out var dt) ? dt : DateTime.Now,
        Open = GetDouble(e, "open"), High = GetDouble(e, "high"),
        Low = GetDouble(e, "low"), Close = GetDouble(e, "close"), Volume = GetLong(e, "volume")
    };

    private static string GetClassCode(string ticker)
    {
        if (ticker.StartsWith("Si") || ticker.StartsWith("BR") || ticker.StartsWith("GD") ||
            ticker.StartsWith("MX") || ticker.StartsWith("RI") || ticker.StartsWith("CR") ||
            ticker.StartsWith("ED") || ticker.StartsWith("Eu"))
            return "SPBFUT";
        return "TQBR";
    }

    private static int TimeframeToQuikInterval(TimeSpan tf) => (int)tf.TotalMinutes switch
    {
        1 => 1, 5 => 5, 15 => 15, 30 => 30, 60 => 60, 1440 => 1440, _ => 5
    };

    private static OrderStatus MapOrderStatus(string s) => s switch
    {
        "active" => OrderStatus.Active, "filled" => OrderStatus.Filled,
        "partially_filled" => OrderStatus.PartiallyFilled,
        "cancelled" => OrderStatus.Cancelled, "rejected" => OrderStatus.Rejected,
        _ => OrderStatus.Pending
    };

    private static double GetDouble(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;
    private static long GetLong(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : 0;
    private static string GetString(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    public void Dispose()
    {
        _autoReconnect = false;
        _cts?.Cancel();
        _fallbackCts?.Cancel();
        _fallback?.Dispose();
    }
}
