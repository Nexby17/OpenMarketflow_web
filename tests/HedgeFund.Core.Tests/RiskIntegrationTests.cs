using HedgeFund.Core.Models;
using HedgeFund.Core.Risk;
using Xunit;

namespace HedgeFund.Core.Tests;

/// <summary>
/// Integration tests for RiskGate + CircuitBreaker integration.
/// Verifies that risk checks block orders when limits are exceeded.
/// </summary>
public class RiskIntegrationTests
{
    private Order CreateOrder(string ticker = "SBER", SignalDirection dir = SignalDirection.Buy, double price = 270.0, int volume = 1)
    {
        return new Order
        {
            Ticker = ticker,
            Direction = dir,
            Price = price,
            Volume = volume,
            Timestamp = DateTime.UtcNow,
        };
    }

    [Fact]
    public void Test_RiskIntegrationHelper_AllowsWhenNoRiskGate()
    {
        // No RiskGate configured = backward compat, allow
        var (approved, result) = RiskIntegrationHelper.CheckBeforeOrder(
            null, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.True(approved);
    }

    [Fact]
    public void Test_RiskIntegrationHelper_AllowsValidOrder()
    {
        var gate = new RiskGate(goPerLot: 12000, maxLots: 5, dailyLossLimit: 5000);
        var (approved, result) = RiskIntegrationHelper.CheckBeforeOrder(
            gate, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.True(approved);
    }

    [Fact]
    public void Test_RiskIntegrationHelper_DeniesWhenBlocked()
    {
        var gate = new RiskGate(maxLots: 10);
        gate.BlockTrading("Test circuit breaker trip");

        // After BlockTrading, ALL orders must be denied
        var (approved, result) = RiskIntegrationHelper.CheckBeforeOrder(
            gate, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.False(approved);
        Assert.Contains("BLOCKED", result.Reason);
    }

    [Fact]
    public void Test_RiskIntegrationHelper_IsTradingBlocked()
    {
        var gate = new RiskGate();
        Assert.False(RiskIntegrationHelper.IsTradingBlocked(gate));
        
        gate.BlockTrading("Test");
        Assert.True(RiskIntegrationHelper.IsTradingBlocked(gate));
        
        gate.UnblockTrading();
        Assert.False(RiskIntegrationHelper.IsTradingBlocked(gate));
    }

    [Fact]
    public void Test_CircuitBreaker_Trip_BlocksAllOrders()
    {
        var gate = new RiskGate(maxLots: 10);
        var cb = new CircuitBreaker(
            gate,
            dailyLossLimit: 5000,
            emergencyCloseAll: () => Task.CompletedTask,
            cancelAllOrders: () => Task.CompletedTask);

        // Before trip: orders allowed
        var (approvedBefore, _) = RiskIntegrationHelper.CheckBeforeOrder(
            gate, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.True(approvedBefore);

        // Trigger circuit breaker
        var tripped = cb.UpdatePnLAsync(realisedPnL: -6000, unrealisedPnL: 0, accountEquity: 100000).Result;
        Assert.True(tripped);
        Assert.True(gate.IsBlocked);

        // After trip: ALL orders denied
        var (approvedAfter, resultAfter) = RiskIntegrationHelper.CheckBeforeOrder(
            gate, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.False(approvedAfter);
        Assert.Contains("BLOCKED", resultAfter.Reason);
    }

    [Fact]
    public void Test_CheckOrder_BlockedFailsFast()
    {
        // When blocked, CheckOrder should deny immediately without checking other fields
        var gate = new RiskGate(maxLots: 10);
        gate.BlockTrading("Emergency");

        // Even with insufficient equity AND max lots exceeded AND duplicate,
        // the result should contain the BLOCKED message (checked first)
        var order = CreateOrder(volume: 100);
        var result = gate.CheckOrder(order, currentLots: 100, accountEquity: 1);
        Assert.False(result.Approved);
        Assert.Contains("BLOCKED", result.Reason);
    }

    [Fact]
    public void Test_UnblockTrading_RestoresOrderFlow()
    {
        var gate = new RiskGate(maxLots: 10);
        gate.BlockTrading("Test");
        Assert.True(RiskIntegrationHelper.IsTradingBlocked(gate));

        gate.UnblockTrading();
        
        var (approved, _) = RiskIntegrationHelper.CheckBeforeOrder(
            gate, CreateOrder(), currentLots: 0, accountEquity: 100000);
        Assert.True(approved);
    }
}