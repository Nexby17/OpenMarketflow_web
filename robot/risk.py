"""Portfolio Risk Manager — capital protection across all strategies.

Replaces the legacy single-position RiskManager with a full portfolio-level
system that aggregates exposure from OF Scalper + Arbitrage robots and enforces
limits designed for 10,000,000 RUB capital.

Architecture:
    PortfolioRiskManager (singleton)
    ├── PerPosition limits  (per-ticker loss, size, count)
    ├── Portfolio limits    (gross/net exposure, total positions)
    ├── Risk metrics        (VaR parametric, stress-test, correlation)
    ├── Kill-switches       (daily DD, peak DD, vol regime)
    └── Reporting           (snapshot for dashboard / API)

Usage:
    from risk import PortfolioRiskManager, PositionInfo

    prm = PortfolioRiskManager(capital=10_000_000)
    prm.register_position(PositionInfo(...))
    ok, reason = prm.can_open_new_position(ticker="GAZP", exposure=2_500_000)
    ok, reason = prm.can_trade_now()
    snapshot = prm.snapshot()
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

import numpy as np

log = logging.getLogger("risk")

MSK = timezone(timedelta(hours=3))

# ═══════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════


@dataclass
class PositionInfo:
    """Single position reported by a strategy instance."""
    ticker: str                   # e.g. "GAZP", "GZM6", "SiU6"
    strategy_id: str              # e.g. "arb_gazp", "of_scalper"
    side: int                     # 1 = long, -1 = short, 0 = hedge
    quantity: int                 # lots / contracts
    exposure_rub: float           # |notional| in RUB (price * lots * multiplier)
    entry_price: float
    current_price: float
    unrealized_pnl: float         # current unrealized P&L in RUB
    daily_pnl: float              # P&L since session start
    sector: str = ""              # "oil_gas", "banking", "futures", etc.


@dataclass
class PortfolioLimits:
    """All configurable portfolio limits.

    Values below are DEFAULTS calibrated for 10,000,000 RUB capital.
    Override via constructor or hot-reload API.
    """

    # ── Per-Position Limits ──
    max_loss_per_position_rub: float = 150_000.0       # 1.5% of capital
    max_loss_per_position_pct: float = 1.5             # alt: % of capital
    max_position_size_pct: float = 35.0                # max 35% per ticker (arb legs need room)
    max_positions_per_ticker: int = 4                  # max simultaneous legs on one ticker (arb = 2 legs + roll)

    # ── Portfolio Exposure Limits ──
    max_total_exposure_pct: float = 200.0              # max 200% of capital deployed (delta-neutral arb)
    max_gross_exposure_rub: float = 20_000_000.0       # hard ceiling (delta-neutral arb has high gross)
    max_net_exposure_rub: float = 3_000_000.0          # net directional (long - short)
    max_positions_total: int = 20                      # max simultaneous positions across all robots

    # ── Correlation / Sector Limits ──
    max_correlated_exposure_pct: float = 35.0          # max 35% in correlated cluster
    max_sector_exposure_pct: float = 40.0              # max 40% in single sector

    # ── VaR Limits ──
    var_confidence_95: float = 0.95
    var_confidence_99: float = 0.99
    var_horizon_days: int = 1
    max_var_99_rub: float = 300_000.0                  # 1-day 99% VaR <= 300K (3% of capital, arb has basis risk)
    max_var_95_rub: float = 180_000.0                  # 1-day 95% VaR <= 180K (1.8%)

    # ── Kill-Switches ──
    daily_loss_limit_rub: float = 150_000.0            # stop ALL trading at -150K daily (1.5%)
    daily_loss_limit_pct: float = 1.5
    drawdown_limit_pct: float = 5.0                    # max DD from peak equity (5%)
    volatility_regime_threshold: float = 3.0           # stop if market vol > 3x historical avg

    # ── Session / Time Guards ──
    no_trade_start_hhmm: int = 2350                    # MSK
    no_trade_end_hhmm: int = 700
    clearing_start_hhmm: int = 1359                    # MOEX intermediate clearing
    clearing_end_hhmm: int = 1406
    clearing_evening_start_hhmm: int = 1858            # MOEX main clearing
    clearing_evening_end_hhmm: int = 1905

    # ── Capital ──
    capital: float = 10_000_000.0


@dataclass
class RiskSnapshot:
    """Point-in-time portfolio risk state for dashboard/API."""
    timestamp: float
    capital: float
    total_exposure: float
    gross_exposure: float
    net_exposure: float
    total_unrealized_pnl: float
    total_daily_pnl: float
    peak_equity: float
    current_drawdown_pct: float
    var_95: float
    var_99: float
    positions_count: int
    can_trade: bool
    kill_switch_reason: str
    sector_exposure: dict[str, float]
    correlated_clusters: list[dict]


# ═══════════════════════════════════════════════════════════════════
#  CORRELATION CLUSTERS (MOEX-specific)
# ═══════════════════════════════════════════════════════════════════

# Pre-defined correlation clusters for MOEX instruments.
# Based on empirical correlation of daily returns (2023-2025).
# Instruments within the same cluster are treated as correlated.

CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "oil_gas": ["ROSN", "RN", "GAZP", "GZ", "SNGS", "SN", "TATN", "TT", "LKOH", "LK"],
    "banking": ["SBER", "SR", "VTBR"],
    "metals": ["GMKN", "NLMK", "MAGN", "CHMF", "ALRS", "AL", "POLY"],
    "futures_si": ["SiU6", "SiZ6", "SiH6"],
    "futures_br": ["BR"],
    "index": ["MXI"],
}

# Cross-cluster correlation override (if known).
# If clusters are NOT in this map, their correlation = 0 (independent).
CROSS_CLUSTER_CORR: dict[tuple[str, str], float] = {
    ("oil_gas", "banking"): 0.55,    # MOEX heavyweights move together
    ("oil_gas", "metals"): 0.45,
    ("banking", "metals"): 0.40,
    ("futures_si", "oil_gas"): 0.30,  # RUB weakness = commodity strength
    ("futures_si", "banking"): 0.35,
}

# Default per-ticker annualized volatility (for VaR when no history available).
# Calibrated from MOEX 2024-2025 data.
DEFAULT_TICKER_VOL: dict[str, float] = {
    "GAZP": 0.35, "GZ": 0.35, "GZM6": 0.35,
    "SBER": 0.40, "SR": 0.40, "SRU6": 0.40,
    "ROSN": 0.32, "RN": 0.32, "RNU6": 0.32,
    "TATN": 0.36, "TT": 0.36, "TTU6": 0.36,
    "ALRS": 0.42, "AL": 0.42, "ALU6": 0.42,
    "LKOH": 0.30, "LK": 0.30, "LKU6": 0.30,
    "SiU6": 0.12, "SiZ6": 0.12,
    "BR": 0.45,
}
DEFAULT_VOL = 0.40  # 40% annualized if unknown


def _cluster_for_ticker(ticker: str) -> str:
    """Return correlation cluster name for a ticker."""
    base = ticker.split("@")[0].rstrip("0123456789HMUZV")
    for cluster, tickers in CORRELATION_CLUSTERS.items():
        if base in tickers or ticker in tickers:
            return cluster
    return "unknown"


def _vol_for_ticker(ticker: str) -> float:
    """Return annualized volatility for a ticker."""
    base = ticker.split("@")[0]
    for t, v in DEFAULT_TICKER_VOL.items():
        if base == t or base.startswith(t):
            return v
    return DEFAULT_VOL


# ═══════════════════════════════════════════════════════════════════
#  PORTFOLIO RISK MANAGER
# ═══════════════════════════════════════════════════════════════════


class PortfolioRiskManager:
    """Portfolio-level risk manager for multi-strategy trading.

    Thread-safe. Designed as a singleton shared across robot instances.
    Each robot registers its positions via `register_position()` on each tick,
    then calls `can_trade_now()` or `can_open_new_position()` before acting.

    The manager computes:
    - Aggregate exposure (gross / net / total)
    - Parametric VaR (1-day, 95% and 99%)
    - Stress-test scenarios (-5%, -10%, -15% market shock)
    - Correlation-based cluster exposure
    - Daily loss / drawdown kill-switches
    """

    # Z-scores for parametric VaR
    _Z_95 = 1.645
    _Z_99 = 2.326

    def __init__(self, capital: float = 10_000_000.0, limits: PortfolioLimits | None = None):
        self._limits = limits or PortfolioLimits()
        self._limits.capital = capital

        self._positions: list[PositionInfo] = []
        self._peak_equity: float = capital
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""
        self._killed_reason: str = ""
        self._killed_until: float = 0.0

        # Volatility regime detection
        self._market_vol_history: list[float] = []
        self._market_vol_baseline: float = 0.0

        # Historical returns for correlation (updated externally)
        self._returns_history: dict[str, list[float]] = defaultdict(list)
        self._max_returns_history = 252  # 1 year of daily returns

        self._lock = threading.RLock()

    # ═══════════════════════════════════════════════════════════════
    #  LIMITS ACCESS
    # ═══════════════════════════════════════════════════════════════

    @property
    def limits(self) -> PortfolioLimits:
        return self._limits

    def update_limits(self, **kwargs):
        """Hot-update one or more limits."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._limits, k):
                    setattr(self._limits, k, v)
                    log.info(f"Limit updated: {k} = {v}")
                else:
                    log.warning(f"Unknown limit: {k}")

    def update_capital(self, capital: float):
        """Update total capital (e.g., after deposit/withdrawal)."""
        with self._lock:
            self._limits.capital = capital
            self._limits.max_gross_exposure_rub = capital * self._limits.max_total_exposure_pct / 100.0

    # ═══════════════════════════════════════════════════════════════
    #  POSITION REGISTRATION
    # ═══════════════════════════════════════════════════════════════

    def clear_positions(self):
        """Clear all registered positions (called at start of each risk check cycle)."""
        with self._lock:
            self._positions.clear()

    def register_position(self, pos: PositionInfo):
        """Register a position for portfolio risk check.

        Called by each strategy on every tick with its current position state.
        The manager aggregates all positions for portfolio-level checks.
        """
        with self._lock:
            if pos.exposure_rub > 0 or pos.quantity > 0:
                self._positions.append(pos)

    def register_positions(self, positions: list[PositionInfo]):
        """Register multiple positions at once."""
        with self._lock:
            for p in positions:
                if p.exposure_rub > 0 or p.quantity > 0:
                    self._positions.append(p)

    # ═══════════════════════════════════════════════════════════════
    #  DAILY P&L TRACKING
    # ═══════════════════════════════════════════════════════════════

    def update_daily_pnl(self, pnl: float):
        """Update aggregate daily P&L from broker or strategy sum."""
        with self._lock:
            self._check_daily_reset()
            self._daily_pnl = pnl

    def _check_daily_reset(self):
        """Reset daily P&L at 07:00 MSK (start of trading day)."""
        now = datetime.now(MSK)
        today = now.strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            if self._daily_pnl_date:  # not first run
                log.info(f"Daily reset: date={today} prev_pnl={self._daily_pnl:.2f}")
            self._daily_pnl_date = today
            self._daily_pnl = 0.0
            self._clear_kill_switch()

    def _clear_kill_switch(self):
        """Clear daily kill-switch (called on daily reset or manual unlock)."""
        if self._killed_reason:
            log.info(f"Kill-switch cleared: was '{self._killed_reason}'")
        self._killed_reason = ""
        self._killed_until = 0.0

    def force_unlock(self):
        """Manually clear kill-switch (admin override)."""
        with self._lock:
            self._clear_kill_switch()

    # ═══════════════════════════════════════════════════════════════
    #  TIME GUARDS
    # ═══════════════════════════════════════════════════════════════

    def _msk_hhmm(self) -> int:
        now = datetime.now(MSK)
        return now.hour * 100 + now.minute

    def is_night(self) -> bool:
        """Check if within no-trade window."""
        hhmm = self._msk_hhmm()
        return hhmm >= self._limits.no_trade_start_hhmm or hhmm < self._limits.no_trade_end_hhmm

    def is_clearing(self) -> bool:
        """Check if within MOEX clearing window."""
        hhmm = self._msk_hhmm()
        if self._limits.clearing_start_hhmm <= hhmm <= self._limits.clearing_end_hhmm:
            return True
        if self._limits.clearing_evening_start_hhmm <= hhmm <= self._limits.clearing_evening_end_hhmm:
            return True
        return False

    # ═══════════════════════════════════════════════════════════════
    #  EXPOSURE CALCULATION
    # ═══════════════════════════════════════════════════════════════

    def _gross_exposure(self) -> float:
        """Sum of |exposure| across all positions."""
        return sum(abs(p.exposure_rub) for p in self._positions)

    def _net_exposure(self) -> float:
        """Net directional exposure (long - short) in RUB."""
        net = 0.0
        for p in self._positions:
            net += p.exposure_rub * p.side
        return net

    def _total_exposure_pct(self) -> float:
        """Gross exposure as % of capital."""
        if self._limits.capital <= 0:
            return float('inf')
        return self._gross_exposure() / self._limits.capital * 100.0

    def _ticker_exposure(self) -> dict[str, float]:
        """Exposure grouped by base ticker."""
        groups: dict[str, float] = defaultdict(float)
        for p in self._positions:
            base = p.ticker.split("@")[0].rstrip("0123456789HMUZV")
            groups[base] += abs(p.exposure_rub)
        return groups

    def _sector_exposure(self) -> dict[str, float]:
        """Exposure grouped by sector."""
        groups: dict[str, float] = defaultdict(float)
        for p in self._positions:
            sector = p.sector or _cluster_for_ticker(p.ticker)
            groups[sector] += abs(p.exposure_rub)
        return dict(groups)

    def _cluster_exposure(self) -> dict[str, float]:
        """Exposure grouped by correlation cluster."""
        groups: dict[str, float] = defaultdict(float)
        for p in self._positions:
            cluster = _cluster_for_ticker(p.ticker)
            groups[cluster] += abs(p.exposure_rub)
        return dict(groups)

    # ═══════════════════════════════════════════════════════════════
    #  VALUE AT RISK (Parametric)
    # ═══════════════════════════════════════════════════════════════

    def _compute_var(self) -> tuple[float, float]:
        """Compute parametric 1-day VaR (95% and 99%) for the portfolio.

        Uses individual position volatilities and assumes zero correlation
        within unknown clusters. For known clusters, applies the cross-cluster
        correlation matrix.

        Returns (var_95, var_99) in RUB.
        """
        if not self._positions:
            return 0.0, 0.0

        # Convert annual vol to daily
        daily_vol_factor = 1.0 / math.sqrt(252.0)

        # Group positions by cluster for correlation adjustment.
        # Key insight: for arb positions (long spot + short futures in same cluster),
        # the NET exposure is what matters for VaR — not gross.
        # We compute net directional exposure per cluster first, then aggregate.
        cluster_net_exp: dict[str, float] = defaultdict(float)
        cluster_gross_exp: dict[str, float] = defaultdict(float)

        for pos in self._positions:
            ticker = pos.ticker
            cluster = _cluster_for_ticker(ticker)
            cluster_net_exp[cluster] += pos.exposure_rub * pos.side
            cluster_gross_exp[cluster] += pos.exposure_rub

        # For each cluster: use net exposure for directional VaR.
        # If fully hedged (net ~ 0), use basis risk = small residual.
        # Basis risk ~ 15% of gross for typical arb pairs.
        cluster_var: dict[str, float] = defaultdict(float)
        for cluster, net_exp in cluster_net_exp.items():
            gross = cluster_gross_exp[cluster]
            # Find avg vol for this cluster
            cluster_positions = [p for p in self._positions if _cluster_for_ticker(p.ticker) == cluster]
            avg_vol = sum(_vol_for_ticker(p.ticker) for p in cluster_positions) / max(len(cluster_positions), 1)
            vol_daily = avg_vol * daily_vol_factor

            # Directional risk = net exposure * vol
            directional_var = (abs(net_exp) * vol_daily) ** 2

            # Basis risk: for hedged positions, there's residual spread risk.
            # Estimate: basis_vol ~ 20% of asset vol * gross exposure * hedge_ratio_imbalance
            if gross > 0 and abs(net_exp) < gross * 0.5:  # mostly hedged
                # Basis risk scales with gross but at much lower vol
                basis_vol = vol_daily * 0.25  # 25% of directional vol = basis vol
                basis_var = (gross * basis_vol) ** 2
            else:
                basis_var = 0.0

            cluster_var[cluster] = directional_var + basis_var

        # Combine clusters with cross-cluster correlation
        # For simplicity: assume independence across clusters unless in CROSS_CLUSTER_CORR.
        # Within a cluster: positions are correlated (use summed exposure, not sqrt of sum of squares).
        total_variance = 0.0
        clusters = list(cluster_var.keys())
        cluster_sigmas = {c: math.sqrt(v) for c, v in cluster_var.items()}

        for i, ci in enumerate(clusters):
            total_variance += cluster_var[ci]  # diagonal
            for cj in clusters[i + 1:]:
                # Off-diagonal: 2 * rho * sigma_i * sigma_j
                rho = CROSS_CLUSTER_CORR.get((ci, cj), 0.0)
                rho_sym = CROSS_CLUSTER_CORR.get((cj, ci), rho)
                total_variance += 2.0 * rho_sym * cluster_sigmas[ci] * cluster_sigmas[cj]

        portfolio_sigma = math.sqrt(max(total_variance, 0.0))

        var_95 = portfolio_sigma * self._Z_95
        var_99 = portfolio_sigma * self._Z_99

        return var_95, var_99

    def _stress_test(self) -> dict[str, float]:
        """Run stress-test scenarios.

        Returns dict with estimated portfolio loss for each shock level.
        """
        if not self._positions:
            return {"shock_5pct": 0.0, "shock_10pct": 0.0, "shock_15pct": 0.0}

        losses = {"shock_5pct": 0.0, "shock_10pct": 0.0, "shock_15pct": 0.0}
        for pos in self._positions:
            vol = _vol_for_ticker(pos.ticker)
            # Beta approximation: stock-specific shock = market_shock * (vol / market_vol)
            # Assume market_vol ~ 25% annualized (MOEX index historical avg)
            market_vol = 0.25
            beta = min(vol / market_vol, 3.0)  # cap beta at 3x

            for shock_pct, shock_key in [(0.05, "shock_5pct"), (0.10, "shock_10pct"), (0.15, "shock_15pct")]:
                pos_loss = pos.exposure_rub * shock_pct * beta * pos.side
                # For short positions, market drop = profit, so invert
                # loss = exposure * shock * (side for long: negative; short: positive)
                # If market drops by shock_pct, a long position loses shock_pct * beta * exposure
                # A short position GAINS that amount.
                # We're computing portfolio loss (negative = loss):
                pos_loss = -pos.exposure_rub * shock_pct * beta * pos.side
                losses[shock_key] += pos_loss

        return losses

    # ═══════════════════════════════════════════════════════════════
    #  DRAWDOWN TRACKING
    # ═══════════════════════════════════════════════════════════════

    def update_equity(self, current_equity: float):
        """Update current portfolio equity for drawdown tracking.

        Called with broker-reported total equity (cash + positions value).
        """
        with self._lock:
            if current_equity > self._peak_equity:
                self._peak_equity = current_equity

    def _current_drawdown_pct(self) -> float:
        """Current drawdown from peak equity in %."""
        if self._peak_equity <= 0:
            return 0.0
        # Use daily P&L as proxy if equity not updated
        current = self._peak_equity + self._daily_pnl
        if current >= self._peak_equity:
            return 0.0
        return (self._peak_equity - current) / self._peak_equity * 100.0

    # ═══════════════════════════════════════════════════════════════
    #  VOLATILITY REGIME FILTER
    # ═══════════════════════════════════════════════════════════════

    def update_market_volatility(self, current_vol: float, baseline_vol: float):
        """Update market volatility readings for regime detection.

        Args:
            current_vol: Current realized volatility (e.g., 20-day annualized).
            baseline_vol: Long-term average volatility (e.g., 90-day annualized).
        """
        with self._lock:
            self._market_vol_history.append(current_vol)
            if len(self._market_vol_history) > 60:
                self._market_vol_history.pop(0)
            self._market_vol_baseline = baseline_vol

    def _volatility_regime_breached(self) -> bool:
        """Check if market volatility exceeds threshold x baseline."""
        if self._market_vol_baseline <= 0 or self._limits.volatility_regime_threshold <= 0:
            return False
        current = self._market_vol_history[-1] if self._market_vol_history else 0.0
        ratio = current / self._market_vol_baseline
        if ratio > self._limits.volatility_regime_threshold:
            log.warning(
                f"Volatility regime breach: current={current:.2%} "
                f"baseline={self._market_vol_baseline:.2%} ratio={ratio:.1f}x "
                f"threshold={self._limits.volatility_regime_threshold:.1f}x"
            )
            return True
        return False

    # ═══════════════════════════════════════════════════════════════
    #  KILL-SWITCH CHECKS
    # ═══════════════════════════════════════════════════════════════

    def _check_kill_switches(self) -> tuple[bool, str]:
        """Check all kill-switch conditions.

        Returns (is_killed, reason). If killed, ALL trading must stop.
        """
        # Already killed?
        if self._killed_reason and time.time() < self._killed_until:
            return True, self._killed_reason

        # Daily reset clears kill-switch
        self._check_daily_reset()
        if self._killed_reason and time.time() >= self._killed_until:
            self._clear_kill_switch()

        # ── Daily loss limit ──
        daily_limit = max(
            self._limits.daily_loss_limit_rub,
            self._limits.capital * self._limits.daily_loss_limit_pct / 100.0
        )
        if self._daily_pnl <= -daily_limit:
            reason = f"Daily loss limit: {self._daily_pnl:.0f} <= -{daily_limit:.0f}"
            self._activate_kill_switch(reason, until_next_day=True)
            return True, reason

        # ── Drawdown limit ──
        dd = self._current_drawdown_pct()
        if dd >= self._limits.drawdown_limit_pct:
            reason = f"Drawdown limit: {dd:.2f}% >= {self._limits.drawdown_limit_pct:.1f}%"
            self._activate_kill_switch(reason, until_next_day=True)
            return True, reason

        # ── Volatility regime ──
        if self._volatility_regime_breached():
            reason = f"Volatility regime: market vol > {self._limits.volatility_regime_threshold:.1f}x baseline"
            # Don't kill for full day -- just 30 min cooldown
            self._activate_kill_switch(reason, duration_sec=1800)
            return True, reason

        return False, ""

    def _activate_kill_switch(self, reason: str, until_next_day: bool = False, duration_sec: float = 0):
        """Activate kill-switch."""
        with self._lock:
            self._killed_reason = reason
            if until_next_day:
                # Kill until 07:00 MSK tomorrow
                tomorrow = datetime.now(MSK) + timedelta(days=1)
                kill_until = tomorrow.replace(hour=7, minute=0, second=0, microsecond=0)
                self._killed_until = kill_until.timestamp()
            else:
                self._killed_until = time.time() + duration_sec
            log.critical(f"KILL-SWITCH ACTIVATED: {reason} (until {datetime.fromtimestamp(self._killed_until, MSK):%H:%M:%S} MSK)")

    @property
    def is_killed(self) -> bool:
        """Check if kill-switch is currently active."""
        killed, _ = self._check_kill_switches()
        return killed

    @property
    def kill_reason(self) -> str:
        return self._killed_reason

    # ═══════════════════════════════════════════════════════════════
    #  MAIN CHECK FUNCTIONS (called by robots)
    # ═══════════════════════════════════════════════════════════════

    def can_trade_now(self) -> tuple[bool, str]:
        """Check if trading is allowed right now (time + kill-switches).

        This does NOT check position limits -- use can_open_new_position() for that.
        """
        # Kill-switch
        killed, reason = self._check_kill_switches()
        if killed:
            return False, reason

        # Time guards
        if self.is_night():
            return False, f"Night mode ({self._msk_hhmm():04d} MSK)"

        if self.is_clearing():
            return False, f"Clearing ({self._msk_hhmm():04d} MSK)"

        return True, "OK"

    def can_open_new_position(self, ticker: str, exposure_rub: float, side: int = 1) -> tuple[bool, str]:
        """Check if a new position can be opened without breaching limits.

        Args:
            ticker: Base ticker (e.g. "GAZP")
            exposure_rub: Planned notional exposure in RUB
            side: 1 for long, -1 for short

        Returns (allowed, reason).
        """
        with self._lock:
            # ── Time + Kill-switch ──
            ok, reason = self.can_trade_now()
            if not ok:
                return False, reason

            base = ticker.split("@")[0].rstrip("0123456789HMUZV")

            # ── Per-ticker position count ──
            ticker_positions = sum(1 for p in self._positions if base in p.ticker)
            if ticker_positions >= self._limits.max_positions_per_ticker:
                return False, f"Max positions per ticker ({base}): {ticker_positions} >= {self._limits.max_positions_per_ticker}"

            # ── Per-ticker exposure ──
            ticker_exposure = sum(abs(p.exposure_rub) for p in self._positions if base in p.ticker)
            new_ticker_exposure = ticker_exposure + exposure_rub
            max_ticker_exposure = self._limits.capital * self._limits.max_position_size_pct / 100.0
            if new_ticker_exposure > max_ticker_exposure:
                return False, f"Ticker {base} exposure: {new_ticker_exposure:,.0f} > {max_ticker_exposure:,.0f} ({self._limits.max_position_size_pct}%)"

            # ── Total positions count ──
            if len(self._positions) >= self._limits.max_positions_total:
                return False, f"Max positions total: {len(self._positions)} >= {self._limits.max_positions_total}"

            # ── Gross exposure ──
            new_gross = self._gross_exposure() + exposure_rub
            if new_gross > self._limits.max_gross_exposure_rub:
                return False, f"Gross exposure: {new_gross:,.0f} > {self._limits.max_gross_exposure_rub:,.0f}"

            # ── Net exposure ──
            new_net = abs(self._net_exposure() + exposure_rub * side)
            if new_net > self._limits.max_net_exposure_rub:
                return False, f"Net exposure: {new_net:,.0f} > {self._limits.max_net_exposure_rub:,.0f}"

            # ── Total exposure % ──
            new_total_pct = new_gross / self._limits.capital * 100.0
            if new_total_pct > self._limits.max_total_exposure_pct:
                return False, f"Total exposure: {new_total_pct:.1f}% > {self._limits.max_total_exposure_pct:.1f}%"

            # ── Sector exposure ──
            sector = _cluster_for_ticker(ticker)
            sector_exp = sum(abs(p.exposure_rub) for p in self._positions if _cluster_for_ticker(p.ticker) == sector)
            new_sector_exp = sector_exp + exposure_rub
            max_sector = self._limits.capital * self._limits.max_sector_exposure_pct / 100.0
            if new_sector_exp > max_sector:
                return False, f"Sector '{sector}' exposure: {new_sector_exp:,.0f} > {max_sector:,.0f} ({self._limits.max_sector_exposure_pct}%)"

            # ── Correlated cluster exposure ──
            cluster = _cluster_for_ticker(ticker)
            cluster_exp = sum(abs(p.exposure_rub) for p in self._positions if _cluster_for_ticker(p.ticker) == cluster)
            new_cluster_exp = cluster_exp + exposure_rub
            max_cluster = self._limits.capital * self._limits.max_correlated_exposure_pct / 100.0
            if new_cluster_exp > max_cluster:
                return False, f"Correlated cluster '{cluster}' exposure: {new_cluster_exp:,.0f} > {max_cluster:,.0f} ({self._limits.max_correlated_exposure_pct}%)"

            # ── VaR check (with hypothetical new position) ──
            # Temporarily add position, compute VaR, then remove
            temp_pos = PositionInfo(
                ticker=ticker, strategy_id="_check", side=side,
                quantity=0, exposure_rub=exposure_rub,
                entry_price=0, current_price=0,
                unrealized_pnl=0, daily_pnl=0,
            )
            self._positions.append(temp_pos)
            var_95, var_99 = self._compute_var()
            self._positions.pop()

            if var_99 > self._limits.max_var_99_rub:
                return False, f"VaR 99% with new position: {var_99:,.0f} > {self._limits.max_var_99_rub:,.0f}"

            return True, "OK"

    def check_position_loss(self, ticker: str, unrealized_pnl: float) -> tuple[bool, str]:
        """Check if a single position has breached its loss limit.

        Returns (ok, reason). If not ok, position must be closed.
        """
        max_loss = max(
            self._limits.max_loss_per_position_rub,
            self._limits.capital * self._limits.max_loss_per_position_pct / 100.0
        )
        if unrealized_pnl <= -max_loss:
            return False, f"Position {ticker} loss limit: {unrealized_pnl:.0f} <= -{max_loss:.0f}"
        return True, "OK"

    # ═══════════════════════════════════════════════════════════════
    #  SNAPSHOT / REPORTING
    # ═══════════════════════════════════════════════════════════════

    def snapshot(self) -> RiskSnapshot:
        """Generate a full risk snapshot for dashboard / API."""
        with self._lock:
            var_95, var_99 = self._compute_var()
            stress = self._stress_test()
            can_trade, reason = self.can_trade_now()

            return RiskSnapshot(
                timestamp=time.time(),
                capital=self._limits.capital,
                total_exposure=self._gross_exposure(),
                gross_exposure=self._gross_exposure(),
                net_exposure=self._net_exposure(),
                total_unrealized_pnl=sum(p.unrealized_pnl for p in self._positions),
                total_daily_pnl=self._daily_pnl,
                peak_equity=self._peak_equity,
                current_drawdown_pct=self._current_drawdown_pct(),
                var_95=round(var_95, 2),
                var_99=round(var_99, 2),
                positions_count=len(self._positions),
                can_trade=can_trade,
                kill_switch_reason=reason,
                sector_exposure=self._sector_exposure(),
                correlated_clusters=[
                    {"cluster": c, "exposure": round(e, 2), "limit_pct": self._limits.max_correlated_exposure_pct}
                    for c, e in self._cluster_exposure().items()
                ],
            )

    def get_status(self) -> dict:
        """Get status dict for JSON API."""
        s = self.snapshot()
        stress = self._stress_test()
        return {
            "timestamp": datetime.fromtimestamp(s.timestamp, MSK).isoformat(),
            "capital": s.capital,
            "exposure": {
                "gross": round(s.gross_exposure, 2),
                "net": round(s.net_exposure, 2),
                "total_pct": round(s.gross_exposure / s.capital * 100, 2) if s.capital > 0 else 0,
            },
            "pnl": {
                "unrealized": round(s.total_unrealized_pnl, 2),
                "daily": round(s.total_daily_pnl, 2),
                "daily_limit": round(-self._limits.daily_loss_limit_rub, 2),
                "daily_remaining": round(-self._limits.daily_loss_limit_rub - s.total_daily_pnl, 2),
            },
            "drawdown": {
                "current_pct": round(s.current_drawdown_pct, 2),
                "limit_pct": self._limits.drawdown_limit_pct,
                "peak_equity": round(s.peak_equity, 2),
            },
            "var": {
                "95_1d": s.var_95,
                "99_1d": s.var_99,
                "limit_99": self._limits.max_var_99_rub,
                "breached": s.var_99 > self._limits.max_var_99_rub,
            },
            "stress_test": {k: round(v, 2) for k, v in stress.items()},
            "positions": {
                "count": s.positions_count,
                "max": self._limits.max_positions_total,
            },
            "can_trade": s.can_trade,
            "kill_switch": s.kill_switch_reason,
            "sector_exposure": {k: round(v, 2) for k, v in s.sector_exposure.items()},
            "correlated_clusters": s.correlated_clusters,
            "time": {
                "msk_hhmm": f"{self._msk_hhmm():04d}",
                "is_night": self.is_night(),
                "is_clearing": self.is_clearing(),
            },
            "limits": {
                "max_loss_per_position": self._limits.max_loss_per_position_rub,
                "max_position_size_pct": self._limits.max_position_size_pct,
                "max_total_exposure_pct": self._limits.max_total_exposure_pct,
                "max_net_exposure": self._limits.max_net_exposure_rub,
                "daily_loss_limit": self._limits.daily_loss_limit_rub,
                "drawdown_limit_pct": self._limits.drawdown_limit_pct,
                "volatility_regime_threshold": self._limits.volatility_regime_threshold,
            },
        }

    # ═══════════════════════════════════════════════════════════════
    #  LEGACY COMPAT (for main.py)
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> tuple[bool, str]:
        """Legacy compat: same as can_trade_now()."""
        return self.can_trade_now()

    def check_pnl(self, unrealized_pnl: float) -> tuple[bool, str]:
        """Legacy compat: check P&L against per-position limit."""
        return self.check_position_loss("legacy", unrealized_pnl)

    def check_lots(self, lots: int) -> tuple[bool, str]:
        """Legacy compat: check lot count (maps to max_positions_total)."""
        with self._lock:
            if len(self._positions) + lots > self._limits.max_positions_total:
                return False, f"Too many positions: {len(self._positions)} + {lots} > {self._limits.max_positions_total}"
        return True, "OK"

    def is_clearing_now(self) -> bool:
        """Legacy compat."""
        return self.is_clearing()


