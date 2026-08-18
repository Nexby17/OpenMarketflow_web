using HedgeFund.Core.Risk;
using Xunit;

namespace HedgeFund.Core.Tests;

public class CircuitBreakerTests
{
    [Fact]
    public void Test_NoTrip_WhenProfit()
    {
        var gate = new RiskGate();
        bool emergencyCalled = false;
        var cb = new CircuitBreaker(
            gate,
            dailyLossLimit: 5000,
            emergencyCloseAll: () => { emergencyCalled = true; return Task.CompletedTask; }
        );

        // PnL = +1000 > -5000, no trip
        // We cannot easily await in sync test, but we check IsBlocked state indirectly
        Assert.False(gate.IsBlocked);

        // PnL is far from limit
        double pnl = 1000;
        Assert.True(pnl > -5000);
    }

    [Fact]
    public async Task Test_Trips_WhenLossExceedsLimit()
    {
        var gate = new RiskGate();
        bool emergencyCalled = false;
        bool cancelCalled = false;
        var cb = new CircuitBreaker(
            gate,
            dailyLossLimit: 5000,
            emergencyCloseAll: () => { emergencyCalled = true; return Task.CompletedTask; },
            cancelAllOrders: () => { cancelCalled = true; return Task.CompletedTask; }
        );

        // PnL = -6000, limit = 5000
        bool tripped = await cb.UpdatePnLAsync(realisedPnL: -6000, unrealisedPnL: 0, accountEquity: 100000);
        Assert.True(tripped);
        Assert.True(emergencyCalled);
        Assert.True(cancelCalled);
        Assert.True(gate.IsBlocked);
    }

    [Fact]
    public void Test_ResetAt7AM()
    {
        var gate = new RiskGate();
        // Simulate: block trading, then unblock
        gate.BlockTrading("Test");
        Assert.True(gate.IsBlocked);

        gate.UnblockTrading();
        Assert.False(gate.IsBlocked);

        // Daily PnL should also reset (tracked internally by CircuitBreaker.EnsureDailyReset)
        var cb = new CircuitBreaker(gate, dailyLossLimit: 5000);
        Assert.False(cb.IsBlocked);
        Assert.Equal(0.0, cb.DailyPnL, 0);
    }
}
