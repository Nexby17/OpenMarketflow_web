using HedgeFund.Core.Models;

namespace HedgeFund.Core.Risk;

/// <summary>
/// Centralized risk-check helper for all Launchers and TradingService.
/// Every order path must call this before PlaceOrderAsync.
/// </summary>
public static class RiskIntegrationHelper
{
    /// <summary>
    /// Check if an order is allowed by the risk gate.
    /// Returns (approved, result). Logs denial to console.
    /// </summary>
    public static (bool approved, RiskCheckResult result) CheckBeforeOrder(
        RiskGate? riskGate,
        Order order,
        int currentLots,
        double accountEquity,
        double realisedPnl = 0,
        double unrealisedPnl = 0,
        Action<string>? logger = null)
    {
        if (riskGate == null)
        {
            // No risk gate configured - allow (for backward compat / testing)
            return (true, RiskCheckResult.Allow("No RiskGate configured"));
        }

        var result = riskGate.CheckOrder(order, currentLots, accountEquity, realisedPnl, unrealisedPnl);

        if (!result.Approved)
        {
            var msg = $"[RISK DENIED] {order.Ticker} {order.Direction} {order.Volume}x @ {order.Price:F0}: {result.Reason}";
            logger?.Invoke(msg);
            Console.WriteLine(msg);
        }

        return (result.Approved, result);
    }

    /// <summary>
    /// Check if trading is currently blocked by circuit breaker.
    /// </summary>
    public static bool IsTradingBlocked(RiskGate? riskGate)
    {
        return riskGate?.IsBlocked == true;
    }
}