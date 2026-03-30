using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// Коннектор Финам через новый Trade API v1 (REST).
/// Host: api.finam.ru
/// Auth: Bearer JWT token
/// </summary>
public class FinamConnector : IBrokerConnector
{
    private FinamApiClient? _client;
    private string _accountId = string.Empty;
    private readonly Dictionary<string, Action<Candle>> _candleSubscriptions = new();
    private CancellationTokenSource? _pollCts;

    public string BrokerName => "Финам (Trade API)";
    public bool IsConnected { get; private set; }

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    /// <summary>
    /// Подключение. login = JWT token, password = account_id (опционально).
    /// </summary>
    public async Task<bool> ConnectAsync(string token, string accountId = "")
    {
        try
        {
            _client = new FinamApiClient(token);

            // Проверяем подключение
            var clock = await _client.GetClockAsync();
            if (clock == null)
            {
                OnError?.Invoke("Не удалось получить время сервера");
                return false;
            }

            // Получаем счета
            if (string.IsNullOrEmpty(accountId))
            {
                var accounts = await _client.GetAccountsAsync();
                if (accounts?.Accounts.Count > 0)
                {
                    _accountId = accounts.Accounts[0].AccountId;
                }
                else
                {
                    OnError?.Invoke("Нет доступных торговых счетов. Используйте торговый токен.");
                    // Продолжаем работу — для просмотровых токенов
                }
            }
            else
            {
                _accountId = accountId;
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
        _pollCts?.Cancel();
        _client?.Dispose();
        _client = null;
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        return Task.CompletedTask;
    }

    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        EnsureConnected();
        _candleSubscriptions[ticker] = onCandle;

        // Polling свечей (REST не поддерживает стриминг, нужен gRPC для этого)
        // Пока используем polling каждые N секунд
        _pollCts = new CancellationTokenSource();
        _ = Task.Run(async () =>
        {
            while (!_pollCts.Token.IsCancellationRequested)
            {
                try
                {
                    var tf = TimeframeToString(timeframe);
                    var to = DateTime.UtcNow.ToString("o");
                    var from = DateTime.UtcNow.Subtract(timeframe * 2).ToString("o");

                    var bars = await _client!.GetBarsAsync($"{ticker}@MISX", tf, from, to);
                    if (bars?.Bars.Count > 0)
                    {
                        var lastBar = bars.Bars[^1];
                        var candle = BarToCandle(lastBar);
                        onCandle(candle);
                    }
                }
                catch (Exception ex)
                {
                    OnError?.Invoke($"Ошибка получения свечей {ticker}: {ex.Message}");
                }

                await Task.Delay(timeframe < TimeSpan.FromMinutes(5) 
                    ? TimeSpan.FromSeconds(10) 
                    : TimeSpan.FromSeconds(30), _pollCts.Token);
            }
        }, _pollCts.Token);
    }

    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        // Требует gRPC стриминг (SubscribeOrderBook)
        // Для REST пока не реализовано
        OnError?.Invoke("Level2 подписка требует gRPC. Пока не реализовано.");
        return Task.CompletedTask;
    }

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        EnsureConnected();

        var tf = TimeframeToString(timeframe);
        var bars = await _client!.GetBarsAsync(
            $"{ticker}@MISX", tf,
            from.ToString("o"), to.ToString("o"));

        if (bars?.Bars == null) return Array.Empty<Candle>();

        return bars.Bars.Select(BarToCandle).ToArray();
    }

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        EnsureConnected();
        EnsureAccountId();

        var request = new PlaceOrderRequest
        {
            AccountId = _accountId,
            Symbol = $"{order.Ticker}@MISX",
            Side = order.Direction == SignalDirection.Buy ? "SIDE_BUY" : "SIDE_SELL",
            Quantity = order.Volume,
            OrderType = order.Type == OrderType.Market ? "ORDER_TYPE_MARKET" : "ORDER_TYPE_LIMIT",
            Price = order.Type == OrderType.Limit ? order.Price : null
        };

        var result = await _client!.PlaceOrderAsync(request);
        order.BrokerOrderId = result?.OrderId ?? "";
        order.Status = OrderStatus.Active;
        
        OnOrderUpdate?.Invoke(order);
        return order;
    }

    public async Task<bool> CancelOrderAsync(string orderId)
    {
        EnsureConnected();
        EnsureAccountId();

        var result = await _client!.CancelOrderAsync(_accountId, orderId);
        return result != null;
    }

    public async Task<Order[]> GetActiveOrdersAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var result = await _client!.GetOrdersAsync(_accountId);
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

        var account = await _client!.GetAccountAsync(_accountId);
        if (account?.Positions == null) return Array.Empty<Position>();

        return account.Positions.Select(p => new Position
        {
            Ticker = p.Symbol.Split('@')[0],
            Direction = p.Balance > 0 ? SignalDirection.Buy : SignalDirection.Sell,
            Entries = new List<PositionEntry>
            {
                new()
                {
                    Price = p.AveragePrice,
                    Volume = (int)Math.Abs(p.Balance),
                    Comment = "Позиция из Финам"
                }
            }
        }).ToArray();
    }

    public async Task<double> GetBalanceAsync()
    {
        EnsureConnected();
        EnsureAccountId();

        var account = await _client!.GetAccountAsync(_accountId);
        return account?.Money.FirstOrDefault(m => m.Currency == "RUB")?.Balance ?? 0;
    }

    // === Helpers ===

    private void EnsureConnected()
    {
        if (!IsConnected || _client == null)
            throw new InvalidOperationException("Не подключен к Финам API");
    }

    private void EnsureAccountId()
    {
        if (string.IsNullOrEmpty(_accountId))
            throw new InvalidOperationException("Торговый счёт не привязан. Используйте торговый токен.");
    }

    private static string TimeframeToString(TimeSpan tf) => tf.TotalMinutes switch
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
        "ORDER_STATUS_ACTIVE" => OrderStatus.Active,
        "ORDER_STATUS_FILLED" => OrderStatus.Filled,
        "ORDER_STATUS_CANCELLED" => OrderStatus.Cancelled,
        "ORDER_STATUS_REJECTED" => OrderStatus.Rejected,
        _ => OrderStatus.Pending
    };

    public void Dispose()
    {
        _pollCts?.Cancel();
        _client?.Dispose();
    }
}
