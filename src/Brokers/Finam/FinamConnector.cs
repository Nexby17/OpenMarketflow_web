using HedgeFund.Core;
using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// Коннектор Финам через Trade API (REST only).
/// Все данные (свечи, стакан, котировки, позиции, заявки) — через REST polling.
/// gRPC стриминг отключён.
/// </summary>
public class FinamConnector : IBrokerConnector
{
    private FinamApiClient? _restClient;
    public FinamApiClient? RestClient => _restClient;
    private string _accountId = string.Empty;
    private CancellationTokenSource? _globalCts;

    public string BrokerName => "Финам (Trade API REST)";
    public bool IsConnected { get; private set; }

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    /// <summary>
    /// Подключение через REST. token = access_token (tapi_sk_...).
    /// Аутентификация → JWT, затем поиск торгового счёта.
    /// </summary>
    public async Task<bool> ConnectAsync(string token, string accountId = "")
    {
        try
        {
            _globalCts = new CancellationTokenSource();

            var apiKey = Environment.GetEnvironmentVariable("FINAM_API_KEY") ?? "";
            _restClient = !string.IsNullOrEmpty(apiKey)
                ? new FinamApiClient(apiKey)
                : new FinamApiClient(token);
            await _restClient.AuthenticateAsync();
            Console.WriteLine("[FINAM] ✅ REST auth OK");

            // Определяем счёт: явный параметр > env > список счетов (FORTS приоритет)
            if (!string.IsNullOrEmpty(accountId))
            {
                _accountId = accountId;
            }
            else
            {
                _accountId = Environment.GetEnvironmentVariable("FINAM_ACCOUNT_ID") ?? "";
            }

            if (string.IsNullOrEmpty(_accountId))
            {
                var accounts = await _restClient.GetAccountsAsync();
                if (accounts?.Accounts != null && accounts.Accounts.Count > 0)
                {
                    // Предпочитаем FORTS счёт (фьючерсы), иначе первый
                    var forts = accounts.Accounts.FirstOrDefault(a =>
                        a.Type.Contains("FORT", StringComparison.OrdinalIgnoreCase) ||
                        a.Type.Contains("FUT", StringComparison.OrdinalIgnoreCase));
                    _accountId = (forts ?? accounts.Accounts[0]).AccountId;
                }
                else
                {
                    var details = await _restClient.GetTokenDetailsAsync();
                    if (details?.AccountIds?.Count > 0)
                        _accountId = details.AccountIds[^1];
                }
            }

            if (string.IsNullOrEmpty(_accountId))
            {
                OnError?.Invoke("Не удалось определить торговый счёт");
                return false;
            }

            Console.WriteLine($"[FINAM] Account: {_accountId}");
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
        _restClient?.Dispose();
        _restClient = null;
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        return Task.CompletedTask;
    }

    // === Маркетдата: REST polling ===

    private DateTime _lastCandleTime = DateTime.MinValue;
    private bool _candlePollingActive = false;
    private readonly Dictionary<string, Action<Candle>> _candleSubscribers = new();
    private readonly object _candleLock = new();

    /// <summary>Подписка на свечи через REST polling (дедупликация по timestamp).</summary>
    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        EnsureConnected();
        lock (_candleLock) _candleSubscribers[ticker] = onCandle;
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
                    var to = DateTime.UtcNow;
                    var from = to.Subtract(timeframe * 3);
                    var bars = await _restClient!.GetBarsAsync(symbol, tfStr, from.ToString("o"), to.ToString("o"));

                    if (bars?.Bars?.Count > 0)
                    {
                        foreach (var bar in bars.Bars)
                        {
                            var candle = BarToCandle(bar);
                            if (candle.Timestamp > _lastCandleTime)
                            {
                                _lastCandleTime = candle.Timestamp;
                                if (_candleSubscribers.TryGetValue(ticker, out var cb)) cb(candle);
                            }
                        }
                    }
                    pollErrors = 0;
                }
                catch (Exception ex)
                {
                    pollErrors++;
                    if (pollErrors <= 3 || pollErrors % 20 == 0)
                        Console.WriteLine($"[CANDLE] ⚠️ REST poll error #{pollErrors}: {ex.Message}");
                }

