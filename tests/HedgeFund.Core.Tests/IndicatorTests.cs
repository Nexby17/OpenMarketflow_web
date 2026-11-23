using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;
using Xunit;

namespace HedgeFund.Core.Tests;

public class IndicatorTests
{
    [Fact]
    public void Test_EMA_CalculatesCorrectly()
    {
        var ema = new EMA(30);

        // Feed 30 identical bars to warm up
        for (int i = 0; i < 30; i++)
        {
            ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });
        }

        // After 30 bars of 100, EMA should be close to 100
        Assert.True(Math.Abs(ema.Value - 100.0) < 0.01, $"EMA should be ~100, got {ema.Value}");

        // Feed 100 next
        ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });
        ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });
        ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });
        ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });
        ema.Update(new Candle { Timestamp = DateTime.UtcNow, Open = 100, High = 100, Low = 100, Close = 100, Volume = 1000 });

        // After 5 more updates, EMA (30) with multiplier 2/(30+1)~0.0645 should converge slower
        // With all values == 100, EMA remains 100
        Assert.True(Math.Abs(ema.Value - 100.0) < 0.01, $"EMA should stay ~100, got {ema.Value}");
    }

    [Fact]
    public void Test_ParabolicSAR_BasicFlow()
    {
        var psar = new ParabolicSAR(0.02, 0.02, 0.2);

        // First candle — NaN (needs 2+)
        psar.Update(new Candle { Open = 100, High = 105, Low = 95, Close = 100 });

        // Second candle — should produce SAR value (uptrend by default)
        double val = psar.Update(new Candle { Open = 101, High = 108, Low = 99, Close = 102 });
        Assert.False(double.IsNaN(val), "SAR should not be NaN after 2 candles");
        Assert.Equal(1, psar.Trend); // Default uptrend

        // Continue in uptrend
        for (int i = 0; i < 5; i++)
        {
            psar.Update(new Candle { Open = 102, High = 110 + i, Low = 101, Close = 105 + i });
        }
        Assert.Equal(1, psar.Trend);
    }

    [Fact]
    public void Test_BollingerBands_Calculates()
    {
        var bb = new BollingerBands(period: 20, stdDevMultiplier: 2.0);

        // Feed 20 bars at 100
        for (int i = 0; i < 20; i++)
        {
            bb.Update(new Candle { Open = 100, High = 100, Low = 100, Close = 100 });
        }

        // All values are 100, so stdDev = 0 → bands = 100
        Assert.True(Math.Abs(bb.Middle - 100.0) < 0.01, $"Middle should be ~100, got {bb.Middle}");
        Assert.True(Math.Abs(bb.Upper - 100.0) < 0.01, $"Upper should be ~100, got {bb.Upper}");
        Assert.True(Math.Abs(bb.Lower - 100.0) < 0.01, $"Lower should be ~100, got {bb.Lower}");

        // Feed a high candle to create variance
        bb.Update(new Candle { Open = 100, High = 110, Low = 100, Close = 110 }); // dequeues first bar at 100

        // Now most values are 100, one is 110 → there's some variance
        Assert.True(bb.Middle > 100.0, $"Middle should be > 100, got {bb.Middle}");
        Assert.True(bb.Upper > bb.Middle, $"Upper ({bb.Upper}) should be > Middle ({bb.Middle})");
        Assert.True(bb.Lower < bb.Middle, $"Lower ({bb.Lower}) should be < Middle ({bb.Middle})");
    }
}
