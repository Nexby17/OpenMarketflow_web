namespace HedgeFund.Server.Connectors;

public interface IConnector : IDisposable
{
    Task<bool> ConnectAsync(string token);
    Task DisconnectAsync();
    Task<bool> CancelOrderAsync(string orderId);
}
