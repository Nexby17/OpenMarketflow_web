"""Order Flow engine — real-time delta/CVD/OB metrics + signal generation."""
import logging
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import numpy as np

log = logging.getLogger("orderflow")

MSK = timezone(timedelta(hours=3))

# Trade side constants (from Finam protobuf)
SIDE_BUY = 1
SIDE_SELL = 2


@dataclass
class Trade:
    """Single executed trade (обезличенная сделка)."""
    price: float
    size: int
    side: int          # SIDE_BUY or SIDE_SELL
    timestamp: float   # unix epoch


@dataclass
class BarMetrics:
    """Calculated metrics for a completed bar."""
    buy_volume: int = 0
    sell_volume: int = 0
    delta: int = 0
    delta_ratio: float = 0.0
    total_volume: int = 0
    price_change: float = 0.0
    cvd: float = 0.0
    timestamp: float = 0.0


@dataclass
class OBMetrics:
    """Current order book snapshot metrics."""
    imbalance: float = 0.0
    total_buy: int = 0
    total_sell: int = 0
    wall_price: float = 0.0
    wall_side: int = 0       # 1=buy wall, -1=sell wall
    wall_size: int = 0
    near_buy: int = 0        # lots within radius of mid
    near_sell: int = 0
    timestamp: float = 0.0


@dataclass
class OFSignal:
    """Generated signal."""
    signal_type: str        # 'absorption', 'cvd_divergence', 'ob_imbalance'
    direction: int          # 1=LONG, -1=SHORT
    strength: float = 0.0   # 0..1
    price: float = 0.0
    timestamp: float = 0.0


class TradeCollector:
    """Collects individual trades and aggregates into bar-level metrics."""

    def __init__(self):
        self._trades: list[Trade] = []
        self._lock = threading.Lock()
        self._bar_start_ts: float = 0.0
        self._bar_end_ts: float = 0.0
        self._bar_open: float = 0.0

        # CVD state
        self._cvd: float = 0.0
        self._last_cvd_reset_day: int = -1

    def add_trade(self, trade: Trade):
        with self._lock:
            self._trades.append(trade)

    def reset_daily_cvd(self):
        """Reset CVD — call at 07:00 MSK daily."""
        with self._lock:
            self._cvd = 0.0
            log.info("CVD reset (daily 07:00 MSK)")

    @property
    def cvd(self) -> float:
        return self._cvd

    def check_daily_reset(self, now: datetime):
        """Auto-reset CVD at 07:00 MSK."""
        msk = now.astimezone(MSK)
        today = msk.date()
        # Reset if it's past 07:00 MSK and we haven't reset today
        if msk.hour >= 7 and self._last_cvd_reset_day != today.toordinal():
            self._last_cvd_reset_day = today.toordinal()
            self.reset_daily_cvd()

    def close_bar(self, bar_open: float, bar_close: float, bar_start_ts: float, bar_end_ts: float) -> BarMetrics:
        """Aggregate trades within [bar_start_ts, bar_end_ts) into BarMetrics."""
        with self._lock:
            buy_vol = 0
            sell_vol = 0
            for t in self._trades:
                if bar_start_ts <= t.timestamp < bar_end_ts:
                    if t.side == SIDE_BUY:
                        buy_vol += t.size
                    elif t.side == SIDE_SELL:
                        sell_vol += t.size

            delta = buy_vol - sell_vol
            total = buy_vol + sell_vol
            delta_ratio = delta / total if total > 0 else 0.0
            price_change = bar_close - bar_open

            self._cvd += delta

            m = BarMetrics(
                buy_volume=buy_vol,
                sell_volume=sell_vol,
                delta=delta,
                delta_ratio=delta_ratio,
                total_volume=total,
                price_change=price_change,
                cvd=self._cvd,
                timestamp=bar_end_ts,
            )

            # Remove processed trades
            self._trades = [t for t in self._trades if t.timestamp >= bar_end_ts]
            return m

    def get_recent_delta(self, seconds: float = 60) -> tuple[int, int]:
        """Get buy/sell volume for last N seconds (for intrabar signals)."""
        cutoff = time.time() - seconds
        with self._lock:
            buy_vol = sum(t.size for t in self._trades if t.timestamp >= cutoff and t.side == SIDE_BUY)
            sell_vol = sum(t.size for t in self._trades if t.timestamp >= cutoff and t.side == SIDE_SELL)
        return buy_vol, sell_vol


