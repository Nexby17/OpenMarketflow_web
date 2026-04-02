using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using HedgeFund.Core;
using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Quik;

/// <summary>
/// Коннектор к QUIK через TCP + Lua скрипт.
/// 
/// Архитектура:
/// 1. Наше приложение поднимает TCP-сервер (по умолчанию порт 34130)
/// 2. В QUIK запускается Lua-скрипт (quik_bridge.lua), который:
///    - Подключается к нашему TCP-серверу
///    - Стримит стакан, сделки, свечи, позиции, ордера
///    - Принимает команды на выставление/отмену заявок
/// 3. Обмен JSON-сообщениями по TCP
/// 
/// Формат сообщения: {type}|{json}\n
/// Типы: quote, orderbook, trade, candle, order, position, balance, allTrades, response
/// </summary>
public class QuikConnector : IBrokerConnector
{
    private TcpListener? _server;
    private TcpClient? _client;
    private NetworkStream? _stream;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private CancellationTokenSource? _cts;
    private readonly object _lock = new();

    private readonly int _port;
    private readonly string _host;

    // Реконнект
    private bool _autoReconnect = true;
    private int _reconnectAttempts;
    private DateTime _lastPingReceived;
    private DateTime _lastDataReceived;
    private const int PING_TIMEOUT_SEC = 30;
    private const int RECONNECT_DELAY_MS = 5000;
    private const int MAX_RECONNECT_ATTEMPTS = 0; // 0 = бесконечно

    // Fallback: Finam REST API для данных когда QUIK офлайн
    private readonly Finam.FinamConnector? _fallback;
    private bool _useFallback;
    private string _fallbackToken = string.Empty;

    // Подписки
    private readonly ConcurrentDictionary<string, Action<Candle>> _candleCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<double, double>> _level2Callbacks = new();
    private readonly ConcurrentDictionary<string, Action<OrderBookSnapshot>> _orderBookCallbacks = new();
    private readonly ConcurrentDictionary<string, Action<QuoteData>> _quoteCallbacks = new();

    // Кэш данных
    private readonly ConcurrentDictionary<string, QuoteData> _lastQuotes = new();
    private readonly ConcurrentDictionary<string, OrderBookSnapshot> _lastOrderBooks = new();
    private readonly ConcurrentDictionary<string, Order> _activeOrders = new();
    private readonly ConcurrentDictionary<string, Position> _positions = new();
    private double _balance;
    private int _requestId;

    // Ожидание ответов на команды
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pendingRequests = new();

    public string BrokerName => "QUIK (Финам)";
    public bool IsConnected { get; private set; }

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    // Дополнительные события для UI
    public event Action<string, OrderBookSnapshot>? OnOrderBookUpdate;
    public event Action<string, QuoteData>? OnQuoteUpdate;
    public event Action<Trade>? OnAllTrades; // лента всех сделок

    /// <summary>
    /// host/port — TCP сервер для Lua.
    /// finamToken — токен Finam REST API для fallback (опционально).
    /// </summary>
    public QuikConnector(string host = "0.0.0.0", int port = 34130, string? finamToken = null)
    {
        _host = host;
        _port = port;

        // Fallback на Finam REST API когда QUIK недоступен
        if (!string.IsNullOrEmpty(finamToken))
        {
            _fallbackToken = finamToken;
            _fallback = new Finam.FinamConnector();
        }
    }

    // === Подключение ===

