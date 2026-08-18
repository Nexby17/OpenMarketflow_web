namespace HedgeFund.Server.Connectors;

public class TransaqConnectorAdapter : IConnector
{
    private bool _connected;
    
    public Task<bool> ConnectAsync(string token) 
    { 
        _connected = true;
        return Task.FromResult(true); 
    }
    
    public Task DisconnectAsync() 
    { 
        _connected = false;
        return Task.CompletedTask; 
    }
    
    public Task<bool> CancelOrderAsync(string orderId)
    {
        return Task.FromResult(true);
    }
    
    public void Dispose() { }
}
