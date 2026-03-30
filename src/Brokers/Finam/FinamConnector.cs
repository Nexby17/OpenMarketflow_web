using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Finam;

/// <summary>
/// Коннектор к Финам через Transaq Connector.
/// 
/// Transaq Connector — COM-библиотека (DLL) для работы с API Финам.
/// Протокол: XML-based (команды и ответы в XML).
/// 
/// Требует: установленный Transaq Connector + QUIK (опционально)
/// 
/// TODO:
/// - Загрузить TXmlConnector.dll
/// - Реализовать Initialize/Uninitialize
/// - Парсинг XML-ответов
/// - Команды: connect, disconnect, neworder, cancelorder
/// - Подписки: quotations, alltrades, candles
/// </summary>
public class FinamConnector : IBrokerConnector
{
    public string BrokerName => "Финам (Transaq)";
    public bool IsConnected { get; private set; }
    
    // Путь к TXmlConnector.dll
    public string ConnectorDllPath { get; set; } = @"C:\Transaq\txmlconnector64.dll";
    
    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    public async Task<bool> ConnectAsync(string login, string password)
    {
        // TODO: Transaq Connector подключение
        // 1. LoadLibrary(ConnectorDllPath)
        // 2. Initialize(logDir, logLevel)  
        // 3. SendCommand("<command id=\"connect\"><login>...</login><password>...</password><host>tr1.finam.ru</host><port>3900</port></command>")
        // 4. Парсинг ответа
        
        throw new NotImplementedException(
            "Для реализации нужен Transaq Connector DLL. " +
            "Убедись что путь к txmlconnector64.dll указан верно.");
    }

    public Task DisconnectAsync()
    {
        // TODO: SendCommand("<command id=\"disconnect\"/>")
        IsConnected = false;
        OnConnectionChanged?.Invoke(false);
        return Task.CompletedTask;
    }

    public Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        // TODO: SendCommand("<command id=\"subscribe_ticks\">...")
        throw new NotImplementedException();
    }

    public Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        // TODO: SendCommand("<command id=\"subscribe\"><quotations><security>...")
        throw new NotImplementedException();
    }

    public Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        // TODO: SendCommand("<command id=\"gethistorydata\">...")
        throw new NotImplementedException();
    }

    public Task<Order> PlaceOrderAsync(Order order)
    {
        // TODO: SendCommand("<command id=\"neworder\"><security>...</security><price>...</price><quantity>...</quantity>...")
        throw new NotImplementedException();
    }

    public Task<bool> CancelOrderAsync(string orderId)
    {
        // TODO: SendCommand("<command id=\"cancelorder\"><transactionid>...</transactionid></command>")
        throw new NotImplementedException();
    }

    public Task<Order[]> GetActiveOrdersAsync()
    {
        throw new NotImplementedException();
    }

    public Task<Position[]> GetPositionsAsync()
    {
        // TODO: Парсить <positions> из колбэка
        throw new NotImplementedException();
    }

    public Task<double> GetBalanceAsync()
    {
        throw new NotImplementedException();
    }

    public void Dispose()
    {
        // TODO: Uninitialize()
        DisconnectAsync().Wait();
    }
}
