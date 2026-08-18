using HedgeFund.Core.Averaging;
using HedgeFund.Core.Models;
using Xunit;

namespace HedgeFund.Core.Tests;

public class AveragingEngineTests
{
    private AveragingSettings CreateSettings(int maxAveragingCount = 5, bool stopLossEnabled = false, double maxPositionRub = 0)
    {
        return new AveragingSettings
        {
            Enabled = true,
            Mode = AveragingMode.Fixed,
            BaseLotSize = 1,
            MaxAveragingCount = maxAveragingCount,
            StopLossEnabled = stopLossEnabled,
            MinProfitToClose = 0,
            MaxPositionRub = maxPositionRub,
        };
    }

    private Position CreatePosition(string ticker = "SBER", SignalDirection dir = SignalDirection.Buy, double avgPrice = 270.0, int lots = 1)
    {
        return new Position
        {
            Ticker = ticker,
            Direction = dir,
            Entries = new List<PositionEntry>
            {
                new PositionEntry { Price = avgPrice, Volume = lots, Timestamp = DateTime.UtcNow }
            }
        };
    }

    [Fact]
    public void Test_HoldWhenNoPosition()
    {
        var engine = new AveragingEngine(CreateSettings());
        var position = new Position { Ticker = "SBER", Direction = SignalDirection.Buy };

        var result = engine.Evaluate(position, currentPrice: 270.0, hasNewSignal: true);
        Assert.Equal(AveragingDecision.Hold, result.Decision);
    }

    [Fact]
    public void Test_CloseAllWhenInProfit()
    {
        var engine = new AveragingEngine(CreateSettings());
        var position = CreatePosition("SBER", SignalDirection.Buy, avgPrice: 270.0, lots: 1);

        var result = engine.Evaluate(position, currentPrice: 275.0, hasNewSignal: false);
        Assert.Equal(AveragingDecision.CloseAll, result.Decision);
        Assert.Equal(1, result.Volume);
    }

    [Fact]
    public void Test_AverageWhenInLossAndSignal()
    {
        var engine = new AveragingEngine(CreateSettings(maxAveragingCount: 5));
        var position = CreatePosition("SBER", SignalDirection.Buy, avgPrice: 270.0, lots: 1);

        var result = engine.Evaluate(position, currentPrice: 250.0, hasNewSignal: true);
        Assert.Equal(AveragingDecision.Average, result.Decision);
        Assert.Equal(1, result.Volume);
    }

    [Fact]
    public void Test_KillSwitch_MaxPositionRub()
    {
        // MaxPositionRub = 200, position value = abs(250 * 1) = 250 → kill-switch
        var engine = new AveragingEngine(CreateSettings(maxPositionRub: 200.0));
        var position = CreatePosition("SBER", SignalDirection.Buy, avgPrice: 260.0, lots: 1);

        var result = engine.Evaluate(position, currentPrice: 250.0, hasNewSignal: true);
        Assert.Equal(AveragingDecision.Hold, result.Decision);
        Assert.Contains("Kill-switch", result.Reason);
    }
}