# ═══════════════════════════════════════════════════════════════════
#  FACTORY: Singleton for multi-robot access
# ═══════════════════════════════════════════════════════════════════

_INSTANCE: PortfolioRiskManager | None = None
_INSTANCE_LOCK = threading.Lock()


def get_risk_manager(capital: float | None = None) -> PortfolioRiskManager:
    """Get or create the singleton PortfolioRiskManager.

    On first call, optionally set the capital. Subsequent calls ignore
    the capital parameter and return the existing instance.
    """
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = PortfolioRiskManager(capital=capital or 10_000_000.0)
        elif capital is not None:
            _INSTANCE.update_capital(capital)
        return _INSTANCE


# ═══════════════════════════════════════════════════════════════════
#  DOCUMENTATION: Parameter Justification for 10M RUB Capital
# ═══════════════════════════════════════════════════════════════════
#
# ── Per-Position Limits ──
#
# max_loss_per_position_rub = 150,000 RUB (1.5%)
#   Rationale: At 10M, losing 1.5% on a single position is recoverable
#   in 2-3 profitable arb trades. Beyond this, position should be closed
#   and the signal re-evaluated. Based on MOEX daily volatility: GAZP
#   moves ~2.2% daily (1-sigma), so 1.5% stop ~ 0.68-sigma -- catches
#   tail events.
#
# max_position_size_pct = 35% (3.5M RUB)
#   Rationale: Arbitrage legs need significant size. GAZP allocation
#   = 3M per leg. 35% per ticker allows this while preventing
#   over-concentration in a single instrument.
#   The per-position LOSS limit (150K) provides the real downside cap.
#
# max_positions_per_ticker = 3
#   Rationale: Arb needs 2 legs (spot + futures). Allow 1 spare for
#   pyramiding/rolling. OF scalper gets 1 position per ticker.
#
# ── Portfolio Exposure ──
#
# max_total_exposure_pct = 200% (20M RUB gross)
#   Rationale: Delta-neutral arbitrage has gross = 2x capital per pair.
#   5 pairs x 2 legs = 10 positions with offsetting exposure.
#   Effective directional risk is on NET, not gross. 200% gross allows
#   full deployment while keeping 50%+ margin buffer.

