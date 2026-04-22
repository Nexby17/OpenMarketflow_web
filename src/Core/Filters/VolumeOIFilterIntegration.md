# Volume OI Filter Integration Guide

## Overview

The `VolumeOIFilter` provides position sizing recommendations based on Volume and Open Interest analysis. It can be integrated into existing strategies like Grid MM to reduce risk during trend continuation phases.

## Integration Steps

### 1. Add Filter to Strategy

```csharp
using HedgeFund.Core.Filters;

public class GridMmRegimeStrategy : IStrategy
{
    // Add filter instance
    private readonly VolumeOIFilter _volOiFilter;

    public GridMmRegimeStrategy(Config config)
    {
        // ... existing initialization ...

        // Initialize Volume OI Filter
        _volOiFilter = new VolumeOIFilter(new VolumeOIFilter.Config
        {
            VolumeWindow = 50,
            VolumeSpikeThreshold = 2.5,
            OiWindow = 50,
            OiDivergenceThreshold = 0.05,
            EmaFast = 20,
            EmaSlow = 200,
            ReducedSizeRatio = 0.5
        });
    }
}
```

### 2. Update Filter on Each Bar

```csharp
public Signal? OnBar(Bar bar)
{
    // Update Volume OI Filter
    var filterResult = _volOiFilter.Update(
        price: bar.Close,
        volume: bar.Volume,
        openInterest: bar.OpenInterest // 0 if not available
    );

    // Log filter result
    if (filterResult.Recommendation != VolumeOIFilter.FilterRecommendation.FULL)
    {
        Logger.Info($"Volume OI Filter: {filterResult.Recommendation} - {filterResult.Reason}");
    }

    // Pause trading if recommended
    if (filterResult.Recommendation == VolumeOIFilter.FilterRecommendation.PAUSE)
    {
        return null; // Skip this bar
    }

    // ... existing strategy logic ...
}
```

### 3. Apply Position Size Multiplier

```csharp
// When opening new positions
private void OpenGridLevels(double entryPrice, int direction)
{
    // Get current filter recommendation
    var filterResult = _volOiFilter.Update(entryPrice, 0, 0); // or use current bar data

    // Calculate adjusted lot size
    int baseLots = GetCurrentLotLevel(); // your existing dynamic lot logic
    int adjustedLots = (int)Math.Round(baseLots * filterResult.PositionSizeMultiplier);

    // Ensure at least 1 lot
    adjustedLots = Math.Max(1, adjustedLots);

    Logger.Info($"Opening grid: base={baseLots}, adjusted={adjustedLots}, multiplier={filterResult.PositionSizeMultiplier:F2}");

    // Use adjustedLots when placing orders
    for (int i = 1; i <= _config.MaxGridLevels; i++)
    {
        double levelPrice = entryPrice + (direction * i * _config.GridStep);
        PlaceLimitOrder(levelPrice, adjustedLots);
    }
}
```

### 4. Export Filter State (Optional)

```csharp
public Dictionary<string, object> GetState()
{
    var state = new Dictionary<string, object>
    {
        // ... existing state ...
    };

    // Add filter state
    var (emaFast, emaSlow, volSma, oiSma) = _volOiFilter.GetIndicators();
    state["volOif_emaFast"] = emaFast;
    state["volOif_emaSlow"] = emaSlow;
    state["volOif_volSma"] = volSma;
    state["volOif_oiSma"] = oiSma;
    state["volOif_trend"] = _volOiFilter.GetTrend();

    return state;
}
```

## Filter Behavior

### FULL Recommendation (1.0x multiplier)
- Normal market conditions
- OI divergence detected (exhaustion signal)
- Use full position size

### REDUCED Recommendation (0.5x multiplier)
- Volume spike without OI divergence
- Indicates trend continuation
- Reduce position size by 50%

### PAUSE Recommendation (0.0x multiplier)
- Extreme volume spike (>5x average)
- Skip trading this bar
- Wait for conditions to normalize

## Data Requirements

### Required Data
- **Price**: Close price (always available)
- **Volume**: Trading volume (usually available)

### Optional Data
- **Open Interest**: Currently not in dataset
  - When available: significantly improves filter accuracy
  - When unavailable: filter uses volume-only logic

### Getting OI Data

**Option 1: MOEX API**
```csharp
// Add to data downloader
// https://iss.moex.com/iss/engines/futures/markets/forts/securities/<ticker>/openinterest.json
```

**Option 2: FINAM API**
```python
# Add to Python downloader
# Check if OI field is available in FINAM data
```

## Testing the Integration

### 1. Unit Test
```csharp
[TestMethod]
public void TestVolumeOIFilter_ReducedSize()
{
    var filter = new VolumeOIFilter();
    var result = filter.Update(price: 100.0, volume: 2500.0, openInterest: 0);

    // Volume spike (2.5x) without OI divergence should give REDUCED
    Assert.AreEqual(VolumeOIFilter.FilterRecommendation.REDUCED, result.Recommendation);
    Assert.AreEqual(0.5, result.PositionSizeMultiplier);
}
```

### 2. Backtest Comparison
Run backtest with and without filter:
- Without filter: baseline performance
- With filter: should reduce drawdown during trending markets
- Expected: lower PnL but also lower risk

### 3. Paper Trading
Test in paper trading mode:
- Monitor filter recommendations
- Verify position sizing adjustments
- Check log messages for filter activity

## Monitoring

### Key Metrics to Track
1. **Filter hit rate**: How often does REDUCED/PAUSE trigger?
2. **PnL impact**: Difference in PnL with vs without filter
3. **Drawdown reduction**: Max DD with vs without filter
4. **False positive rate**: REDUCED when market actually reverses

### Log Examples
```
[INFO] Volume OI Filter: REDUCED - Volume spike (3.2x) in uptrend without OI divergence - reduced size
[INFO] Opening grid: base=3, adjusted=1, multiplier=0.50
[INFO] Volume OI Filter: FULL - OI divergence detected (-6.2%), exhaustion signal - full size
[INFO] Volume OI Filter: PAUSE - Extreme volume spike (>5x), pausing
```

## Troubleshooting

### Issue: Filter always returns FULL
**Cause**: Volume data is 0 or not available
**Fix**: Check data feed, ensure volume field is populated

### Issue: Filter always returns PAUSE
**Cause**: VolumeSpikeThreshold too low
**Fix**: Increase threshold (try 3.0x instead of 2.5x)

### Issue: No OI data available
**Cause**: Data provider doesn't include OI
**Fix**: Filter works without OI, but consider:
1. Switch to data provider with OI
2. Use proxy indicators (volume * price)
3. Accept volume-only logic (less accurate but functional)

## Next Steps

1. **Add OI data download**: Update data downloader to fetch OI from MOEX/FINAM
2. **Backtest with filter**: Run comparison backtests
3. **Optimize parameters**: Tune thresholds based on results
4. **Deploy to production**: Enable in live trading with monitoring
5. **Monitor performance**: Track filter effectiveness over time

## References

- Original strategy: `GridMmRegimeStrategy.cs`
- Filter implementation: `VolumeOIFilter.cs`
- Backtest results: `backtest/results/volume_divergence_*.csv`
- Visualization: `backtest/src/volume_oi_viz.py`
