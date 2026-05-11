using HedgeFund.Core;
using HedgeFund.Core.Models;
using GrpcMd = Grpc.Tradeapi.V1.Marketdata;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// Коннектор Финам через Trade API (REST + gRPC стриминг).
/// gRPC — основной источник реалтайм данных (свечи, котировки, стакан).
/// REST — fallback для исторических данных и legacy совместимости.
/// </summary>
public class FinamConnector : IBrokerConnector
{
    private FinamApiClient? _restClient;
    private FinamGrpcClient? _grpcClient;
    public FinamGrpcClient? GrpcClient => _grpcClient;
    public FinamApiClient? RestClient => _restClient;
    private string _accountId = string.Empty;
    private CancellationTokenSource? _globalCts;

    public string BrokerName => "Финам (Trade API)";
    public bool IsConnected { get; private set; }

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    /// <summary>
    /// Подключение. token = access_token (длинный).
    /// Создаёт REST клиент + gRPC клиент с подписками.
    /// </summary>
    public async Task<bool> ConnectAsync(string token, string accountId = "")
    {
        try
        {
            _globalCts = new CancellationTokenSource();

            // REST клиент — нужен API_KEY (tapi_sk_...), НЕ gRPC token
            var apiKey = Environment.GetEnvironmentVariable("FINAM_API_KEY") ?? "";
            _restClient = !string.IsNullOrEmpty(apiKey)
                ? new FinamApiClient(apiKey)
                : new FinamApiClient(token); // fallback на gRPC token
            await _restClient.AuthenticateAsync();

            // gRPC клиент (основной — стриминг)
            _grpcClient = new FinamGrpcClient(token);
            _grpcClient.OnError += msg => OnError?.Invoke(msg);
            _grpcClient.OnLog += msg => Console.WriteLine(msg);

            bool grpcOk = await _grpcClient.ConnectAsync(accountId);
            if (!grpcOk)
            {
                OnError?.Invoke("⚠️ gRPC не подключился, работаем через REST fallback");
            }
            else
            {
                _accountId = _grpcClient.AccountId;

                // Подписка на свои сделки (gRPC stream)
                _ = Task.Run(() => _grpcClient.SubscribeMyTradesAsync(
                    (symbol, side, price, qty) =>
                    {
                        OnTrade?.Invoke(new Trade
                        {
                            Ticker = symbol.Split('@')[0],
                            Direction = side.Contains("BUY") ? SignalDirection.Buy : SignalDirection.Sell,
                            Price = price,
                            Volume = qty,
                            Timestamp = DateTime.UtcNow
                        });
                    }, _globalCts.Token));

                // Подписка на обновления заявок (gRPC stream)
                _ = Task.Run(() => _grpcClient.SubscribeOrdersAsync(
                    (orderId, status, side, price, qty) =>
                    {
                        OnOrderUpdate?.Invoke(new Order
                        {
                            BrokerOrderId = orderId,
                            Status = MapGrpcOrderStatus(status),
                            Direction = side.Contains("BUY") ? SignalDirection.Buy : SignalDirection.Sell,
                            Price = price,
                            Volume = qty
                        });
                    }, _globalCts.Token));
            }

            // Находим FORTS счёт (для фьючерсов) или первый с балансом
            if (string.IsNullOrEmpty(_accountId))
            {
                var details = await _restClient.GetTokenDetailsAsync();
                if (details?.AccountIds.Count > 0)
                {
                    // Пробуем найти FORTS счёт
                    foreach (var id in details.AccountIds)
                    {
                        try
                        {
                            var acc = await _restClient.GetAccountAsync(id);
                            if (acc != null)
                            {
                                var type = acc.GetType().GetProperty("Type")?.GetValue(acc)?.ToString() ?? "";
                                // В REST ответе тип в поле type (может быть FORTS, MICEX)
                                Console.WriteLine($"[FINAM] Account {id}: money={acc.Money?.Count ?? 0}");
                            }
                        }
                        catch { }
                    }
                    // Используем последний счёт (FORTS обычно последний)
                    _accountId = details.AccountIds[^1];
                }
            }

            IsConnected = true;
            OnConnectionChanged?.Invoke(true);
            return true;
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"Ошибка подключения: {ex.Message}");
            return false;
        }
    }

    public Task DisconnectAsync()
    {
        _globalCts?.Cancel();
        _grpcClient?.Dispose();
        _restClient?.Dispose();
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        return Task.CompletedTask;
    }

    // === Маркетдата: REST polling (надёжно, без разрывов) ===

    private DateTime _lastCandleTime = DateTime.MinValue;
    private bool _candlePollingActive = false;

    /// <summary>
    /// Подписка на свечи через REST polling.
    /// gRPC больше НЕ используется для данных — только для ордеров.
    /// Polling каждые 5 сек для 5-мин TF, дедупликация по timestamp.
    /// </summary>
    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        EnsureConnected();
        if (_candlePollingActive) return;
        _candlePollingActive = true;
        _lastCandleTime = DateTime.MinValue;

        _ = Task.Run(async () =>
        {
            Console.WriteLine($"[CANDLE] 🔄 REST polling для {ticker} TF={timeframe}");
            var symbol = ToSymbol(ticker);
            var tfStr = TimeframeToString(timeframe);
            int pollErrors = 0;

            while (!(_globalCts?.IsCancellationRequested ?? true))
            {
                try
                {
                    // Запрашиваем последние 2 свечи
                    var to = DateTime.UtcNow;
                    var from = to.Subtract(timeframe * 3);
                    var bars = await _restClient!.GetBarsAsync(symbol, tfStr, from.ToString("o"), to.ToString("o"));

                    if (bars?.Bars?.Count > 0)
                    {
                        if (_lastCandleTime == DateTime.MinValue)
                            Console.WriteLine($"[CANDLE] ✅ {ticker}: got {bars.Bars.Count} bars, last={bars.Bars[^1].Timestamp} C={bars.Bars[^1].Close?.ToDouble():F0}");
                        // Отдаём только НОВЫЕ свечи (дедупликация по timestamp)
                        foreach (var bar in bars.Bars)
                        {
                            var candle = BarToCandle(bar);
                            if (candle.Timestamp > _lastCandleTime)
                            {
                                _lastCandleTime = candle.Timestamp;
                                onCandle(candle);
                            }
                        }
                    }
                    pollErrors = 0; // сброс ошибок
                }
                catch (Exception ex)
                {
                    pollErrors++;
                    if (pollErrors <= 3 || pollErrors % 20 == 0)
                        Console.WriteLine($"[CANDLE] ⚠️ REST poll error #{pollErrors}: {ex.Message}");
                }

                // Polling interval: 5 сек для 5-мин TF, 10 сек для 1-мин
                var delay = (int)timeframe.TotalMinutes <= 5 ? 5000 : 10000;
                try { await Task.Delay(delay, _globalCts!.Token); }
                catch { break; }
            }
            _candlePollingActive = false;
            Console.WriteLine("[CANDLE] Polling остановлен");
        });
    }

    /// <summary>Подписка на котировки через gRPC</summary>
    public async Task SubscribeQuotesAsync(string ticker, Action<double, double, double> onQuote,
        CancellationToken ct = default)
    {
        EnsureConnected();
        // Quotes через REST polling пока не реализованы — заглушка
        // gRPC quote stream нестабилен
    }

    /// <summary>Подписка на стакан через gRPC</summary>
    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        // Заглушка — стакан через REST пока не реализован
        return Task.CompletedTask;
    }

    /// <summary>Подписка на стакан через gRPC SubscribeOrderBook</summary>
    public async Task SubscribeOrderBookAsync(string ticker,
        Action<List<(double price, long bidVol, long askVol)>> onOrderBook,
        CancellationToken ct = default)
    {
        EnsureConnected();
        if (_grpcClient?.IsConnected == true)
        {
            var token = ct == default ? _globalCts!.Token : ct;
            await _grpcClient.SubscribeOrderBookAsync(ToSymbol(ticker), (symbol, rows) =>
            {
                onOrderBook(rows);
            }, token);
        }
    }

    /// <summary>Подписка на обновления счёта (позиции, equity)</summary>
    public async Task SubscribeAccountAsync(
        Action<double, List<(string symbol, long qty, double avgPrice)>> onUpdate)
    {
        EnsureConnected();
        if (_grpcClient?.IsConnected == true)
        {
            _ = Task.Run(() => _grpcClient.SubscribeAccountAsync(onUpdate, _globalCts!.Token));
        }
    }

    // === Исторические данные: gRPC unary (fallback REST) ===

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        EnsureConnected();
        var symbol = ToSymbol(ticker);
        var tf = TimeframeToGrpc(timeframe);

        // Пробуем gRPC
        if (_grpcClient?.IsConnected == true)
        {
            try
            {
                return await _grpcClient.GetBarsAsync(symbol, tf, from, to);
            }
            catch (Exception ex)
            {
                OnError?.Invoke($"gRPC GetBars failed: {ex.Message}, fallback REST");
            }
        }

        // REST fallback
        var tfStr = TimeframeToString(timeframe);
        var bars = await _restClient!.GetBarsAsync(symbol, tfStr, from.ToString("o"), to.ToString("o"));
        if (bars?.Bars == null) return Array.Empty<Candle>();
        return bars.Bars.Select(BarToCandle).ToArray();
    }

    // === Торговля: gRPC (fallback REST) ===

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        EnsureConnected();
        EnsureAccountId();

        var symbol = ToSymbol(order.Ticker);
        var side = order.Direction == SignalDirection.Buy
            ? Grpc.Tradeapi.V1.Side.Buy
            : Grpc.Tradeapi.V1.Side.Sell;
        var orderType = order.Type == Core.Models.OrderType.Market
            ? Grpc.Tradeapi.V1.Orders.OrderType.Market
            : Grpc.Tradeapi.V1.Orders.OrderType.Limit;

        if (_grpcClient?.IsConnected == true)
        {
            try
            {
                var orderId = await _grpcClient.PlaceOrderAsync(symbol, side, orderType,
                    order.Volume, order.Type == Core.Models.OrderType.Limit ? order.Price : null,
                    order.Comment);
                order.BrokerOrderId = orderId;
                order.Status = OrderStatus.Active;
                OnOrderUpdate?.Invoke(order);
                return order;
            }
            catch (Exception ex)
            {
                OnError?.Invoke($"gRPC PlaceOrder failed: {ex.Message}, fallback REST");
            }
        }

        // REST fallback
        var request = new PlaceOrderRequest
        {
            Symbol = symbol,
            Side = order.Direction == SignalDirection.Buy ? "SIDE_BUY" : "SIDE_SELL",
            Quantity = new DecimalValue { Value = order.Volume.ToString() },
            OrderType = order.Type == Core.Models.OrderType.Market ? "ORDER_TYPE_MARKET" : "ORDER_TYPE_LIMIT",
            Price = order.Type == Core.Models.OrderType.Limit ? new DecimalValue { Value = ((int)order.Price).ToString() } : null
        };

        var result = await _restClient!.PlaceOrderAsync(_accountId, request);
        order.BrokerOrderId = result?.OrderId ?? "";
        order.Status = OrderStatus.Active;
        OnOrderUpdate?.Invoke(order);
        return order;
    }

    public async Task<bool> CancelOrderAsync(string orderId)
    {
        EnsureConnected();
        EnsureAccountId();

        if (_grpcClient?.IsConnected == true)
        {
            try { return await _grpcClient.CancelOrderAsync(orderId); }
            catch { /* fallback REST */ }
        }

        var result = await _restClient!.CancelOrderAsync(_accountId, orderId);
        return result != null;
    }

    public async Task<Order[]> GetActiveOrdersAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var result = await _restClient!.GetOrdersAsync(_accountId);
        if (result?.Orders == null) return Array.Empty<Order>();

        return result.Orders.Select(o => new Order
        {
            BrokerOrderId = o.OrderId,
            Ticker = o.Symbol.Split('@')[0],
            Direction = o.Side == "SIDE_BUY" ? SignalDirection.Buy : SignalDirection.Sell,
            Status = MapOrderStatus(o.Status),
            Volume = o.Quantity,
            FilledVolume = o.FilledQuantity,
            Price = o.Price
        }).ToArray();
    }

    public async Task<(double equity, List<(string symbol, long qty, double avgPrice)>)> GetAccountInfoAsync()
    {
        if (_grpcClient?.IsConnected == true)
            return await _grpcClient.GetAccountInfoAsync();
        return (0, new());
    }

    public async Task<Position[]> GetPositionsAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        // Пробуем gRPC
        if (_grpcClient?.IsConnected == true)
        {
            try
            {
                var (equity, positions) = await _grpcClient.GetAccountInfoAsync();
                return positions.Select(p => new Position
                {
                    Ticker = p.symbol.Split('@')[0],
                    Direction = p.qty > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                    Entries = new List<PositionEntry>
                    {
                        new() { Price = p.avgPrice, Volume = (int)Math.Abs(p.qty), Comment = "Позиция из Финам" }
                    }
                }).Where(p => p.Entries[0].Volume > 0).ToArray();
            }
            catch { /* fallback REST */ }
        }

        var account = await _restClient!.GetAccountAsync(_accountId);
        if (account?.Positions == null) return Array.Empty<Position>();

        return account.Positions.Select(p => new Position
        {
            Ticker = p.Symbol.Split('@')[0],
            Direction = p.Balance > 0 ? SignalDirection.Buy : SignalDirection.Sell,
            Entries = new List<PositionEntry>
            {
                new() { Price = p.AveragePrice ?? p.CurrentPrice ?? 0, Volume = (int)Math.Abs(p.Balance), Comment = "Позиция из Финам" }
            }
        }).ToArray();
    }

    public async Task<double> GetBalanceAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        if (_grpcClient?.IsConnected == true)
        {
            try
            {
                var (equity, _) = await _grpcClient.GetAccountInfoAsync();
                return equity;
            }
            catch { /* fallback REST */ }
        }

        var account = await _restClient!.GetAccountAsync(_accountId);
        return account?.Money.FirstOrDefault(m => m.Currency == "RUB")?.Balance ?? 0;
    }

    // === Helpers ===

    private void EnsureConnected()
    {
        if (!IsConnected)
            throw new InvalidOperationException("Не подключен к Финам API");
    }

    private void EnsureAccountId()
    {
        if (string.IsNullOrEmpty(_accountId))
            throw new InvalidOperationException("Торговый счёт не привязан");
    }

    /// <summary>REST polling свечей как fallback</summary>
    // PollCandlesRestAsync удалён — заменён на SubscribeCandlesAsync с дедупликацией

    /// <summary>
    /// Определить биржу по тикеру:
    /// MISX — МосБиржа, все основные рынки (акции, облигации, ETF)
    /// RTSX — МосБиржа, рынок деривативов (фьючерсы, опционы)
    /// </summary>
    private static string ToSymbol(string ticker)
    {
        if (ticker.Contains('@')) return ticker;

        // Фьючерсы MOEX: Si, BR, GD, MX, RI, GOLD, ED, Eu, SBRF, GAZR и т.д.
        // Формат: БазовыйКод + Месяц(буква) + Год(цифра), напр.: SiM6, BRN6, GDM6, MXM6
        var futuresPrefixes = new[] { "Si", "BR", "GD", "MX", "RI", "GOLD", "ED", "Eu", 
                                       "SBRF", "GAZR", "LKOH", "ROSN", "VTBR", "SNGR", 
                                       "SILV", "NG", "PL", "CR", "ALRS", "MGNT" };

        foreach (var prefix in futuresPrefixes)
        {
            if (ticker.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                return $"{ticker}@RTSX";  // Рынок деривативов МосБиржи
        }

        // Акции, облигации, ETF → MISX
        return $"{ticker}@MISX";
    }

    private static GrpcMd.TimeFrame TimeframeToGrpc(TimeSpan tf) => (int)tf.TotalMinutes switch
    {
        1 => GrpcMd.TimeFrame.M1,
        5 => GrpcMd.TimeFrame.M5,
        15 => GrpcMd.TimeFrame.M15,
        30 => GrpcMd.TimeFrame.M30,
        60 => GrpcMd.TimeFrame.H1,
        120 => GrpcMd.TimeFrame.H2,
        240 => GrpcMd.TimeFrame.H4,
        480 => GrpcMd.TimeFrame.H8,
        1440 => GrpcMd.TimeFrame.D,
        _ => GrpcMd.TimeFrame.M5
    };

    private static string TimeframeToString(TimeSpan tf) => (int)tf.TotalMinutes switch
    {
        1 => "TIME_FRAME_M1",
        5 => "TIME_FRAME_M5",
        15 => "TIME_FRAME_M15",
        60 => "TIME_FRAME_H1",
        240 => "TIME_FRAME_H4",
        1440 => "TIME_FRAME_D",
        _ => "TIME_FRAME_M5"
    };

    private static Candle BarToCandle(Bar bar) => new()
    {
        Timestamp = DateTime.TryParse(bar.Timestamp, out var ts) ? ts : DateTime.UtcNow,
        Open = bar.Open?.ToDouble() ?? 0,
        Close = bar.Close?.ToDouble() ?? 0,
        High = bar.High?.ToDouble() ?? 0,
        Low = bar.Low?.ToDouble() ?? 0,
        Volume = (long)(bar.Volume?.ToDouble() ?? 0)
    };

    private static OrderStatus MapOrderStatus(string status) => status switch
    {
        "ORDER_STATUS_ACTIVE" or "ORDER_STATUS_NEW" => OrderStatus.Active,
        "ORDER_STATUS_FILLED" => OrderStatus.Filled,
        "ORDER_STATUS_PARTIALLY_FILLED" => OrderStatus.PartiallyFilled,
        "ORDER_STATUS_CANCELLED" or "ORDER_STATUS_CANCELED" => OrderStatus.Cancelled,
        "ORDER_STATUS_REJECTED" => OrderStatus.Rejected,
        _ => OrderStatus.Pending
    };

    private static OrderStatus MapGrpcOrderStatus(string status) => status switch
    {
        var s when s.Contains("FILLED") && !s.Contains("PARTIAL") => OrderStatus.Filled,
        var s when s.Contains("PARTIAL") => OrderStatus.PartiallyFilled,
        var s when s.Contains("CANCEL") => OrderStatus.Cancelled,
        var s when s.Contains("REJECT") => OrderStatus.Rejected,
        var s when s.Contains("NEW") || s.Contains("ACTIVE") => OrderStatus.Active,
        _ => OrderStatus.Pending
    };

    public void Dispose()
    {
        _globalCts?.Cancel();
        _grpcClient?.Dispose();
        _restClient?.Dispose();
    }
}