    /// <summary>
    /// Запускает TCP-сервер и ждёт подключения QUIK Lua-скрипта.
    /// login = не используется (QUIK авторизуется сам)
    /// password = не используется
    /// </summary>
    public async Task<bool> ConnectAsync(string login = "", string password = "")
    {
        // Сохраняем токен если передан (fallback)
        if (!string.IsNullOrEmpty(login) && string.IsNullOrEmpty(_fallbackToken))
            _fallbackToken = login;

        _cts = new CancellationTokenSource();
        _autoReconnect = true;

        // Запускаем TCP сервер + цикл реконнекта
        _ = Task.Run(() => AcceptLoop(_cts.Token), _cts.Token);

        // Подключаем fallback если есть токен
        if (_fallback != null && !string.IsNullOrEmpty(_fallbackToken))
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    await _fallback.ConnectAsync(_fallbackToken);
                    OnError?.Invoke("🔄 Finam REST fallback подключён (для данных когда QUIK офлайн)");
                }
                catch { }
            });
        }

        return true;
    }

    /// <summary>Цикл приёма подключений с авто-реконнектом</summary>
    private async Task AcceptLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _autoReconnect)
        {
            try
            {
                // Запускаем/перезапускаем TCP сервер
                CleanupConnection();
                _server?.Stop();
                _server = new TcpListener(IPAddress.Parse(_host), _port);
                _server.Start();

                OnError?.Invoke($"📡 TCP-сервер на {_host}:{_port}. Ожидаю QUIK...");
                SwitchToFallback(true);

                // Ждём подключение (бесконечно)
                _client = await _server.AcceptTcpClientAsync(ct);
                _client.ReceiveTimeout = PING_TIMEOUT_SEC * 1000;
                _client.SendTimeout = 5000;
                _stream = _client.GetStream();
                _reader = new StreamReader(_stream, Encoding.UTF8);
                _writer = new StreamWriter(_stream, Encoding.UTF8) { AutoFlush = true };

                IsConnected = true;
                _useFallback = false;
                _reconnectAttempts = 0;
                _lastPingReceived = DateTime.UtcNow;
                _lastDataReceived = DateTime.UtcNow;
                OnConnectionChanged?.Invoke(true);
                OnError?.Invoke("✅ QUIK подключён! Реалтайм данные активны.");

                // Запрашиваем начальное состояние
                try
                {
                    await SendCommandAsync("get_balance", new { });
                    await SendCommandAsync("get_positions", new { });
                    await SendCommandAsync("get_orders", new { });
                }
                catch { }

                // Переподписываем на все предыдущие подписки
                await ResubscribeAll();

                // Читаем сообщения пока соединение живо
                await ReadLoop(ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                _reconnectAttempts++;
                OnError?.Invoke($"⚠️ QUIK отключён: {ex.Message}. Переподключение #{_reconnectAttempts}...");
            }

            // Переключаемся на fallback
            IsConnected = false;
            OnConnectionChanged?.Invoke(false);
            SwitchToFallback(true);

            if (MAX_RECONNECT_ATTEMPTS > 0 && _reconnectAttempts >= MAX_RECONNECT_ATTEMPTS)
            {
                OnError?.Invoke("❌ Превышен лимит реконнектов");
                break;
            }

            // Пауза перед реконнектом
            try { await Task.Delay(RECONNECT_DELAY_MS, ct); }
            catch { break; }
        }
    }

    /// <summary>Переподписаться на все активные подписки после реконнекта</summary>
    private async Task ResubscribeAll()
    {
        foreach (var ticker in _candleCallbacks.Keys)
        {
            try
            {
                await SendCommandAsync("subscribe_candles", new
                {
                    class_code = GetClassCode(ticker),
                    sec_code = ticker,
                    interval = 5 // дефолтный, можно хранить в словаре
                });
            }
            catch { }
        }

        foreach (var ticker in _level2Callbacks.Keys.Union(_orderBookCallbacks.Keys))
        {
            try
            {
                await SendCommandAsync("subscribe_orderbook", new
                {
                    class_code = GetClassCode(ticker),
                    sec_code = ticker,
                    depth = 20
                });
            }
            catch { }
        }

        foreach (var ticker in _quoteCallbacks.Keys)
        {
            try
            {
                await SendCommandAsync("subscribe_quotes", new
                {
                    class_code = GetClassCode(ticker),
                    sec_code = ticker
                });
            }
            catch { }
        }

        OnError?.Invoke($"🔄 Подписки восстановлены: {_candleCallbacks.Count} свечи, {_orderBookCallbacks.Count} стакан, {_quoteCallbacks.Count} котировки");
    }

    /// <summary>Переключиться на fallback / вернуться на QUIK</summary>
    private void SwitchToFallback(bool useFallback)
    {
        if (_fallback == null) return;
        if (_useFallback == useFallback) return;

        _useFallback = useFallback;
        if (useFallback)
        {
            OnError?.Invoke("🔄 Переключено на Finam REST API (данные с задержкой, без стакана)");
            StartFallbackPolling();
        }
        else
        {
            OnError?.Invoke("✅ QUIK онлайн — реалтайм данные восстановлены");
            StopFallbackPolling();
        }
    }

    private CancellationTokenSource? _fallbackCts;

    private void StartFallbackPolling()
    {
        if (_fallback == null || !_fallback.IsConnected) return;
        _fallbackCts?.Cancel();
        _fallbackCts = new CancellationTokenSource();
        var ct = _fallbackCts.Token;

        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested && _useFallback)
            {
                try
                {
                    // Поллим свечи для подписанных тикеров
                    foreach (var (ticker, cb) in _candleCallbacks)
                    {
                        var candles = await _fallback.GetHistoricalCandlesAsync(
                            ticker, TimeSpan.FromMinutes(5),
                            DateTime.UtcNow.AddMinutes(-10), DateTime.UtcNow);
                        if (candles.Length > 0)
                            cb(candles[^1]); // последняя свеча
                    }

                    // Позиции и баланс
                    _balance = await _fallback.GetBalanceAsync();
                    var positions = await _fallback.GetPositionsAsync();
                    foreach (var p in positions)
                        _positions[p.Ticker] = p;

                    // Ордера
                    var orders = await _fallback.GetActiveOrdersAsync();
                    _activeOrders.Clear();
                    foreach (var o in orders)
                        _activeOrders[o.BrokerOrderId] = o;
                }
                catch (Exception ex)
                {
                    OnError?.Invoke($"Fallback polling ошибка: {ex.Message}");
                }

                await Task.Delay(10_000, ct); // каждые 10 сек
            }
        }, ct);
    }

    private void StopFallbackPolling()
    {
        _fallbackCts?.Cancel();
        _fallbackCts = null;
    }

    private void CleanupConnection()
    {
        _reader?.Dispose(); _reader = null;
        _writer?.Dispose(); _writer = null;
        _stream?.Dispose(); _stream = null;
        _client?.Dispose(); _client = null;
    }

    public async Task DisconnectAsync()
    {
        _autoReconnect = false;
        _cts?.Cancel();
        _fallbackCts?.Cancel();
        
        try
        {
            if (_writer != null)
                await SendRawAsync("disconnect|{}");
        }
        catch { }

        CleanupConnection();
        _server?.Stop();
        _fallback?.Dispose();

        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
    }

    // === Маркетдата ===

    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        _candleCallbacks[ticker] = onCandle;

        int intervalSec = (int)timeframe.TotalSeconds;
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

    /// <summary>Подписаться на полный стакан (для вкладки Стакан)</summary>
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

    /// <summary>Подписаться на котировки</summary>
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
        var response = await SendCommandAsync("get_candles", new
        {
            class_code = GetClassCode(ticker),
            sec_code = ticker,
            interval = TimeframeToQuikInterval(timeframe),
            count = 200
        });

        if (response.TryGetProperty("candles", out var candlesArr))
        {
            return candlesArr.EnumerateArray()
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
        var response = await SendCommandAsync("cancel_order", new
        {
            order_id = orderId
        });

        if (_activeOrders.TryRemove(orderId, out var order))
        {
            order.Status = OrderStatus.Cancelled;
            OnOrderUpdate?.Invoke(order);
        }

        return response.TryGetProperty("success", out var s) && s.GetBoolean();
    }

    public Task<Order[]> GetActiveOrdersAsync()
    {
        return Task.FromResult(_activeOrders.Values.ToArray());
    }

    // === Позиции ===

    public Task<Position[]> GetPositionsAsync()
    {
        return Task.FromResult(_positions.Values.ToArray());
    }

    public Task<double> GetBalanceAsync()
    {
        return Task.FromResult(_balance);
    }

    // === Чтение сообщений от QUIK ===

    private async Task ReadLoop(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested && _reader != null)
            {
                var line = await _reader.ReadLineAsync(ct);
                if (line == null)
                {
                    OnError?.Invoke("⚠️ QUIK: соединение закрыто (EOF)");
                    break; // выйдем в AcceptLoop → реконнект
                }

                _lastDataReceived = DateTime.UtcNow;

                try
                {
                    ProcessMessage(line);
                }
                catch (Exception ex)
                {
                    OnError?.Invoke($"Ошибка обработки сообщения: {ex.Message}");
                }
            }
        }
        catch (OperationCanceledException) { }
        catch (IOException ex)
        {
            OnError?.Invoke($"🔌 QUIK TCP разрыв: {ex.Message}");
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"🔌 QUIK отключён: {ex.Message}");
        }
        finally
        {
            IsConnected = false;
            OnConnectionChanged?.Invoke(false);
            CleanupConnection();
        }
    }

    private void ProcessMessage(string raw)
    {
        // Формат: type|json
        var pipeIdx = raw.IndexOf('|');
        if (pipeIdx < 0) return;

        var type = raw[..pipeIdx];
        var json = raw[(pipeIdx + 1)..];
        var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        switch (type)
        {
            case "quote":
                ProcessQuote(root);
                break;

            case "orderbook":
                ProcessOrderBook(root);
                break;

            case "trade":
                ProcessTrade(root);
                break;

            case "allTrades":
                ProcessAllTrades(root);
                break;

            case "candle":
                ProcessCandleUpdate(root);
                break;

            case "order":
                ProcessOrderUpdate(root);
                break;

            case "position":
                ProcessPositionUpdate(root);
                break;

            case "balance":
                if (root.TryGetProperty("value", out var bal))
                    _balance = bal.GetDouble();
                break;

            case "response":
                ProcessResponse(root);
                break;

            case "ping":
                _ = SendRawAsync("pong|{}");
                break;
        }
    }

    private void ProcessQuote(JsonElement e)
    {
        var ticker = e.GetProperty("sec_code").GetString() ?? "";
        var quote = new QuoteData
        {
            Ticker = ticker,
            Bid = GetDouble(e, "bid"),
            Ask = GetDouble(e, "ask"),
            Last = GetDouble(e, "last"),
            Change = GetDouble(e, "change"),
            ChangePercent = GetDouble(e, "change_pct"),
            High = GetDouble(e, "high"),
            Low = GetDouble(e, "low"),
            Open = GetDouble(e, "open"),
            PrevClose = GetDouble(e, "prev_close"),
            Volume = GetLong(e, "volume"),
            OpenInterest = GetLong(e, "open_interest"),
            Time = DateTime.Now
        };

        _lastQuotes[ticker] = quote;

        if (_quoteCallbacks.TryGetValue(ticker, out var cb))
            cb(quote);

        if (_level2Callbacks.TryGetValue(ticker, out var l2cb))
            l2cb(quote.Bid, quote.Ask);

        OnQuoteUpdate?.Invoke(ticker, quote);
    }

    private void ProcessOrderBook(JsonElement e)
    {
        var ticker = e.GetProperty("sec_code").GetString() ?? "";
        var snapshot = new OrderBookSnapshot
        {
            Ticker = ticker,
            Time = DateTime.Now
        };

        if (e.TryGetProperty("bids", out var bids) && e.TryGetProperty("asks", out var asks))
        {
            var entries = new List<OrderBookEntry>();

            foreach (var ask in asks.EnumerateArray())
            {
                entries.Add(new OrderBookEntry
                {
                    Price = ask.GetProperty("price").GetDouble(),
                    AskVolume = ask.GetProperty("quantity").GetInt64(),
                    BidVolume = 0
                });
            }

            foreach (var bid in bids.EnumerateArray())
            {
                var price = bid.GetProperty("price").GetDouble();
                var existing = entries.FirstOrDefault(x => Math.Abs(x.Price - price) < 0.001);
                if (existing != null)
                {
                    existing.BidVolume = bid.GetProperty("quantity").GetInt64();
                }
                else
                {
                    entries.Add(new OrderBookEntry
                    {
                        Price = price,
                        BidVolume = bid.GetProperty("quantity").GetInt64(),
                        AskVolume = 0
                    });
                }
            }

            snapshot.Entries = entries.OrderByDescending(x => x.Price).ToList();

            if (snapshot.Entries.Count > 0)
            {
                var bestBid = entries.Where(x => x.BidVolume > 0).MaxBy(x => x.Price);
                var bestAsk = entries.Where(x => x.AskVolume > 0).MinBy(x => x.Price);
                snapshot.BestBid = bestBid?.Price ?? 0;
                snapshot.BestAsk = bestAsk?.Price ?? 0;
                snapshot.LastPrice = _lastQuotes.TryGetValue(ticker, out var q) ? q.Last : (snapshot.BestBid + snapshot.BestAsk) / 2;
            }

            // Отмечаем наши ордера в стакане
            foreach (var order in _activeOrders.Values.Where(o => o.Ticker == ticker))
            {
                var entry = snapshot.Entries.FirstOrDefault(x => Math.Abs(x.Price - order.Price) < 0.001);
                if (entry != null)
                {
                    entry.OurOrderVolume = order.Volume;
                    entry.OurOrderDirection = order.Direction == SignalDirection.Buy ? "BUY" : "SELL";
                }
            }
        }

        _lastOrderBooks[ticker] = snapshot;

        if (_orderBookCallbacks.TryGetValue(ticker, out var cb))
            cb(snapshot);

        OnOrderBookUpdate?.Invoke(ticker, snapshot);
    }

    private void ProcessTrade(JsonElement e)
    {
        var trade = new Trade
        {
            Ticker = e.GetProperty("sec_code").GetString() ?? "",
            Price = GetDouble(e, "price"),
            Volume = (int)GetLong(e, "quantity"),
            Direction = GetString(e, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Timestamp = DateTime.Now,
            BrokerTradeId = GetString(e, "trade_id")
        };

        OnTrade?.Invoke(trade);
    }

    private void ProcessAllTrades(JsonElement e)
    {
        var trade = new Trade
        {
            Ticker = e.GetProperty("sec_code").GetString() ?? "",
            Price = GetDouble(e, "price"),
            Volume = (int)GetLong(e, "quantity"),
            Direction = GetString(e, "flags") == "1" ? SignalDirection.Sell : SignalDirection.Buy,
            Timestamp = DateTime.Now,
            BrokerTradeId = GetString(e, "trade_num")
        };

        OnAllTrades?.Invoke(trade);
    }

    private void ProcessCandleUpdate(JsonElement e)
    {
        var ticker = e.GetProperty("sec_code").GetString() ?? "";
        var candle = ParseCandle(e);

        if (_candleCallbacks.TryGetValue(ticker, out var cb))
            cb(candle);
    }

    private void ProcessOrderUpdate(JsonElement e)
    {
        var orderId = GetString(e, "order_id");
        var status = GetString(e, "status");

        var order = new Order
        {
            BrokerOrderId = orderId,
            Ticker = GetString(e, "sec_code"),
            Direction = GetString(e, "direction") == "BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Price = GetDouble(e, "price"),
            Volume = (int)GetLong(e, "quantity"),
            FilledVolume = (int)GetLong(e, "filled"),
            Status = MapOrderStatus(status)
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
        {
            _positions.TryRemove(ticker, out _);
        }
        else
        {
            _positions[ticker] = new Position
            {
                Ticker = ticker,
                Direction = volume > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                Entries = new List<PositionEntry>
                {
                    new()
                    {
                        Price = GetDouble(e, "avg_price"),
                        Volume = Math.Abs(volume),
                        Comment = "QUIK позиция"
                    }
                }
            };
        }
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

    // === Отправка команд в QUIK ===

    private async Task<JsonElement> SendCommandAsync(string command, object parameters, int timeoutMs = 5000)
    {
        var id = Interlocked.Increment(ref _requestId);
        var tcs = new TaskCompletionSource<JsonElement>();
        _pendingRequests[id] = tcs;

        var msg = new
        {
            request_id = id,
            command,
            @params = parameters
        };

        await SendRawAsync($"command|{JsonSerializer.Serialize(msg)}");

        // Ждём ответ с таймаутом
        var timeout = Task.Delay(timeoutMs);
        var completed = await Task.WhenAny(tcs.Task, timeout);

        if (completed == timeout)
        {
            _pendingRequests.TryRemove(id, out _);
            throw new TimeoutException($"QUIK не ответил на команду {command} за {timeoutMs}мс");
        }

        return await tcs.Task;
    }

    private async Task SendRawAsync(string message)
    {
        if (_writer == null) return;
        lock (_lock)
        {
            _writer.WriteLine(message);
        }
        await Task.CompletedTask;
    }

    // === Helpers ===

    private static Candle ParseCandle(JsonElement e) => new()
    {
        Timestamp = DateTime.TryParse(GetString(e, "datetime"), out var dt) ? dt : DateTime.Now,
        Open = GetDouble(e, "open"),
        High = GetDouble(e, "high"),
        Low = GetDouble(e, "low"),
        Close = GetDouble(e, "close"),
        Volume = GetLong(e, "volume")
    };

    private static string GetClassCode(string ticker)
    {
        // Фьючерсы MOEX
        if (ticker.StartsWith("Si") || ticker.StartsWith("BR") || ticker.StartsWith("GD") ||
            ticker.StartsWith("MX") || ticker.StartsWith("RI") || ticker.StartsWith("CR") ||
            ticker.StartsWith("ED") || ticker.StartsWith("Eu"))
            return "SPBFUT";

        // Акции MOEX
        return "TQBR";
    }

    private static int TimeframeToQuikInterval(TimeSpan tf) => (int)tf.TotalMinutes switch
    {
        1 => 1,      // INTERVAL_M1
        5 => 5,      // INTERVAL_M5
        15 => 15,    // INTERVAL_M15
        30 => 30,    // INTERVAL_M30
        60 => 60,    // INTERVAL_H1
        240 => 240,  // INTERVAL_H4 (не стандарт, но Lua обработает)
        1440 => 1440, // INTERVAL_D1
        _ => 5
    };

    private static OrderStatus MapOrderStatus(string status) => status switch
    {
        "active" => OrderStatus.Active,
        "filled" => OrderStatus.Filled,
        "partially_filled" => OrderStatus.PartiallyFilled,
        "cancelled" => OrderStatus.Cancelled,
        "rejected" => OrderStatus.Rejected,
        _ => OrderStatus.Pending
    };

    private static double GetDouble(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) ? v.GetDouble() : 0;

    private static long GetLong(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) ? v.GetInt64() : 0;

    private static string GetString(JsonElement e, string prop) =>
        e.TryGetProperty(prop, out var v) ? v.GetString() ?? "" : "";

    public void Dispose()
    {
        _autoReconnect = false;
        _cts?.Cancel();
        _fallbackCts?.Cancel();
        CleanupConnection();
        _server?.Stop();
        _fallback?.Dispose();
    }
}
