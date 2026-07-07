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
    signal_type: str        # 'dm_wall_agree', 'cvd_divergence', 'ob_imbalance'
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
    """Tracks L2 order book state and calculates metrics.

    Also tracks wall consumption: when a large wall (3x avg) shrinks to <30%
    of its peak size, it generates a wall_consumed event with direction.
    """

    WALL_EXPIRY = 300.0  # 5 minutes
    WALL_CONSUME_RATIO = 0.30  # consumed if shrinks to <30%
    WALL_MIN_SAMPLES = 500  # need N sizes seen before detecting walls

    def __init__(self, wall_multiplier: float = 3.0, scan_radius: int = 50):
        self._bids: dict[float, int] = {}  # price → size
        self._asks: dict[float, int] = {}
        self._lock = threading.Lock()
        self._wall_mult = wall_multiplier
        self._scan_radius = scan_radius

        # Wall consumption tracking
        self._walls: dict[float, dict] = {}  # price → {side, size, ts}
        self._sizes_seen: deque[int] = deque(maxlen=2000)
        self._wall_consumed_events: deque[dict] = deque(maxlen=50)

    # Action constants from Finam protobuf
    ACTION_REMOVE = 1
    ACTION_ADD = 2
    ACTION_UPDATE = 3

    def update(self, rows: list):
        """Process incremental OB update. rows = list of (price, buy_size, sell_size, action)."""
        with self._lock:
            now = time.time()
            for price, buy, sell, action in rows:
                if price <= 0:
                    continue

                # Track all sizes for avg calculation
                if buy > 0:
                    self._sizes_seen.append(buy)
                if sell > 0:
                    self._sizes_seen.append(sell)

                if action == self.ACTION_REMOVE:
                    # Wall cancelled = fake, remove tracking
                    if price in self._walls:
                        del self._walls[price]
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

                # Detect new walls
                if len(self._sizes_seen) >= self.WALL_MIN_SAMPLES and action in (1, 2):
                    avg_size = float(np.mean(self._sizes_seen))
                    threshold = avg_size * self._wall_mult
                    if threshold > 0 and price not in self._walls:
                        if buy >= threshold:
                            self._walls[price] = {'side': 'B', 'size': buy, 'ts': now}
                        elif sell >= threshold:
                            self._walls[price] = {'side': 'S', 'size': sell, 'ts': now}

            # Check existing walls for consumption
            expired = []
            for wp, winfo in self._walls.items():
                age = now - winfo['ts']
                if age > self.WALL_EXPIRY:
                    expired.append(wp)
                    continue

                book = self._bids if winfo['side'] == 'B' else self._asks
                cur_size = book.get(wp, 0)

                if cur_size < winfo['size'] * self.WALL_CONSUME_RATIO:
                    # Wall consumed: bid wall eaten → SHORT, ask wall eaten → LONG
                    direction = -1 if winfo['side'] == 'B' else 1
                    self._wall_consumed_events.append({
                        'ts': now, 'dir': direction, 'price': wp,
                    })
                    log.debug(f"Wall consumed @ {wp}: side={winfo['side']} → dir={direction}")
                    expired.append(wp)

            for wp in expired:
                self._walls.pop(wp, None)

    def get_wall_consumed_direction(self, window: float = 120.0) -> Optional[int]:
        """Check if a wall was consumed in the last `window` seconds.
        Returns 1 (LONG) or -1 (SHORT) or None.
        """
        cutoff = time.time() - window
        with self._lock:
            for evt in reversed(self._wall_consumed_events):
                if evt['ts'] >= cutoff:
                    return evt['dir']
            return None

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
                 cvd_lookback: int = 10,
                 ob_imbalance_threshold: float = 0.50,
                 atr_period: int = 14,
                 dm_lookback: int = 5,
                 wall_window: float = 120.0,
                 cvd_accel_period: int = 10,
                 cvd_accel_threshold: float = 1000.0,
                 agg_window: int = 3,
                 agg_ratio_threshold: float = 1.0,
                 use_agg_ratio: bool = True):
        self._cvd_lb = cvd_lookback
        self._ob_thresh = ob_imbalance_threshold
        self._atr_period = atr_period
        self._dm_lb = dm_lookback
        self._wall_window = wall_window

        # CVD Acceleration params
        self._cvd_accel_period = cvd_accel_period
        self._cvd_accel_threshold = cvd_accel_threshold
        self._agg_window = agg_window
        self._agg_ratio_threshold = agg_ratio_threshold
        self._use_agg_ratio = use_agg_ratio

        # Aggression tracking (buy/sell volume at improving prices)
        self._agg_buy_history: deque[int] = deque(maxlen=max(agg_window, 5))
        self._agg_sell_history: deque[int] = deque(maxlen=max(agg_window, 5))

        # History for CVD divergence + delta momentum
        self._bar_history: deque[BarMetrics] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._close_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._high_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))
        self._low_history: deque[float] = deque(maxlen=max(cvd_lookback * 2, 30))

        # CVD Trend EMA state
        self._cvd_ema_fast: float = 0.0
        self._cvd_ema_slow: float = 0.0
        self._cvd_trend_dir: int = 0  # 1=LONG, -1=SHORT, 0=FLAT
        self._cvd_trend_ready: bool = False
        self._cvd_trend_alpha_f: float = 2.0 / (5 + 1)
        self._cvd_trend_alpha_s: float = 2.0 / (15 + 1)
        self._cvd_trend_reverse_count: int = 0
        self._cvd_trend_prev_cvd: float = 0.0

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

    def set_agg_window(self, window: int):
        """Update aggression tracking window."""
        self._agg_window = window
        self._agg_buy_history = deque(maxlen=max(window, 5))
        self._agg_sell_history = deque(maxlen=max(window, 5))

    def add_bar_aggression(self, agg_buy: int, agg_sell: int):
        """Feed per-bar aggression volumes (ticks at improving prices).
        agg_buy: volume of ticks where price went UP (buyer aggression).
        agg_sell: volume of ticks where price went DOWN (seller aggression).
        """
        self._agg_buy_history.append(agg_buy)
        self._agg_sell_history.append(agg_sell)

    def get_agg_ratio(self) -> float:
        """Rolling aggression ratio (buy/sell). >1 = buyers aggressive, <1 = sellers aggressive."""
        total_buy = sum(self._agg_buy_history)
        total_sell = sum(self._agg_sell_history)
        return total_buy / max(total_sell, 1)

    def check_cvd_accel(self, current_price: float) -> Optional[OFSignal]:
        """CVD Acceleration: rate of change of cumulative delta → direction.

        CVD_accel = CVD_now - CVD[N bars ago]. Positive = buyers accelerating, negative = sellers.
        Optional aggression ratio filter: if use_agg_ratio=True, require
        aggression to confirm direction.
        """
        n = len(self._bar_history)
        cap = self._cvd_accel_period
        if n < cap + 1:
            return None

        cvd_now = self._bar_history[-1].cvd
        cvd_prev = self._bar_history[-(cap + 1)].cvd
        cvd_accel = cvd_now - cvd_prev

        signal_dir = 0
        strength = min(abs(cvd_accel) / max(abs(self._cvd_accel_threshold), 1), 1.0)

        if cvd_accel > self._cvd_accel_threshold:
            # CVD accelerating up → LONG
            if self._use_agg_ratio:
                ratio = self.get_agg_ratio()
                if ratio < self._agg_ratio_threshold:
                    return None  # aggression doesn't confirm
            signal_dir = 1
        elif cvd_accel < -self._cvd_accel_threshold:
            # CVD accelerating down → SHORT
            if self._use_agg_ratio:
                ratio = self.get_agg_ratio()
                # For SHORT: need sell aggression (ratio < 1/threshold)
                inv_thresh = 1.0 / max(self._agg_ratio_threshold - 0.3, 0.5)
                if ratio > inv_thresh:
                    return None  # aggression doesn't confirm
            signal_dir = -1

        if signal_dir == 0:
            return None

        log.info(f"CVD Accel signal → {'LONG' if signal_dir == 1 else 'SHORT'} | "
                 f"accel={cvd_accel:.0f} thresh={self._cvd_accel_threshold:.0f} "
                 f"agg_ratio={self.get_agg_ratio():.2f}")

        return OFSignal(
            signal_type='cvd_accel',
            direction=signal_dir,
            strength=strength,
            price=current_price,
            timestamp=time.time(),
        )

    def check_cvd_trend(self, current_price: float) -> Optional[OFSignal]:
        """CVD Trend: EMA(fast=5)/EMA(slow=15) crossover on cumulative volume delta.

        Direction = LONG when fast EMA > slow EMA, SHORT when fast < slow.
        Resets on daily CVD reset (CVD drops to ~0).
        Returns signal only on crossover (direction change).
        """
        if len(self._bar_history) < 15:
            return None

        current_cvd = self._bar_history[-1].cvd

        if not self._cvd_trend_ready:
            self._cvd_ema_fast = current_cvd
            self._cvd_ema_slow = current_cvd
            self._cvd_trend_prev_cvd = current_cvd
            self._cvd_trend_ready = True
            return None

        # Detect daily CVD reset (CVD dropped to near-zero from a large value)
        prev_abs = abs(self._cvd_trend_prev_cvd)
        if prev_abs > 500 and abs(current_cvd) < prev_abs * 0.1:
            log.info(f"CVD Trend: daily reset (CVD {self._cvd_trend_prev_cvd:.0f} → {current_cvd:.0f})")
            self._cvd_ema_fast = current_cvd
            self._cvd_ema_slow = current_cvd
            self._cvd_trend_dir = 0
            self._cvd_trend_reverse_count = 0
            self._cvd_trend_prev_cvd = current_cvd
            return None

        # Update EMAs
        self._cvd_ema_fast = self._cvd_trend_alpha_f * current_cvd + (1 - self._cvd_trend_alpha_f) * self._cvd_ema_fast
        self._cvd_ema_slow = self._cvd_trend_alpha_s * current_cvd + (1 - self._cvd_trend_alpha_s) * self._cvd_ema_slow
        self._cvd_trend_prev_cvd = current_cvd

        new_dir = 1 if self._cvd_ema_fast > self._cvd_ema_slow else -1

        if new_dir != self._cvd_trend_dir and self._cvd_trend_dir != 0:
            old_dir = self._cvd_trend_dir
            self._cvd_trend_dir = new_dir
            self._cvd_trend_reverse_count = 0
            log.info(f"CVD Trend CROSSOVER → {'LONG' if new_dir == 1 else 'SHORT'} | EMA_f={self._cvd_ema_fast:.0f} EMA_s={self._cvd_ema_slow:.0f} CVD={current_cvd:.0f}")
            return OFSignal(
                signal_type='cvd_trend',
                direction=new_dir,
                strength=0.9,
                price=current_price,
                timestamp=time.time(),
            )
        elif self._cvd_trend_dir == 0:
            self._cvd_trend_dir = new_dir
            self._cvd_trend_reverse_count = 0
            log.info(f"CVD Trend INIT → {'LONG' if new_dir == 1 else 'SHORT'} | EMA_f={self._cvd_ema_fast:.0f} EMA_s={self._cvd_ema_slow:.0f}")
        else:
            # Track consecutive bars against trend (for reverse exit)
            if new_dir != self._cvd_trend_dir:
                self._cvd_trend_reverse_count += 1
            else:
                self._cvd_trend_reverse_count = 0

        return None

    def get_cvd_trend_direction(self) -> int:
        """Current CVD trend direction (1=LONG, -1=SHORT, 0=not ready)."""
        return self._cvd_trend_dir

    def get_cvd_trend_reverse_count(self) -> int:
        """Bars with direction opposite to current trend."""
        return self._cvd_trend_reverse_count

    def check_dm_wall_agree(self, ob_tracker: 'OrderBookTracker', current_price: float) -> Optional[OFSignal]:
        """Signal A: Delta Momentum + Wall Consumed agree.

        dm: N consecutive bars with same delta direction (all buy or all sell).
        wall consumed: a large wall was eaten within the window.
        Both must fire AND agree on direction.
        """
        if len(self._bar_history) < self._dm_lb:
            return None

        # 1. Delta momentum: last N bars all same delta sign
        deltas = [m.delta for m in list(self._bar_history)[-self._dm_lb:]]
        dm_dir = None
        if all(d > 0 for d in deltas):
            dm_dir = 1   # all buy delta → LONG
        elif all(d < 0 for d in deltas):
            dm_dir = -1  # all sell delta → SHORT

        if dm_dir is None:
            return None

        # 2. Wall consumed (within window)
        wall_dir = ob_tracker.get_wall_consumed_direction(self._wall_window)
        if wall_dir is None:
            return None

        # 3. Must agree
        if dm_dir != wall_dir:
            return None

        return OFSignal(
            signal_type='dm_wall_agree',
            direction=dm_dir,
            strength=0.8,
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

    def check_reverse_signal(self, ob: OBMetrics, position_dir: int, current_price: float,
                              ob_tracker: Optional['OrderBookTracker'] = None, confirm_count: int = 1,
                              use_dm_wall: bool = True, use_cvd: bool = True, use_ob_imbalance: bool = True,
                              use_cvd_accel: bool = True) -> bool:
        """Check if enough signals fired in the OPPOSITE direction of our position.
        For CVD Trend: uses reverse_count (consecutive bars against trend).
        For CVD Accel: checks if accel flipped direction.
        """
        reverse_count = 0

        # CVD Trend reverse: consecutive bars with EMA flipped against position
        if use_cvd:
            td = self.get_cvd_trend_direction()
            if td != 0 and td != position_dir:
                rc = self.get_cvd_trend_reverse_count()
                if rc >= confirm_count:
                    return True

        # CVD Acceleration reverse
        if use_cvd_accel:
            n = len(self._bar_history)
            cap = self._cvd_accel_period
            if n >= cap + 1:
                cvd_now = self._bar_history[-1].cvd
                cvd_prev = self._bar_history[-(cap + 1)].cvd
                cvd_accel = cvd_now - cvd_prev
                # If accel fires opposite to our position direction
                if position_dir == 1 and cvd_accel < -self._cvd_accel_threshold:
                    return True
                elif position_dir == -1 and cvd_accel > self._cvd_accel_threshold:
                    return True

        if use_dm_wall and ob_tracker is not None:
            sig_a = self.check_dm_wall_agree(ob_tracker, current_price)
            if sig_a and sig_a.direction != position_dir:
                reverse_count += 1

        if use_ob_imbalance:
            sig_c = self.check_ob_imbalance(ob, current_price)
            if sig_c and sig_c.direction != position_dir:
                reverse_count += 1

        # If CVD trend already handles reverse, supplement with other signals
        if use_cvd and self.get_cvd_trend_direction() != 0:
            return reverse_count >= 1  # any supplementary signal suffices

        return reverse_count >= confirm_count

    def generate_signals(self, ob: OBMetrics, current_price: float,
                        ob_tracker: Optional['OrderBookTracker'] = None,
                        use_dm_wall: bool = True, use_cvd: bool = True, use_ob_imbalance: bool = True,
                        use_cvd_accel: bool = True) -> list[OFSignal]:
        """Generate all matching signals at current moment.
        When use_cvd_accel=True, uses CVD Acceleration + Aggression Ratio.
        When use_cvd=True, uses CVD Trend (EMA crossover).
        When use_ob_imbalance=True, uses OB Imbalance.
        """
        signals = []
        if use_cvd_accel:
            sig = self.check_cvd_accel(current_price)
            if sig:
                signals.append(sig)
        if use_cvd:
            sig = self.check_cvd_trend(current_price)
            if sig:
                signals.append(sig)
        if use_dm_wall and ob_tracker is not None:
            sig = self.check_dm_wall_agree(ob_tracker, current_price)
            if sig:
                signals.append(sig)
        if use_ob_imbalance:
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
