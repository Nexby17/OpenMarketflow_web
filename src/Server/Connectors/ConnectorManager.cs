using HedgeFund.Server.Connectors;

namespace HedgeFund.Core.Connectors;

public class ConnectorManager
{
    private readonly List<string> _availableConnectors = new() { "Finam", "Transaq" };
    public IReadOnlyList<string> AvailableConnectors => _availableConnectors;
    public IConnector? Active { get; set; }
    public bool IsConnected => Active != null;
    
    public async Task DisconnectAsync()
    {
        if (Active != null)
        {
            await Active.DisconnectAsync();
            Active.Dispose();
            Active = null;
        }
    }
}
