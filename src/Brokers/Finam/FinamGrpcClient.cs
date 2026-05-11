using Grpc.Core;
using Grpc.Net.Client;
using GrpcAuth = Grpc.Tradeapi.V1.Auth;
using GrpcMd = Grpc.Tradeapi.V1.Marketdata;
using GrpcOrd = Grpc.Tradeapi.V1.Orders;
using GrpcAcc = Grpc.Tradeapi.V1.Accounts;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// gRPC клиент для Finam Trade API.
/// Стриминг свечей, котировок, стакана, заявок, сделок.
/// Авторефреш JWT через SubscribeJwtRenewal.
/// Автопереподключение при разрыве.
/// </summary>
public class FinamGrpcClient : IDisposable
{
    private readonly string _accessToken;
    private readonly string _endpoint;
    private GrpcChannel? _channel;
    private string _jwt = string.Empty;
    private string _accountId = string.Empty;
    private CancellationTokenSource? _jwtRenewalCts;

    private GrpcAuth.AuthService.AuthServiceClient? _authClient;
    private GrpcMd.MarketDataService.MarketDataServiceClient? _marketDataClient;
    private GrpcOrd.OrdersService.OrdersServiceClient? _ordersClient;
    private GrpcAcc.AccountsService.AccountsServiceClient? _accountsClient;

    public bool IsConnected { get; private set; }
    public string AccountId => _accountId;

    public event Action<string>? OnError;
    public event Action<string>? OnLog;

    public FinamGrpcClient(string accessToken, string endpoint = "https://api.finam.ru")
    {
        _accessToken = accessToken;
        _endpoint = endpoint;
    }

    // === Подключение ===

