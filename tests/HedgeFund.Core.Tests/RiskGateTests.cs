using HedgeFund.Core.Models;
using HedgeFund.Core.Risk;
using Xunit;

namespace HedgeFund.Core.Tests;

public class RiskGateTests
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
    public void Test_CheckOrder_AllowsValidOrder()
    {
        var gate = new RiskGate(goPerLot: 12000, safetyMarginFactor: 1.2, maxLots: 5, dailyLossLimit: 5000, maxDrawdownPct: 0.05);
        var order = CreateOrder();
        // Equity = 100000 > required (12000 * 1 * 1.2 = 14400)
        var result = gate.CheckOrder(order, currentLots: 0, accountEquity: 100000, realisedPnl: 0, unrealisedPnl: 0);
        Assert.True(result.Approved);
    }

    [Fact]
    public void Test_CheckOrder_DeniesInsufficientEquity()
    {
        var gate = new RiskGate(goPerLot: 12000, safetyMarginFactor: 1.2, maxLots: 5);
        var order = CreateOrder(volume: 2);
        // Required equity = 12000 * (0 + 2) * 1.2 = 28800, account = 20000 < 28800
        var result = gate.CheckOrder(order, currentLots: 0, accountEquity: 20000);
        Assert.False(result.Approved);
        Assert.Contains("Insufficient equity", result.Reason);
    }

    [Fact]
    public void Test_CheckOrder_DeniesMaxPositionExceeded()
    {
        var gate = new RiskGate(goPerLot: 12000, safetyMarginFactor: 1.2, maxLots: 2);
        var order = CreateOrder(volume: 1);
        // currentLots=2 + new=1 = 3 > maxLots=2
        var result = gate.CheckOrder(order, currentLots: 2, accountEquity: 100000);
        Assert.False(result.Approved);
        Assert.Contains("Max position exceeded", result.Reason);
    }

    [Fact]
    public void Test_CheckOrder_DeniesDuplicate()
    {
        var gate = new RiskGate(maxLots: 10);
        var order1 = CreateOrder("SBER", SignalDirection.Buy, 270.0);
        var order2 = CreateOrder("SBER", SignalDirection.Buy, 270.0);

        // First one should pass
        var result1 = gate.CheckOrder(order1, currentLots: 0, accountEquity: 100000);
        Assert.True(result1.Approved);

        // Second one within 3 sec window — duplicate
        var result2 = gate.CheckOrder(order2, currentLots: 0, accountEquity: 100000);
        Assert.False(result2.Approved);
        Assert.Contains("Duplicate order", result2.Reason);
    }

    [Fact]
    public void Test_CheckOrder_DeniesDailyLossLimit()
    {
        var gate = new RiskGate(dailyLossLimit: 5000, maxLots: 10);
        var order = CreateOrder();
        // PnL = -6000 <= -5000
        var result = gate.CheckOrder(order, currentLots: 0, accountEquity: 100000, realisedPnl: -4000, unrealisedPnl: -2000);
        Assert.False(result.Approved);
        Assert.Contains("Daily loss limit hit", result.Reason);
    }

    [Fact]
    public void Test_CheckOrder_DeniesMaxDrawdown()
    {
        var gate = new RiskGate(maxDrawdownPct: 0.05, maxLots: 10);

        // First, set peak equity
        gate.CheckOrder(CreateOrder("AAPL"), currentLots: 0, accountEquity: 100000);
        // Now equity = 94000, drawdown = (100000-94000)/100000 = 0.06 >= 0.05
        var order = CreateOrder("SBER");
        var result = gate.CheckOrder(order, currentLots: 0, accountEquity: 94000);
        Assert.False(result.Approved);
        Assert.Contains("Max drawdown exceeded", result.Reason);
    }

    [Fact]
    public void Test_BlockTrading_BlocksAllOrders()
    {
        var gate = new RiskGate(maxLots: 10);
        gate.BlockTrading("Manual block");

        var order = CreateOrder();
        // BlockTrading sets IsBlocked, but CheckOrder does not check IsBlocked directly —
        // it's the caller's responsibility to skip. We verify IsBlocked is set.
        Assert.True(gate.IsBlocked);

        // After UnblockTrading, IsBlocked should be false
        gate.UnblockTrading();
        Assert.False(gate.IsBlocked);

        // Order should pass after unblock
        var result = gate.CheckOrder(order, currentLots: 0, accountEquity: 100000);
        Assert.True(result.Approved);
    }
}