                var delay = (int)timeframe.TotalMinutes <= 5 ? 5000 : 10000;
                try { await Task.Delay(delay, _globalCts!.Token); }
                catch { break; }
            }
            _candlePollingActive = false;
            Console.WriteLine("[CANDLE] Polling остановлен");
        });
    }

    /// <summary>Подписка на котировки через REST polling стакана (best bid/ask).</summary>
    public async Task SubscribeQuotesAsync(string ticker, Action<double, double, double> onQuote,
        CancellationToken ct = default)
    {
        EnsureConnected();
        var token = ct == default ? _globalCts!.Token : ct;
        _ = Task.Run(async () =>
        {
            var symbol = ToSymbol(ticker);
            while (!token.IsCancellationRequested)
            {
                try
                {
                    var rows = await _restClient!.GetOrderBookAsync(symbol);
                    if (rows != null && rows.Count > 0)
                    {
                        double bestBid = 0, bestAsk = 0;
                        foreach (var r in rows)
                        {
                            if (r.BidVolume > 0 && (bestBid == 0 || r.Price > bestBid)) bestBid = r.Price;
                            if (r.AskVolume > 0 && (bestAsk == 0 || r.Price < bestAsk)) bestAsk = r.Price;
                        }
                        var last = (bestBid > 0 && bestAsk > 0) ? (bestBid + bestAsk) / 2 : (bestBid > 0 ? bestBid : bestAsk);
                        onQuote(bestBid, bestAsk, last);
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[QUOTE] ⚠️ {ticker}: {ex.Message}");
                }
                try { await Task.Delay(3000, token); } catch { break; }
            }
        });
    }

    /// <summary>Подписка на best bid/ask (Level 2) через REST polling.</summary>
    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        EnsureConnected();
        _ = SubscribeQuotesAsync(ticker, (bid, ask, _) => onBidAsk(bid, ask));
        return Task.CompletedTask;
    }

    /// <summary>Подписка на стакан через REST polling.</summary>
    public async Task SubscribeOrderBookAsync(string ticker,
        Action<List<(double price, long bidVol, long askVol)>> onOrderBook,
        CancellationToken ct = default)
    {
        EnsureConnected();
        var token = ct == default ? _globalCts!.Token : ct;
        _ = Task.Run(async () =>
        {
            var symbol = ToSymbol(ticker);
            while (!token.IsCancellationRequested)
            {
                try
                {
                    var rows = await _restClient!.GetOrderBookAsync(symbol);
                    if (rows != null)
                    {
                        var snapshot = rows.Select(r => (r.Price, (long)r.BidVolume, (long)r.AskVolume)).ToList();
                        onOrderBook(snapshot);
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[BOOK] ⚠️ {ticker}: {ex.Message}");
                }
                try { await Task.Delay(3000, token); } catch { break; }
            }
        });
    }

    /// <summary>Подписка на обновления счёта (позиции, equity) через REST polling.</summary>
    public async Task SubscribeAccountAsync(
        Action<double, List<(string symbol, long qty, double avgPrice)>> onUpdate)
    {
        EnsureConnected();
        _ = Task.Run(async () =>
        {
            while (!(_globalCts?.IsCancellationRequested ?? true))
            {
                try
                {
                    var (equity, positions) = await GetAccountInfoAsync();
                    onUpdate(equity, positions);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[ACCT] ⚠️ {ex.Message}");
                }
                try { await Task.Delay(5000, _globalCts!.Token); } catch { break; }
            }
        });
    }

    // === Исторические данные: REST only ===

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        EnsureConnected();
        var symbol = ToSymbol(ticker);
        var tfStr = TimeframeToString(timeframe);
        var bars = await _restClient!.GetBarsAsync(symbol, tfStr, from.ToString("o"), to.ToString("o"));
        if (bars?.Bars == null) return Array.Empty<Candle>();
        return bars.Bars.Select(BarToCandle).ToArray();
    }

    // === Торговля: REST only ===

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        EnsureConnected();
        EnsureAccountId();

        var symbol = ToSymbol(order.Ticker);

        int maxRetries = 3;
        int delayMs = 1000;
        Exception? lastEx = null;

        for (int attempt = 0; attempt < maxRetries; attempt++)
        {
            try
            {
                var request = new PlaceOrderRequest
                {
                    Symbol = symbol,
                    Side = order.Direction == SignalDirection.Buy ? "SIDE_BUY" : "SIDE_SELL",
                    Quantity = new DecimalValue { Value = order.Volume.ToString() },
                    OrderType = order.Type == Core.Models.OrderType.Market ? "ORDER_TYPE_MARKET" : "ORDER_TYPE_LIMIT",
                    Price = order.Type == Core.Models.OrderType.Limit ? new DecimalValue { Value = order.Price.ToString("F0") } : null,
                    Comment = order.Comment
                };

                var result = await _restClient!.PlaceOrderAsync(_accountId, request);
                order.BrokerOrderId = result?.OrderId ?? "";
                order.Status = OrderStatus.Active;
                OnOrderUpdate?.Invoke(order);
                return order;
            }
            catch (FinamApiException ex) when ((int)ex.StatusCode >= 500)
            {
                lastEx = ex;
                OnError?.Invoke($"REST PlaceOrder attempt {attempt + 1}/{maxRetries}: 5xx, retrying in {delayMs}ms...");
            }
            catch (TaskCanceledException ex)
            {
                lastEx = ex;
                OnError?.Invoke($"PlaceOrder attempt {attempt + 1}/{maxRetries}: timeout, retrying in {delayMs}ms...");
            }
            catch (Exception ex) when (attempt < maxRetries - 1)
            {
                lastEx = ex;
                OnError?.Invoke($"PlaceOrder attempt {attempt + 1}/{maxRetries}: {ex.Message}, retrying in {delayMs}ms...");
            }

            if (attempt < maxRetries - 1)
            {
                await Task.Delay(delayMs);
                delayMs *= 2;
            }
        }

        OnError?.Invoke($"PlaceOrder FAILED after {maxRetries} attempts: {lastEx?.Message}");
        order.Status = OrderStatus.Rejected;
        return order;
    }

    public async Task<bool> CancelOrderAsync(string orderId)
    {
        EnsureConnected();
        EnsureAccountId();
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

    /// <summary>REST: equity + позиции со счёта.</summary>
    public async Task<(double equity, List<(string symbol, long qty, double avgPrice)>)> GetAccountInfoAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var account = await _restClient!.GetAccountAsync(_accountId);
        if (account == null) return (0, new());

        double equity = account.Money?.FirstOrDefault(m => m.Currency == "RUB")?.Balance
                        ?? account.Money?.Sum(m => m.Balance) ?? 0;

        var positions = (account.Positions ?? new())
            .Where(p => p.EffectiveQuantity != 0)
            .Select(p => (
                symbol: p.Symbol.Split('@')[0],
                qty: p.EffectiveQuantity,
                avgPrice: p.AveragePrice ?? p.CurrentPrice ?? 0
            )).ToList();

        return (equity, positions);
    }

    public async Task<Position[]> GetPositionsAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var account = await _restClient!.GetAccountAsync(_accountId);
        if (account?.Positions == null) return Array.Empty<Position>();

        return account.Positions
            .Where(p => p.EffectiveQuantity != 0)
            .Select(p => new Position
            {
                Ticker = p.Symbol.Split('@')[0],
                Direction = p.EffectiveQuantity > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                Entries = new List<PositionEntry>
                {
                    new() { Price = p.AveragePrice ?? p.CurrentPrice ?? 0, Volume = (int)Math.Abs(p.EffectiveQuantity), Comment = "Позиция из Финам" }
                }
            }).ToArray();
    }

    public async Task<double> GetBalanceAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var account = await _restClient!.GetAccountAsync(_accountId);
        return account?.Money?.FirstOrDefault(m => m.Currency == "RUB")?.Balance
               ?? account?.Money?.Sum(m => m.Balance) ?? 0;
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

    private static string ToSymbol(string ticker)
    {
        if (ticker.Contains('@')) return ticker;

        var futuresPrefixes = new[] { "Si", "BR", "GD", "MX", "RI", "GOLD", "ED", "Eu",
                                       "SBRF", "GAZR", "LKOH", "ROSN", "VTBR", "SNGR",
                                       "SILV", "NG", "PL", "CR", "ALRS", "MGNT" };

        foreach (var prefix in futuresPrefixes)
        {
            if (ticker.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                return $"{ticker}@RTSX";
        }

        return $"{ticker}@MISX";
    }

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

    public void Dispose()
    {
        _globalCts?.Cancel();
        _restClient?.Dispose();
    }
}