    public async Task<bool> ConnectAsync(string accountId = "")
    {
        try
        {
            _channel = GrpcChannel.ForAddress(_endpoint, new GrpcChannelOptions
            {
                HttpHandler = new SocketsHttpHandler
                {
                    EnableMultipleHttp2Connections = true,
                    KeepAlivePingDelay = TimeSpan.FromSeconds(30),
                    KeepAlivePingTimeout = TimeSpan.FromSeconds(10),
                    PooledConnectionIdleTimeout = Timeout.InfiniteTimeSpan,
                }
            });

            _authClient = new GrpcAuth.AuthService.AuthServiceClient(_channel);
            _marketDataClient = new GrpcMd.MarketDataService.MarketDataServiceClient(_channel);
            _ordersClient = new GrpcOrd.OrdersService.OrdersServiceClient(_channel);
            _accountsClient = new GrpcAcc.AccountsService.AccountsServiceClient(_channel);

            // Получаем JWT
            var authReply = await _authClient.AuthAsync(new GrpcAuth.AuthRequest { Secret = _accessToken });
            _jwt = authReply.Token;
            Log("✅ gRPC JWT получен");

            // Определяем account_id
            if (!string.IsNullOrEmpty(accountId))
            {
                _accountId = accountId;
            }
            else
            {
                var details = await _authClient.TokenDetailsAsync(
                    new GrpcAuth.TokenDetailsRequest { Token = _jwt },
                    CreateAuthHeaders());
                if (details.AccountIds.Count > 0)
                    _accountId = details.AccountIds[^1]; // FORTS обычно последний
            }
            Log($"📋 Account: {_accountId}");

            // Запускаем авторефреш JWT
            _jwtRenewalCts = new CancellationTokenSource();
            _ = Task.Run(() => RunJwtRenewalAsync(_jwtRenewalCts.Token));

            IsConnected = true;
            return true;
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"gRPC connect failed: {ex.Message}");
            return false;
        }
    }

    private async Task RunJwtRenewalAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var stream = _authClient!.SubscribeJwtRenewal(
                    new GrpcAuth.SubscribeJwtRenewalRequest { Secret = _accessToken });

                await foreach (var response in stream.ResponseStream.ReadAllAsync(ct))
                {
                    _jwt = response.Token;
                    Log("🔄 JWT обновлён");
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                OnError?.Invoke($"JWT renewal error: {ex.Message}");
                await Task.Delay(5000, ct);
            }
        }
    }

    // === MarketData Subscriptions ===

    /// <summary>Подписка на свечи (Server Stream). Автопереподключение.</summary>
    public async Task SubscribeBarsAsync(string symbol, GrpcMd.TimeFrame timeframe,
        Action<Core.Models.Candle> onCandle, CancellationToken ct)
    {
        var request = new GrpcMd.SubscribeBarsRequest
        {
            Symbol = symbol,
            Timeframe = timeframe
        };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _marketDataClient!.SubscribeBars(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var bar in response.Bars)
                    onCandle(BarToCandle(bar));
            }
        }, $"SubscribeBars({symbol})", ct);
    }

    /// <summary>Подписка на котировки (Server Stream)</summary>
    public async Task SubscribeQuoteAsync(string[] symbols,
        Action<string, double, double, double> onQuote, CancellationToken ct)
    {
        var request = new GrpcMd.SubscribeQuoteRequest();
        request.Symbols.AddRange(symbols);

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _marketDataClient!.SubscribeQuote(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var quote in response.Quote)
                {
                    onQuote(quote.Symbol,
                        DecToDouble(quote.Bid),
                        DecToDouble(quote.Ask),
                        DecToDouble(quote.Last));
                }
            }
        }, $"SubscribeQuote", ct);
    }

    /// <summary>Подписка на стакан (Server Stream)</summary>
    public async Task SubscribeOrderBookAsync(string symbol,
        Action<string, List<(double price, long bidVol, long askVol)>> onOrderBook,
        CancellationToken ct)
    {
        var request = new GrpcMd.SubscribeOrderBookRequest { Symbol = symbol };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _marketDataClient!.SubscribeOrderBook(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var ob in response.OrderBook)
                {
                    var rows = new List<(double price, long bidVol, long askVol)>();
                    foreach (var row in ob.Rows)
                    {
                        double price = DecToDouble(row.Price);
                        long bidVol = row.SideCase == GrpcMd.StreamOrderBook.Types.Row.SideOneofCase.BuySize
                            ? (long)DecToDouble(row.BuySize) : 0;
                        long askVol = row.SideCase == GrpcMd.StreamOrderBook.Types.Row.SideOneofCase.SellSize
                            ? (long)DecToDouble(row.SellSize) : 0;
                        rows.Add((price, bidVol, askVol));
                    }
                    onOrderBook(ob.Symbol, rows);
                }
            }
        }, $"SubscribeOrderBook({symbol})", ct);
    }

    /// <summary>Подписка на ленту сделок инструмента</summary>
    public async Task SubscribeLatestTradesAsync(string symbol,
        Action<string, double, long, string> onTrade, CancellationToken ct)
    {
        var request = new GrpcMd.SubscribeLatestTradesRequest { Symbol = symbol };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _marketDataClient!.SubscribeLatestTrades(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var trade in response.Trades)
                {
                    onTrade(symbol,
                        DecToDouble(trade.Price),
                        (long)DecToDouble(trade.Size),
                        trade.Side.ToString());
                }
            }
        }, $"SubscribeLatestTrades({symbol})", ct);
    }

    // === Orders Subscriptions ===

    /// <summary>Подписка на обновления своих заявок</summary>
    public async Task SubscribeOrdersAsync(
        Action<string, string, string, double, int> onOrderUpdate, CancellationToken ct)
    {
        var request = new GrpcOrd.SubscribeOrdersRequest { AccountId = _accountId };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _ordersClient!.SubscribeOrders(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var os in response.Orders)
                {
                    onOrderUpdate(
                        os.OrderId,
                        os.Status.ToString(),
                        os.Order?.Side.ToString() ?? "",
                        DecToDouble(os.Order?.LimitPrice),
                        (int)DecToDouble(os.InitialQuantity));
                }
            }
        }, "SubscribeOrders", ct);
    }

    /// <summary>Подписка на свои сделки</summary>
    public async Task SubscribeMyTradesAsync(
        Action<string, string, double, int> onTrade, CancellationToken ct)
    {
        var request = new GrpcOrd.SubscribeTradesRequest { AccountId = _accountId };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _ordersClient!.SubscribeTrades(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                foreach (var trade in response.Trades)
                {
                    onTrade(
                        trade.Symbol,
                        trade.Side.ToString(),
                        DecToDouble(trade.Price),
                        (int)DecToDouble(trade.Size));
                }
            }
        }, "SubscribeMyTrades", ct);
    }

    // === Account Subscription ===

    /// <summary>Подписка на обновления счёта (позиции, баланс, equity)</summary>
    public async Task SubscribeAccountAsync(
        Action<double, List<(string symbol, long qty, double avgPrice)>> onUpdate,
        CancellationToken ct)
    {
        var request = new GrpcAcc.GetAccountRequest { AccountId = _accountId };

        await RunStreamWithReconnect(async (token) =>
        {
            using var stream = _accountsClient!.SubscribeAccount(request, CreateAuthHeaders(), cancellationToken: token);
            await foreach (var response in stream.ResponseStream.ReadAllAsync(token))
            {
                double equity = DecToDouble(response.Equity);
                var positions = new List<(string symbol, long qty, double avgPrice)>();

                foreach (var pos in response.Positions)
                {
                    positions.Add((
                        pos.Symbol,
                        (long)DecToDouble(pos.Quantity),
                        DecToDouble(pos.AveragePrice)));
                }

                onUpdate(equity, positions);
            }
        }, "SubscribeAccount", ct);
    }

    // === Unary Methods ===

    /// <summary>Выставить заявку через gRPC</summary>
    public async Task<string> PlaceOrderAsync(string symbol, Grpc.Tradeapi.V1.Side side,
        GrpcOrd.OrderType orderType, int quantity, double? limitPrice = null, string? comment = null)
    {
        var order = new GrpcOrd.Order
        {
            AccountId = _accountId,
            Symbol = symbol,
            Side = side,
            Type = orderType,
            Quantity = new Google.Type.Decimal { Value = quantity.ToString() },
            TimeInForce = GrpcOrd.TimeInForce.Day,
        };

        if (limitPrice.HasValue)
            order.LimitPrice = new Google.Type.Decimal { Value = ((int)limitPrice.Value).ToString() };
        if (comment != null)
            order.Comment = comment;

        var response = await _ordersClient!.PlaceOrderAsync(order, CreateAuthHeaders());
        Log($"📋 PlaceOrder: symbol={symbol} side={side} type={orderType} qty={quantity} price={limitPrice} → orderId={response.OrderId} status={response.Status}");
        return response.OrderId;
    }

    /// <summary>Отменить заявку через gRPC</summary>
    public async Task<bool> CancelOrderAsync(string orderId)
    {
        var request = new GrpcOrd.CancelOrderRequest
        {
            AccountId = _accountId,
            OrderId = orderId
        };
        var response = await _ordersClient!.CancelOrderAsync(request, CreateAuthHeaders());
        return !string.IsNullOrEmpty(response.OrderId);
    }

    /// <summary>Получить историю свечей (unary)</summary>
    public async Task<Core.Models.Candle[]> GetBarsAsync(string symbol, GrpcMd.TimeFrame timeframe,
        DateTime from, DateTime to)
    {
        var request = new GrpcMd.BarsRequest
        {
            Symbol = symbol,
            Timeframe = timeframe,
            Interval = new Google.Type.Interval
            {
                StartTime = Google.Protobuf.WellKnownTypes.Timestamp.FromDateTime(from.ToUniversalTime()),
                EndTime = Google.Protobuf.WellKnownTypes.Timestamp.FromDateTime(to.ToUniversalTime())
            }
        };

        var response = await _marketDataClient!.BarsAsync(request, CreateAuthHeaders());
        return response.Bars.Select(BarToCandle).ToArray();
    }

    /// <summary>Получить последнюю котировку (unary)</summary>
    public async Task<(double bid, double ask, double last)> GetLastQuoteAsync(string symbol)
    {
        var request = new GrpcMd.QuoteRequest { Symbol = symbol };
        var response = await _marketDataClient!.LastQuoteAsync(request, CreateAuthHeaders());
        return (
            DecToDouble(response.Quote?.Bid),
            DecToDouble(response.Quote?.Ask),
            DecToDouble(response.Quote?.Last));
    }

    /// <summary>Получить информацию о счёте (unary)</summary>
    public async Task<(double equity, List<(string symbol, long qty, double avgPrice)> positions)> GetAccountInfoAsync()
    {
        var request = new GrpcAcc.GetAccountRequest { AccountId = _accountId };
        var response = await _accountsClient!.GetAccountAsync(request, CreateAuthHeaders());
        double equity = DecToDouble(response.Equity);
        var positions = response.Positions.Select(p => (
            p.Symbol,
            (long)DecToDouble(p.Quantity),
            DecToDouble(p.AveragePrice)
        )).ToList();
        return (equity, positions);
    }

    // === Helpers ===

    private async Task RunStreamWithReconnect(Func<CancellationToken, Task> streamAction,
        string streamName, CancellationToken ct)
    {
        int retryDelay = 1000;
        const int maxRetryDelay = 30000;

        while (!ct.IsCancellationRequested)
        {
            try
            {
                Log($"📡 {streamName}: подключение...");
                await streamAction(ct);
                Log($"📡 {streamName}: стрим завершён, переподключение...");
                retryDelay = 1000;
            }
            catch (OperationCanceledException) { break; }
            catch (RpcException ex) when (ex.StatusCode == StatusCode.Cancelled) { break; }
            catch (RpcException ex) when (ex.StatusCode == StatusCode.Unauthenticated)
            {
                OnError?.Invoke($"📡 {streamName}: JWT протух, обновляю...");
                try
                {
                    var authReply = await _authClient!.AuthAsync(new GrpcAuth.AuthRequest { Secret = _accessToken });
                    _jwt = authReply.Token;
                    Log("🔄 JWT обновлён");
                    retryDelay = 1000;
                    continue;
                }
                catch (Exception authEx)
                {
                    OnError?.Invoke($"Auth refresh failed: {authEx.Message}");
                }
            }
            catch (RpcException ex) when (ex.StatusCode == StatusCode.Unavailable)
            {
                OnError?.Invoke($"📡 {streamName}: сервер недоступен, повтор через {retryDelay / 1000}с");
            }
            catch (Exception ex)
            {
                OnError?.Invoke($"📡 {streamName}: ошибка: {ex.Message}");
            }

            await Task.Delay(retryDelay, ct);
            retryDelay = Math.Min(retryDelay * 2, maxRetryDelay);
        }
    }

    private Metadata CreateAuthHeaders()
    {
        return new Metadata { { "Authorization", $"Bearer {_jwt}" } };
    }

    private static Core.Models.Candle BarToCandle(GrpcMd.Bar bar) => new()
    {
        Timestamp = bar.Timestamp?.ToDateTime() ?? DateTime.UtcNow,
        Open = DecToDouble(bar.Open),
        High = DecToDouble(bar.High),
        Low = DecToDouble(bar.Low),
        Close = DecToDouble(bar.Close),
        Volume = (long)DecToDouble(bar.Volume)
    };

    private static double DecToDouble(Google.Type.Decimal? d)
    {
        if (d == null || string.IsNullOrEmpty(d.Value)) return 0;
        return double.TryParse(d.Value, System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out var val) ? val : 0;
    }

    private void Log(string msg)
    {
        OnLog?.Invoke($"[gRPC] {msg}");
        Console.WriteLine($"[gRPC] {msg}");
    }

    public void Dispose()
    {
        _jwtRenewalCts?.Cancel();
        _channel?.Dispose();
    }
}
