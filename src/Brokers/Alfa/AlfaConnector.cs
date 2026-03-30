using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Alfa;

/// <summary>
/// Коннектор к Альфа-Инвестиции PRO API.
/// 
/// PRO API работает через систему каналов (Channel) и роутер.
/// Протокол: protobuf-based, подписки через Bus/Responder.
/// 
/// Требует: запущенный терминал Альфа-Директ 4.0
/// Документация: https://alfabank.servicecdn.ru/alfadirect/files/Alfa-Investments-Pro-API.pdf
/// 
/// TODO: 
/// - Реализовать подключение к роутеру через TCP
/// - Подписка на каналы маркетдаты (#Data.*)
/// - Отправка заявок через #Order.*
/// - Получение позиций через #Portfolio.*
/// </summary>
public class AlfaConnector : IBrokerConnector
{
    public string BrokerName => "Альфа-Инвестиции";
    public bool IsConnected { get; private set; }

    // Настройки подключения к роутеру PRO API
    public string RouterHost { get; set; } = "localhost";
    public int RouterPort { get; set; } = 15100; // порт по умолчанию
    
    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    public async Task<bool> ConnectAsync(string login, string password)
    {
        // TODO: Подключение к роутеру Альфа PRO API
        // 1. TCP-соединение с RouterHost:RouterPort
        // 2. Аутентификация через каналы
        // 3. Подписка на системные каналы
        
        throw new NotImplementedException(
            "Для реализации требуется protobuf-схема из документации Альфа PRO API. " +
            "Скачай PDF и извлеки .proto файлы.");
    }

    public Task DisconnectAsync()
    {
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        return Task.CompletedTask;
    }

    public Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        // TODO: Подписка на канал #Data.Candle.{ticker}
        throw new NotImplementedException();
    }

    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        // TODO: Подписка на канал #Data.Level2.{ticker}
        throw new NotImplementedException();
    }

    public Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        // TODO: Запрос через #Data.Query (AllTradeEntity и т.д.)
        throw new NotImplementedException();
    }

    public Task<Order> PlaceOrderAsync(Order order)
    {
        // TODO: Отправка заявки через #Order.Place
        throw new NotImplementedException();
    }

    public Task<bool> CancelOrderAsync(string orderId)
    {
        // TODO: Снятие заявки через #Order.Cancel
        throw new NotImplementedException();
    }

    public Task<Order[]> GetActiveOrdersAsync()
    {
        throw new NotImplementedException();
    }

    public Task<Position[]> GetPositionsAsync()
    {
        // TODO: Запрос через #Portfolio.Positions
        throw new NotImplementedException();
    }

    public Task<double> GetBalanceAsync()
    {
        throw new NotImplementedException();
    }

    public void Dispose()
    {
        DisconnectAsync().Wait();
    }
}