class OrderBookTracker:
    """Tracks L2 order book state and calculates metrics."""

    def __init__(self, wall_multiplier: float = 3.0, scan_radius: int = 50):
        self._bids: dict[float, int] = {}  # price → size
        self._asks: dict[float, int] = {}
        self._lock = threading.Lock()
        self._wall_mult = wall_multiplier
        self._scan_radius = scan_radius

    # Action constants from Finam protobuf
    ACTION_REMOVE = 1
    ACTION_ADD = 2
    ACTION_UPDATE = 3

    def update(self, rows: list):
        """Process incremental OB update. rows = list of (price, buy_size, sell_size, action)."""
        with self._lock:
            for price, buy, sell, action in rows:
                if price <= 0:
                    continue
                if action == self.ACTION_REMOVE:
                    self._bids.pop(price, None)
                    self._asks.pop(price, None)
                else:
                    if buy > 0:
                        self._bids[price] = buy
                    elif price in self._bids:
                        self._bids.pop(price, None)
                    if sell > 0:
                        self._asks[price] = sell
                    elif price in self._asks:
                        self._asks.pop(price, None)

    def get_metrics(self, mid_price: float = 0.0) -> OBMetrics:
        """Calculate current OB metrics."""
        with self._lock:
            total_buy = sum(self._bids.values())
            total_sell = sum(self._asks.values())
            total = total_buy + total_sell

            imbalance = (total_buy - total_sell) / total if total > 0 else 0.0

            # Wall detection
            all_sizes = list(self._bids.values()) + list(self._asks.values())
            avg_size = np.mean(all_sizes) if all_sizes else 0

            wall_price = 0.0
            wall_side = 0
            wall_size = 0
            if avg_size > 0:
                threshold = avg_size * self._wall_mult
                # Find biggest wall
                for p, s in self._bids.items():
                    if s > threshold and s > wall_size:
                        wall_price = p
                        wall_side = 1
                        wall_size = s
                for p, s in self._asks.items():
                    if s > threshold and s > wall_size:
                        wall_price = p
                        wall_side = -1
                        wall_size = s

            # Near liquidity (within scan_radius of mid)
            near_buy = 0
            near_sell = 0
            if mid_price > 0:
                lo = mid_price - self._scan_radius
                hi = mid_price + self._scan_radius
                near_buy = sum(s for p, s in self._bids.items() if lo <= p <= mid_price)
                near_sell = sum(s for p, s in self._asks.items() if mid_price <= p <= hi)

            return OBMetrics(
                imbalance=imbalance,
                total_buy=total_buy,
                total_sell=total_sell,
                wall_price=wall_price,
                wall_side=wall_side,
                wall_size=wall_size,
                near_buy=near_buy,
                near_sell=near_sell,
                timestamp=time.time(),
            )

    @property
    def has_data(self) -> bool:
        with self._lock:
            return len(self._bids) > 0 or len(self._asks) > 0


