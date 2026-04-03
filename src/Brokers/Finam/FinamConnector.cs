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

            // REST клиент (для исторических данных и legacy)
            _restClient = new FinamApiClient(token);
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

            // Fallback account_id через REST если gRPC не дал
            if (string.IsNullOrEmpty(_accountId))
            {
                var details = await _restClient.GetTokenDetailsAsync();
                if (details?.AccountIds.Count > 0)
                    _accountId = details.AccountIds[0];
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

    // === Маркетдата: gRPC стриминг ===

    /// <summary>
    /// Подписка на свечи через gRPC SubscribeBars (реалтайм, без polling).
    /// </summary>
    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        EnsureConnected();
        var symbol = ToSymbol(ticker);
        var tf = TimeframeToGrpc(timeframe);

        if (_grpcClient?.IsConnected == true)
        {
            // gRPC стрим — получаем свечи в реалтайме
            _ = Task.Run(() => _grpcClient.SubscribeBarsAsync(symbol, tf, onCandle, _globalCts!.Token));
        }
        else
        {
            // REST fallback: polling
            OnError?.Invoke($"gRPC недоступен для {ticker}, используем REST polling");
            _ = Task.Run(async () => await PollCandlesRestAsync(ticker, timeframe, onCandle, _globalCts!.Token));
        }
    }

    /// <summary>Подписка на котировки через gRPC</summary>
    public async Task SubscribeQuotesAsync(string ticker, Action<double, double, double> onQuote)
    {
        EnsureConnected();
        if (_grpcClient?.IsConnected == true)
        {
            _ = Task.Run(() => _grpcClient.SubscribeQuoteAsync(
                new[] { ToSymbol(ticker) },
                (symbol, bid, ask, last) => onQuote(bid, ask, last),
                _globalCts!.Token));
        }
    }

    /// <summary>Подписка на стакан через gRPC</summary>
    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        EnsureConnected();
        if (_grpcClient?.IsConnected == true)
        {
            _ = Task.Run(() => _grpcClient.SubscribeQuoteAsync(
                new[] { ToSymbol(ticker) },
                (symbol, bid, ask, last) => onBidAsk(bid, ask),
                _globalCts!.Token));
        }
        else
        {
            OnError?.Invoke("Level2 требует gRPC. Недоступен.");
        }
        return Task.CompletedTask;
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
            AccountId = _accountId,
            Symbol = symbol,
            Side = order.Direction == SignalDirection.Buy ? "SIDE_BUY" : "SIDE_SELL",
            Quantity = order.Volume,
            OrderType = order.Type == Core.Models.OrderType.Market ? "ORDER_TYPE_MARKET" : "ORDER_TYPE_LIMIT",
            Price = order.Type == Core.Models.OrderType.Limit ? order.Price : null
        };

        var result = await _restClient!.PlaceOrderAsync(request);
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
                new() { Price = p.AveragePrice, Volume = (int)Math.Abs(p.Balance), Comment = "Позиция из Финам" }
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
    private async Task PollCandlesRestAsync(string ticker, TimeSpan timeframe,
        Action<Candle> onCandle, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var to = DateTime.UtcNow;
                var from = to.Subtract(timeframe * 2);
                var tfStr = TimeframeToString(timeframe);

                var bars = await _restClient!.GetBarsAsync($"{ticker}@MISX", tfStr, from.ToString("o"), to.ToString("o"));
                if (bars?.Bars.Count > 0)
                    onCandle(BarToCandle(bars.Bars[^1]));
            }
            catch (Exception ex)
            {
                OnError?.Invoke($"REST polling {ticker}: {ex.Message}");
            }

            var delay = timeframe < TimeSpan.FromMinutes(5)
                ? TimeSpan.FromSeconds(10)
                : TimeSpan.FromSeconds(30);
            await Task.Delay(delay, ct);
        }
    }

    private static string ToSymbol(string ticker) =>
        ticker.Contains('@') ? ticker : $"{ticker}@MISX";

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
        Volume = bar.Volume
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