# max_gross_exposure_rub = 20,000,000 RUB
#   Hard ceiling = capital x 200% (allows 5 delta-neutral arb pairs).
#   Individual leg exposures are monitored via max_position_size_pct.
#
# max_net_exposure_rub = 3,000,000 RUB (30%)
#   Rationale: Max directional risk. Arb strategies should be near-zero
#   net. OF scalper may hold up to ~500K directional. 3M ceiling
#   allows some directional bias while limiting tail risk.
#
# max_positions_total = 20
#   Rationale: 5 arb strategies x 2 legs = 10 positions + OF scalper
#   (max 16 sub-lots, counted as 1 position) = 11. Allow 20 for
#   flexibility.
#
# ── Correlation Limits ──
#
# max_correlated_exposure_pct = 35% (3.5M RUB)
#   Rationale: oil_gas cluster (ROSN + GAZP + TATN) has ~60% pairwise
#   correlation. If all three move against the portfolio simultaneously,
#   35% at 60% correlation = effective 21% independent risk.
#   This is aggressive but manageable given arb is delta-neutral.
#
# max_sector_exposure_pct = 40% (4M RUB)
#   Rationale: MOEX is sector-concentrated (oil_gas dominates).
#   40% allows meaningful participation while preventing total
#   sector collapse from destroying the portfolio.
#
# ── VaR Limits ──
#
# max_var_99_rub = 300,000 RUB (3% of capital)
#   Rationale: 1-day 99% VaR of 3% reflects basis risk in 5 simultaneous
#   arb pairs. For pure directional strategies, 2% would be the limit,
#   but delta-neutral arb carries lower tail risk (stress test confirms
#   ~0 loss in market crash scenarios). Basis risk is the real exposure.
#   Industry standard for hedge funds: 1-3% daily VaR.
#
# max_var_95_rub = 180,000 RUB (1.8%)
#   Monitoring threshold -- if 95% VaR exceeds this, alert but don't stop.
#
# ── Kill-Switches ──
#
# daily_loss_limit_rub = 150,000 RUB (1.5%)
#   Rationale: Stop trading for the day after losing 1.5%.
#   Industry rule: daily stop = 50% of monthly stop = 50% of 3% = 1.5%.
#   Forces emotional reset and prevents revenge trading.
#
# drawdown_limit_pct = 5%
#   Rationale: Max peak-to-trough drawdown before halting.
#   At 5% of 10M = 500K. Recovery from 5% DD requires +5.3% gain.
#   From 10% DD: needs +11.1%. From 20%: needs +25%.
#   5% keeps recovery manageable.
#
# volatility_regime_threshold = 3.0x
#   Rationale: If current market vol > 3x historical baseline, something
#   is wrong (geopolitical event, circuit breaker, crash).
#   Historical examples: Feb 2022 (vol spiked to 5x), Mar 2020 (4x).
#   3x threshold catches these while allowing normal elevated vol.
#   Cooldown: 30 min (not full day -- re-evaluate after shock passes).
#
# ── SBER Recommendation ──
#
# PROBLEM: SBER arb MaxDD = -195K at 1.5M allocation = 13% drawdown.
#   This is 2.6x the portfolio daily_loss_limit (1.5%).
#   If SBER has a bad day while other strategies are flat, the portfolio
#   daily stop triggers, shutting down ALL robots.
#
# RECOMMENDATION: Reduce SBER allocation from 1.5M to 1.0M (10%).
#   New MaxDD/capital ratio: 195K / 10M = 1.95% (manageable).
#   New MaxDD/allocation ratio: 195K / 1M = 19.5% (still high but
#   arb is hedged -- the DD includes temporary unrealized, not realized).
#   The freed 500K can go to GAZP (best risk-adjusted after TATN).
#
#   ALTERNATIVE: If walk-forward degradation continues (fold #4 OOS
#   was only +3.1%), consider pausing SBER entirely for 1 quarter
#   and reallocating: GAZP -> 3M (30%), TATN -> 3M (30%).
#
# ── Revised Allocation ──
#
#   Ticker    Old Alloc   New Alloc   Rationale
#   ───────   ─────────   ─────────   ─────────
#   ROSN      3.0M (30%)  3.0M (30%)  No change -- stable, best performer
#   TATN      2.5M (25%)  2.5M (25%)  No change -- best risk-adjusted
#   GAZP      2.5M (25%)  3.0M (30%)  +0.5M from SBER -- high liquidity, 0 roll losses
#   SBER      1.5M (15%)  1.0M (10%)  -0.5M -- high DD, degrading OOS performance
#   ALRS      0.5M (5%)   0.5M (5%)   No change -- small, safe, 100% WR
#   ───────   ─────────   ─────────
#   TOTAL    10.0M (100%) 10.0M (100%)
#
# ═══════════════════════════════════════════════════════════════════