class SignalEngine:
    """Generates trading signals from bar metrics and OB state."""

    def __init__(self,
                 absorption_threshold: float = 0.35,
                 cvd_lookback: int = 10,
                 ob_imbalance_threshold: float = 0.50,
                 atr_period: int = 14,
                 price_stall_factor: float = 0.3):
        self._abs_thresh = absorption_threshold
        self._cvd_lb = cvd_lookback
        self._ob_thresh = ob_imbalance_threshold
        self._atr_period = atr_period
        self._stall_factor = price_stall_factor

        # History for CVD divergence
        self._bar_history: deque[BarMetrics] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._close_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._high_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._low_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))

    def add_bar(self, metrics: BarMetrics, bar_high: float, bar_low: float, bar_close: float):
        """Feed completed bar metrics into the signal engine."""
        self._bar_history.append(metrics)
        self._close_history.append(bar_close)
        self._high_history.append(bar_high)
        self._low_history.append(bar_low)

    def _get_atr(self) -> float:
        """Simple ATR from high-low ranges."""
        if len(self._high_history) < 2:
            return 50.0  # default
        ranges = [h - l for h, l in zip(self._high_history, self._low_history)]
        period = min(self._atr_period, len(ranges))
        return float(np.mean(ranges[-period:])) if ranges else 50.0

    def check_absorption(self, current_price: float) -> Optional[OFSignal]:
        """Signal A: Absorption — strong delta but price stalled."""
        if len(self._bar_history) < 2:
            return None

        m = self._bar_history[-1]
        atr = self._get_atr()

        if atr <= 0:
            return None

        # Strong one-sided flow
        if abs(m.delta_ratio) < self._abs_thresh:
            return None

        # Price barely moved
        if abs(m.price_change) >= self._stall_factor * atr:
            return None

        # Direction: if sellers aggressive (delta < 0) but price held → LONG (buyer absorbing)
        # If buyers aggressive (delta > 0) but price held → SHORT (seller absorbing)
        if m.delta < 0:
            direction = 1   # LONG
        else:
            direction = -1  # SHORT

        strength = min(abs(m.delta_ratio), 1.0)

        return OFSignal(
            signal_type='absorption',
            direction=direction,
            strength=strength,
            price=current_price,
            timestamp=time.time(),
        )

    def check_cvd_divergence(self, current_price: float) -> Optional[OFSignal]:
        """Signal B: CVD Divergence — price new extreme but CVD disagrees."""
        if len(self._bar_history) < self._cvd_lb + 1:
            return None

        lookback = self._cvd_lb
        closes = list(self._close_history)
        lows = list(self._low_history)
        highs = list(self._high_history)
        cvds = [m.cvd for m in self._bar_history]

        current_low = lows[-1]
        current_high = highs[-1]
        current_cvd = cvds[-1]

        # Lookback extremes (excluding current bar)
        prev_lows = lows[-(lookback + 1):-1]
        prev_highs = highs[-(lookback + 1):-1]
        prev_cvds = cvds[-(lookback + 1):-1]

        min_prev_low = min(prev_lows) if prev_lows else current_low
        max_prev_high = max(prev_highs) if prev_highs else current_high
        min_prev_cvd = min(prev_cvds) if prev_cvds else current_cvd
        max_prev_cvd = max(prev_cvds) if prev_cvds else current_cvd

        # Price makes new low, but CVD is higher → sellers exhausted → LONG
        if current_low < min_prev_low and current_cvd > min_prev_cvd:
            return OFSignal(
                signal_type='cvd_divergence',
                direction=1,  # LONG
                strength=0.6,
                price=current_price,
                timestamp=time.time(),
            )

        # Price makes new high, but CVD is lower → buyers exhausted → SHORT
        if current_high > max_prev_high and current_cvd < max_prev_cvd:
            return OFSignal(
                signal_type='cvd_divergence',
                direction=-1,  # SHORT
                strength=0.6,
                price=current_price,
                timestamp=time.time(),
            )

        return None

    def check_ob_imbalance(self, ob: OBMetrics, current_price: float) -> Optional[OFSignal]:
        """Signal C: OrderBook Imbalance — heavy skew + wall."""
        if abs(ob.imbalance) < self._ob_thresh:
            return None

        # Need wall confirmation on the same side
        if ob.imbalance > 0 and ob.wall_side == 1:
            # Buy wall + buy-heavy book → LONG
            return OFSignal(
                signal_type='ob_imbalance',
                direction=1,
                strength=min(abs(ob.imbalance), 1.0),
                price=current_price,
                timestamp=time.time(),
            )
        elif ob.imbalance < 0 and ob.wall_side == -1:
            # Sell wall + sell-heavy book → SHORT
            return OFSignal(
                signal_type='ob_imbalance',
                direction=-1,
                strength=min(abs(ob.imbalance), 1.0),
                price=current_price,
                timestamp=time.time(),
            )

        return None

    def check_reverse_signal(self, ob: OBMetrics, position_dir: int, current_price: float, confirm_count: int = 1) -> bool:
        """Check if enough signals fired in the OPPOSITE direction of our position."""
        reverse_count = 0

        # Check absorption (uses last bar)
        sig_a = self.check_absorption(current_price)
        if sig_a and sig_a.direction != position_dir:
            reverse_count += 1

        sig_b = self.check_cvd_divergence(current_price)
        if sig_b and sig_b.direction != position_dir:
            reverse_count += 1

        sig_c = self.check_ob_imbalance(ob, current_price)
        if sig_c and sig_c.direction != position_dir:
            reverse_count += 1

        return reverse_count >= confirm_count

    def generate_signals(self, ob: OBMetrics, current_price: float) -> list[OFSignal]:
        """Generate all matching signals at current moment."""
        signals = []
        sig = self.check_absorption(current_price)
        if sig:
            signals.append(sig)
        sig = self.check_cvd_divergence(current_price)
        if sig:
            signals.append(sig)
        sig = self.check_ob_imbalance(ob, current_price)
        if sig:
            signals.append(sig)
        return signals

    @property
    def bars_ready(self) -> int:
        return len(self._bar_history)

    @property
    def has_enough_data(self) -> bool:
        return len(self._bar_history) >= max(self._cvd_lb + 1, 5)
