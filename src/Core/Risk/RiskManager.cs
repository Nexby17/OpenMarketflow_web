namespace HedgeFund.Core.Risk;

/// <summary>
/// Risk Manager — динамический sizing по волатильности.
/// Адаптировано из virattt/ai-hedge-fund Risk Manager.
/// </summary>
public static class RiskManager
{
    /// <summary>
    /// Рассчитать макс лотов на основе портфеля и волатильности.
    /// </summary>
    public static int CalculateMaxLots(
        double portfolioValue,
        double currentPrice,
        double goPerLot = 12000.0,
        double dailyVol = 0.005,
        double maxRiskPct = 0.25)
    {
        // Волатильность-адаптивный лимит
        double annualizedVol = dailyVol * Math.Sqrt(252);
        double volLimit = annualizedVol switch
        {
            < 0.15 => 0.25,  // низкая волатильность — 25% портфеля
            < 0.30 => 0.20 - (annualizedVol - 0.15) * 0.5,
            < 0.50 => 0.15 - (annualizedVol - 0.30) * 0.5,
            _ => 0.10       // высокая волатильность — 10%
        };
        volLimit = Math.Clamp(volLimit, 0.10, 0.25);

        double maxPositionValue = portfolioValue * Math.Min(volLimit, maxRiskPct);
        int lotsByGo = (int)(maxPositionValue / goPerLot);
        int maxLots = Math.Max(1, lotsByGo);

        return maxLots;
    }

    /// <summary>
    /// Рассчитать дневную волатильность из массива цен.
    /// </summary>
    public static double CalculateDailyVol(double[] prices, int lookback = 60)
    {
        if (prices.Length < 2) return 0.005;

        int start = Math.Max(0, prices.Length - lookback);
        int count = prices.Length - start - 1;
        if (count < 5) return 0.005;

        double sumSq = 0;
        for (int i = start + 1; i < prices.Length; i++)
        {
            double ret = (prices[i] - prices[i - 1]) / prices[i - 1];
            sumSq += ret * ret;
        }

        return Math.Sqrt(sumSq / count);
    }

    /// <summary>
    /// Оценить волатильность из последней свечи (high-low range).
    /// Быстрая оценка, не требует истории.
    /// </summary>
    public static double EstimateVolFromRange(double high, double low, double close)
    {
        if (close <= 0) return 0.005;
        double range = (high - low) / close;
        return range / (2 * 0.886); // Parkinson approximation
    }
}
