using HedgeFund.Core.Models;

namespace HedgeFund.Core.Risk;

/// <summary>
/// Risk check result for a pre-order validation.
/// </summary>
public class RiskCheckResult
{
    public bool Approved { get; set; } = true;
    public string Reason { get; set; } = string.Empty;
    public List<string> Violations { get; set; } = new();

    public static RiskCheckResult Allow(string reason = "")
    {
        return new RiskCheckResult { Approved = true, Reason = reason };
    }

    public static RiskCheckResult Deny(string reason, params string[] violations)
    {
        return new RiskCheckResult
        {
            Approved = false,
            Reason = reason,
            Violations = violations.Where(v => !string.IsNullOrEmpty(v)).ToList()
        };
    }
}

/// <summary>
/// RiskGate — validates order requests against risk limits before execution.
/// Called by every Launcher before PlaceOrderAsync.
/// </summary>
public class RiskGate
{
    private readonly double _safetyMarginFactor;
    private readonly double _goPerLot;
    private readonly int _maxLots;
    private readonly double _dailyLossLimit;
    private readonly double _maxDrawdownPct;
    private readonly object _lock = new();

    // Duplicate detection: (ticker, direction, price) -> last order timestamp
    private readonly Dictionary<(string ticker, SignalDirection dir, double priceRounded), DateTime> _lastOrderTime = new();
    private static readonly TimeSpan DuplicateWindow = TimeSpan.FromSeconds(3);

    // Daily PnL tracking
    private double _dailyStartEquity;
    private double _peakEquity;
    private DateTime _lastResetDate = DateTime.MinValue;

    /// <summary>
    /// Set to true when trading is blocked (circuit breaker tripped or daily stop hit).
    /// </summary>
    public bool IsBlocked { get; private set; }

    public RiskGate(
        double goPerLot = 12000.0,
        double safetyMarginFactor = 1.2,
        int maxLots = 1,
        double dailyLossLimit = 5000.0,
        double maxDrawdownPct = 0.05)
    {
        _goPerLot = goPerLot;
        _safetyMarginFactor = safetyMarginFactor;
        _maxLots = maxLots;
        _dailyLossLimit = dailyLossLimit;
        _maxDrawdownPct = maxDrawdownPct;
    }

    /// <summary>
    /// Reset daily counter at 07:00 MSK (04:00 UTC). Called on each check.
    /// </summary>
    private void EnsureDailyReset()
    {
        var msk = DateTime.UtcNow.AddHours(3);
        var today = msk.Date;
        if (msk.Hour < 7)
            today = today.AddDays(-1); // Still previous trading day

        if (today != _lastResetDate)
        {
            _lastResetDate = today;
            _lastOrderTime.Clear();
            Console.WriteLine($"[RiskGate] Daily reset: {today:yyyy-MM-dd}");
        }
    }

    /// <summary>
    /// Initialize equity baseline for the day. Called when account equity is first known.
    /// </summary>
    public void SetEquityBaseline(double equity)
    {
        lock (_lock)
        {
            EnsureDailyReset();
            if (_dailyStartEquity == 0 || equity > _peakEquity)
            {
                _dailyStartEquity = _dailyStartEquity == 0 ? equity : _dailyStartEquity;
                _peakEquity = Math.Max(_peakEquity, equity);
            }
        }
    }

    /// <summary>
    /// Main risk check. Called before placing any order.
    /// </summary>
    /// <param name="order">The order request to validate.</param>
    /// <param name="currentLots">Current open lots for this ticker.</param>
    /// <param name="accountEquity">Current account equity in rubles.</param>
    /// <param name="realisedPnl">Realised PnL for the day.</param>
    /// <param name="unrealisedPnl">Unrealised PnL for the day.</param>
    /// <returns>RiskCheckResult with Approved/Denied.</returns>
    public RiskCheckResult CheckOrder(
        Order order,
        int currentLots,
        double accountEquity,
        double realisedPnl = 0,
        double unrealisedPnl = 0)
    {
        lock (_lock)
        {
            EnsureDailyReset();

            // 0. Circuit breaker check - block ALL orders when trading is halted
            if (IsBlocked)
            {
                return RiskCheckResult.Deny("Trading is BLOCKED by circuit breaker. Order rejected.",
                    "Circuit breaker active - all trading halted until 07:00 MSK reset");
            }

            // Track equity peak for drawdown calculation
            if (accountEquity > _peakEquity)
                _peakEquity = accountEquity;
            if (_dailyStartEquity == 0)
                _dailyStartEquity = accountEquity;

            var violations = new List<string>();

            // 1. Equity check: accountEquity >= GO_per_lot * totalLots * safetyMarginFactor
            double requiredEquity = _goPerLot * (currentLots + order.Volume) * _safetyMarginFactor;
            if (accountEquity < requiredEquity)
            {
                violations.Add($"Insufficient equity: {accountEquity:F0} < {requiredEquity:F0} required for {currentLots + order.Volume} lots");
            }

            // 2. Max position check
            if (currentLots + order.Volume > _maxLots)
            {
                violations.Add($"Max position exceeded: {currentLots + order.Volume} > {_maxLots} lots");
            }

            // 3. Duplicate detection
            var key = (order.Ticker, order.Direction, Math.Round(order.Price, 0));
            if (_lastOrderTime.TryGetValue(key, out var lastTime))
            {
                if (DateTime.UtcNow - lastTime < DuplicateWindow)
                {
                    violations.Add($"Duplicate order: {order.Ticker} {order.Direction} @ {order.Price:F0} within {DuplicateWindow.TotalSeconds}s");
                }
            }
            _lastOrderTime[key] = DateTime.UtcNow;

            // Clean old entries
            var cutoff = DateTime.UtcNow - TimeSpan.FromMinutes(1);
            foreach (var k in _lastOrderTime.Keys.ToList())
            {
                if (_lastOrderTime[k] < cutoff)
                    _lastOrderTime.Remove(k);
            }

            // 4. Daily loss check
            double totalDailyPnL = realisedPnl + unrealisedPnl;
            if (totalDailyPnL <= -_dailyLossLimit)
            {
                violations.Add($"Daily loss limit hit: PnL={totalDailyPnL:F0} <= -{_dailyLossLimit:F0}");
            }

            // 5. Max drawdown check
            if (_peakEquity > 0)
            {
                double drawdown = (_peakEquity - accountEquity) / _peakEquity;
                if (drawdown >= _maxDrawdownPct)
                {
                    violations.Add($"Max drawdown exceeded: {drawdown:P2} >= {_maxDrawdownPct:P2} (peak={_peakEquity:F0}, current={accountEquity:F0})");
                }
            }

            if (violations.Count > 0)
            {
                return RiskCheckResult.Deny(string.Join("; ", violations), violations.ToArray());
            }

            return RiskCheckResult.Allow();
        }
    }

    /// <summary>
    /// Block trading (called by circuit breaker on emergency stop).
    /// </summary>
    public void BlockTrading(string reason)
    {
        lock (_lock)
        {
            IsBlocked = true;
            Console.WriteLine($"[RiskGate] Trading BLOCKED: {reason}");
        }
    }

    /// <summary>
    /// Unblock trading (called on daily reset).
    /// </summary>
    public void UnblockTrading()
    {
        lock (_lock)
        {
            IsBlocked = false;
            _peakEquity = 0;
            _dailyStartEquity = 0;
            Console.WriteLine($"[RiskGate] Trading UNBLOCKED");
        }
    }
}
