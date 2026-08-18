using HedgeFund.Core.Models;

namespace HedgeFund.Core.Risk;

/// <summary>
/// CircuitBreaker — daily stop-loss protection.
/// Monitors daily PnL (realised + unrealised). When daily loss exceeds the limit,
/// performs EmergencyStop: close all positions, cancel all orders, block trading.
/// Auto-resets at 07:00 MSK (start of trading day).
/// </summary>
public class CircuitBreaker
{
    private readonly double _dailyLossLimit;
    private readonly RiskGate _riskGate;
    private Func<Task>? _emergencyCloseAll;
    private Func<Task>? _cancelAllOrders;

    private double _dailyStartEquity;
    private double _dailyRealisedPnL;
    private double _dailyUnrealisedPnL;
    private DateTime _lastResetDate = DateTime.MinValue;
    private readonly object _lock = new();

    /// <summary>
    /// True when trading is blocked due to circuit breaker trip.
    /// </summary>
    public bool IsBlocked
    {
        get
        {
            lock (_lock)
            {
                EnsureDailyReset();
                return _riskGate.IsBlocked;
            }
        }
    }

    /// <summary>
    /// Total daily PnL (realised + unrealised).
    /// </summary>
    public double DailyPnL
    {
        get
        {
            lock (_lock)
            {
                return _dailyRealisedPnL + _dailyUnrealisedPnL;
            }
        }
    }

    /// <summary>
    /// Constructor.
    /// </summary>
    /// <param name="riskGate">The associated RiskGate for blocking/unblocking trading.</param>
    /// <param name="dailyLossLimit">Max daily loss in rubles before trip.</param>
    /// <param name="emergencyCloseAll">Async callback: close all positions (market orders).</param>
    /// <param name="cancelAllOrders">Async callback: cancel all active orders.</param>
    public CircuitBreaker(
        RiskGate riskGate,
        double dailyLossLimit = 5000.0,
        Func<Task>? emergencyCloseAll = null,
        Func<Task>? cancelAllOrders = null)
    {
        _riskGate = riskGate;
        _dailyLossLimit = dailyLossLimit;
        _emergencyCloseAll = emergencyCloseAll;
        _cancelAllOrders = cancelAllOrders;
    }

    /// <summary>
    /// Set callbacks after construction.
    /// </summary>
    public void SetCallbacks(Func<Task> emergencyCloseAll, Func<Task> cancelAllOrders)
    {
        _emergencyCloseAll = emergencyCloseAll;
        _cancelAllOrders = cancelAllOrders;
    }

    /// <summary>
    /// Reset daily counter at 07:00 MSK (04:00 UTC). Also unblocks trading.
    /// </summary>
    private void EnsureDailyReset()
    {
        var msk = DateTime.UtcNow.AddHours(3);
        var today = msk.Date;
        if (msk.Hour < 7)
            today = today.AddDays(-1);

        if (today != _lastResetDate)
        {
            _lastResetDate = today;
            _dailyStartEquity = 0;
            _dailyRealisedPnL = 0;
            _dailyUnrealisedPnL = 0;
            _riskGate.UnblockTrading();
            Console.WriteLine($"[CircuitBreaker] Daily reset: {today:yyyy-MM-dd} 07:00 MSK");
        }
    }

    /// <summary>
    /// Update the tracked PnL values. Called by launchers periodically.
    /// If total daily loss exceeds limit, trigger EmergencyStop.
    /// </summary>
    /// <param name="realisedPnL">Realised PnL for today.</param>
    /// <param name="unrealisedPnL">Current unrealised PnL.</param>
    /// <param name="accountEquity">Current account equity.</param>
    /// <returns>True if circuit breaker tripped (emergency stop executed).</returns>
    public async Task<bool> UpdatePnLAsync(double realisedPnL, double unrealisedPnL, double accountEquity)
    {
        lock (_lock)
        {
            EnsureDailyReset();

            _dailyRealisedPnL = realisedPnL;
            _dailyUnrealisedPnL = unrealisedPnL;

            if (_dailyStartEquity == 0 && accountEquity > 0)
                _dailyStartEquity = accountEquity;
        }

        double totalPnL = realisedPnL + unrealisedPnL;
        if (totalPnL <= -_dailyLossLimit && !_riskGate.IsBlocked)
        {
            Console.WriteLine($"[CircuitBreaker] TRIPPED! Daily loss {totalPnL:F0} <= -{_dailyLossLimit:F0}");
            await EmergencyStopAsync($"Daily loss limit hit: {totalPnL:F0} <= -{_dailyLossLimit:F0}");
            return true;
        }

        return false;
    }

    /// <summary>
    /// Emergency stop: close all positions, cancel all orders, block trading.
    /// </summary>
    public async Task EmergencyStopAsync(string reason)
    {
        Console.WriteLine($"[CircuitBreaker] EMERGENCY STOP: {reason}");

        if (_cancelAllOrders != null)
        {
            try { await _cancelAllOrders(); } catch (Exception ex) { Console.WriteLine($"[CircuitBreaker] Cancel orders error: {ex.Message}"); }
        }

        if (_emergencyCloseAll != null)
        {
            try { await _emergencyCloseAll(); } catch (Exception ex) { Console.WriteLine($"[CircuitBreaker] Close all error: {ex.Message}"); }
        }

        _riskGate.BlockTrading(reason);
        Console.WriteLine($"[CircuitBreaker] Emergency stop complete. Trading blocked until 07:00 MSK.");
    }
}
