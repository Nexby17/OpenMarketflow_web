"""Order Flow Strategy — main strategy logic: entry, averaging, pyramiding, exits.

v2 — fixed: LIFO partial TP, lotQueue persistence, commission accounting,
      stale price check, entry lock reset on stop.
"""
import logging
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import deque
import math

from orderflow_engine import (
    TradeCollector, OrderBookTracker, SignalEngine,
    OFSignal, OBMetrics, BarMetrics,
)

from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

log = logging.getLogger("strategy_of")

MSK = timezone(timedelta(hours=3))

LONG = 1
SHORT = -1
FLAT = 0


@dataclass
class OFParams:
    """All user-configurable parameters."""
    lots: int = 1
    max_pyramid_levels: int = 5
    max_average_levels: int = 100
    step_average: int = 50        # pts — шаг усреднения (против позиции)
    step_pyramid: int = 35        # pts — шаг пирамидинга (по тренду)
    spread: int = 50              # pts — мин прибыль на partial TP
    partial_tp: bool = True       # LIFO partial close
    stop_loss_mode: str = "rub"   # rub / pct / pts
    stop_loss_value: float = 7000
    margin_per_lot: float = 7000  # ГО за 1 лот (для pct режима)
    min_profit_per_lot: int = 30  # pts — для полного TP
    max_hold_minutes: int = 999
    dm_lookback: int = 5          # bars for delta momentum
    wall_window: float = 120.0   # seconds to look back for wall consumed
    wall_multiplier: float = 3.0  # wall = N× avg size in OB
    use_dm_wall: bool = False       # enable dm_wall_agree signal
    use_cvd: bool = False            # enable cvd_trend signal
    use_cvd_accel: bool = True      # enable CVD Acceleration + Aggression Ratio (replaces OB Imbalance)
    cvd_lookback: int = 10
    cvd_ema_fast: int = 5           # CVD Trend EMA fast period
    cvd_ema_slow: int = 15          # CVD Trend EMA slow period
    cvd_accel_period: int = 10      # bars for CVD acceleration calc
    cvd_accel_threshold: float = 1000.0  # min CVD accel delta to signal
    agg_window: int = 3              # bars for rolling aggression calc
    agg_ratio_threshold: float = 1.0     # min buy/sell ratio to confirm
    use_agg_ratio: bool = True      # use aggression ratio as filter
    signal_confirm_count: int = 1     # signals needed for ENTRY (CVD Trend = 1)
    signal_confirm_exit: int = 1      # bars reverse for EXIT (CVD Trend reverse)
    vp_filter: bool = False       # Volume Profile filter (off by default)
    atr_period: int = 14
    timeframe: str = "M5"
    step_atr: bool = False        # adaptive step via ATR
    # Protective filters
    close_half_pct: int = 0         # 0=OFF, 50/40/30/20/10 — при X% peak + partial TP trigger → close_all
    close_price_all: float = 0.0    # 0=OFF, иначе цена для принудительного закрытия всей позиции (LONG ≥, SHORT ≤)
    enable_max_levels: bool = True
    enable_daily_stop: bool = True
    enable_ob_filter: bool = True
    ob_min_lots: int = 20         # min bid+ask lots near price
    ob_scan_radius: int = 50      # pts radius for OB filter
    commission: float = 0.90      # per side (0.90₽ = one-way)
    direction_filter: str = "both"  # "both" | "long" | "short"
    # VAH/VAL Volume Profile filter
    use_vah_val: bool = False      # enable VAH/VAL entry filter
    vah_val_pct: int = 70          # value area percentage (70%)
    vah_val_bin_size: int = 5      # VP bin size in points
    vah_val_mode: str = "fade"     # "fade" = counter-trend at VAH/VAL, "breakout" = trend with VAH/VAL
    # VWEMA regime filter
    use_vwema: bool = False       # enable VWEMA trend filter
    vwema_fast: int = 20          # fast VWEMA period
    vwema_slow: int = 40          # slow VWEMA period
    vwema_flat_th: float = 1.0    # flat zone threshold (ATR multiples)
    vwema_block_counter: bool = True  # block counter-trend entries
    vwema_avg_exit: bool = False    # B2: не усреднять против VWEMA + закрыть усреднённую позицию при развороте VWEMA


@dataclass
class LotEntry:
    """Single lot in the position queue (for LIFO partial TP)."""
    price: float
    side: int     # LONG or SHORT (buy=1, sell=-1)
    lots: int     # how many lots at this price
    added_ts: float = 0.0  # time.monotonic() when lot was added (grace period)


class VWEMARegime:
    """Volume-Weighted EMA crossover + ATR regime filter.

    Direction = (fast_VWEMA - slow_VWEMA) / ATR.
    High-volume bars pull the EMA faster (alpha scaled by volume ratio).
    """
    def __init__(self, fast=20, slow=40, flat_th=1.0):
        self.fast_n = fast
        self.slow_n = slow
        self.flat_th = flat_th
        self._ema_f = 0.0
        self._ema_s = 0.0
        self._atr = 0.0
        self._vol_f = 0.0
        self._vol_s = 0.0
        self._closes = deque(maxlen=max(slow, 15))
        self._vols = deque(maxlen=max(slow, 15))
        self._h_q = deque(maxlen=15)
        self._l_q = deque(maxlen=15)
        self._c_q = deque(maxlen=15)
        self._ready = False
        self._af = 2.0 / (fast + 1)
        self._as_ = 2.0 / (slow + 1)
        self._vf = self._af   # volume EMA alpha (same period)
        self._vs = self._as_

    def update(self, close: float, high: float, low: float, volume: float = 0.0):
        self._h_q.append(high)
        self._l_q.append(low)
        self._closes.append(close)
        self._c_q.append(close)
        self._vols.append(volume)

        if len(self._closes) < self.slow_n:
            return

        if not self._ready:
            self._ema_f = sum(list(self._closes)[-self.fast_n:]) / self.fast_n
            self._ema_s = sum(list(self._closes)[-self.slow_n:]) / self.slow_n
            self._vol_f = sum(list(self._vols)[-self.fast_n:]) / self.fast_n if len(self._vols) >= self.fast_n else sum(self._vols) / len(self._vols)
            self._vol_s = sum(list(self._vols)[-self.slow_n:]) / self.slow_n if len(self._vols) >= self.slow_n else sum(self._vols) / len(self._vols)
            self._ready = True
        else:
            # Volume EMAs
            self._vol_f = self._vf * volume + (1 - self._vf) * self._vol_f
            self._vol_s = self._vs * volume + (1 - self._vs) * self._vol_s

            # Volume-normalized alpha
            vf_norm = self._vol_f / self._vol_s if self._vol_s > 0 else 1.0
            vf_c = min(max(vf_norm, 0.3), 3.0)
            a_f = min(self._af * vf_c, 0.95)
            a_s = min(self._as_ * 1.0, 0.95)  # slow EMA — no volume boost

            self._ema_f = a_f * close + (1 - a_f) * self._ema_f
            self._ema_s = a_s * close + (1 - a_s) * self._ema_s

        # ATR (14-period Wilder)
        if len(self._h_q) > 1 and len(self._c_q) > 1:
            tr = max(high - low, abs(high - list(self._c_q)[-2]), abs(low - list(self._c_q)[-2]))
            self._atr = self._atr * 13 / 14 + tr / 14 if self._atr > 0 else tr

    @property
    def direction(self) -> int:
        if not self._ready or self._atr <= 0:
            return 0
        s = (self._ema_f - self._ema_s) / self._atr
        return 0 if abs(s) < self.flat_th else (1 if s > 0 else -1)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def state(self) -> dict:
        return {
            "direction": self.direction,
            "ema_f": round(self._ema_f, 1),
            "ema_s": round(self._ema_s, 1),
            "atr": round(self._atr, 1),
            "spread_atr": round((self._ema_f - self._ema_s) / self._atr, 2) if self._atr > 0 else 0,
            "ready": self._ready,
        }


class VolumeProfileCalculator:
    """Volume Profile — accumulates volume at price levels from tick trades.

    Calculates VAH (Value Area High), VAL (Value Area Low), POC (Point of Control)
    for the current trading session. Resets at midnight MSK.
    """
    def __init__(self, bin_size: int = 5, value_area_pct: int = 70):
        self.bin_size = bin_size
        self.value_area_pct = value_area_pct
        self._profile: dict[int, int] = {}  # bin → total volume
        self._total_volume: int = 0
        self._session_date: Optional[str] = None
        self._vah: float = 0.0
        self._val: float = 0.0
        self._poc: float = 0.0

    def _check_session_reset(self, now: datetime):
        """Reset profile at midnight MSK."""
        msk_date = now.astimezone(MSK).strftime("%Y-%m-%d")
        if self._session_date != msk_date:
            self._session_date = msk_date
            self._profile.clear()
            self._total_volume = 0
            self._vah = 0.0
            self._val = 0.0
            self._poc = 0.0

    def add_trade(self, price: float, volume: int, now: datetime):
        """Add a tick trade to the volume profile."""
        self._check_session_reset(now)
        if price <= 0 or volume <= 0:
            return
        bin_key = int(price // self.bin_size * self.bin_size)
        self._profile[bin_key] = self._profile.get(bin_key, 0) + volume
        self._total_volume += volume

    def calculate(self) -> bool:
        """Recalculate VAH/VAL/POC. Returns True if values are ready."""
        if not self._profile or self._total_volume <= 0:
            return False

        # POC = bin with highest volume
        poc_bin = max(self._profile, key=self._profile.get)
        self._poc = float(poc_bin + self.bin_size / 2)

        # Value Area: expand from POC until value_area_pct of total volume is captured
        target_volume = self._total_volume * self.value_area_pct / 100.0
        sorted_bins = sorted(self._profile.keys())
        poc_idx = sorted_bins.index(poc_bin)

        captured = self._profile[poc_bin]
        va_low_idx = poc_idx
        va_high_idx = poc_idx

        while captured < target_volume:
            # Try expand down
            down_vol = self._profile.get(sorted_bins[va_low_idx - 1], 0) if va_low_idx > 0 else -1
            # Try expand up
            up_vol = self._profile.get(sorted_bins[va_high_idx + 1], 0) if va_high_idx < len(sorted_bins) - 1 else -1

            if down_vol < 0 and up_vol < 0:
                break
            if down_vol >= up_vol:
                va_low_idx -= 1
                captured += self._profile[sorted_bins[va_low_idx]]
            else:
                va_high_idx += 1
                captured += self._profile[sorted_bins[va_high_idx]]

        self._val = float(sorted_bins[va_low_idx])
        self._vah = float(sorted_bins[va_high_idx] + self.bin_size)
        return True

    @property
    def vah(self) -> float:
        return self._vah

    @property
    def val(self) -> float:
        return self._val

    @property
    def poc(self) -> float:
        return self._poc

    @property
    def ready(self) -> bool:
        return self._vah > 0 and self._val > 0

    @property
    def state(self) -> dict:
        return {
            "vah": round(self._vah, 1) if self._vah > 0 else 0,
            "val": round(self._val, 1) if self._val > 0 else 0,
            "poc": round(self._poc, 1) if self._poc > 0 else 0,
            "ready": self.ready,
            "bins": len(self._profile),
            "totalVolume": self._total_volume,
        }


class OrderFlowStrategy:
    """Main Order Flow strategy — entry, averaging, pyramiding, partial TP, exits."""

    def __init__(self, params: OFParams):
        self.p = params
        self.trades = TradeCollector()
        self.ob_tracker = OrderBookTracker(
            wall_multiplier=params.wall_multiplier,
            scan_radius=params.ob_scan_radius,
        )
        self.signals = SignalEngine(
            cvd_lookback=params.cvd_lookback,
            ob_imbalance_threshold=0.50,  # legacy, disabled
            atr_period=params.atr_period,
            dm_lookback=params.dm_lookback,
            wall_window=params.wall_window,
            cvd_accel_period=params.cvd_accel_period,
            cvd_accel_threshold=params.cvd_accel_threshold,
            agg_window=params.agg_window,
            agg_ratio_threshold=params.agg_ratio_threshold,
            use_agg_ratio=params.use_agg_ratio,
        )
        # Apply CVD Trend EMA periods
        self.signals._cvd_trend_ema_fast_period = params.cvd_ema_fast
        self.signals._cvd_trend_ema_slow_period = params.cvd_ema_slow
        self.signals._cvd_trend_alpha_f = 2.0 / (params.cvd_ema_fast + 1)
        self.signals._cvd_trend_alpha_s = 2.0 / (params.cvd_ema_slow + 1)

        # Position state
        self._dir: int = FLAT

        # VWEMA regime filter
        self.vwema: Optional[VWEMARegime] = None
        self._init_vwema()

        # Volume Profile (VAH/VAL/POC)
        self.vp: Optional[VolumeProfileCalculator] = None
        self._init_vp()
        self._entry_price: float = 0.0
        self._avg_price: float = 0.0
        self._total_lots: int = 0
        self._peak_lots: int = 0
        self._average_levels: int = 0
        self._pyramid_levels: int = 0
        self._last_average_price: float = 0.0
        self._last_pyramid_price: float = 0.0
        self._lot_queue: deque[LotEntry] = deque()
        self._realized_pnl: float = 0.0
        self._round_trips: int = 0
        self._entry_time: Optional[datetime] = None
        self._signal_type: str = ""

        # Trade history for journal
        self._trade_history: list[dict] = []
        self._position_entry_price: float = 0.0  # For tracking close_all entry

        # Last bar metrics for UI
        self._last_delta: float = 0.0
        self._last_ob_imbalance: float = 0.0
        self._last_dm_wall: str = "—"  # dm_wall_agree signal status

        # Daily PnL tracking
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: Optional[str] = None

        # Entry lock
        self._entry_lock_until: float = 0.0

        # Last bar processing
        self._last_bar_ts: float = 0.0

        # Current price (updated each tick)
        self._current_price: float = 0.0

        # Stale price detection
        self._last_price_update: float = 0.0

        # Trade stream health (LatestTrades from FinamPy)
        self._last_trade_ts: float = 0.0

        # Broker ground truth (synced from DataProvider)
        self._broker_avg_price: float = 0.0
        self._broker_current_price: float = 0.0
        self._broker_sync_time: float = 0.0
        self._last_action_time: float = 0.0

        # State lock
        self._lock = threading.Lock()

    # ---------- Properties ----------

    @property
    def in_position(self) -> bool:
        return self._dir != FLAT and self._total_lots > 0

    @property
    def direction(self) -> int:
        return self._dir

    @property
    def total_lots(self) -> int:
        return self._total_lots

    @property
    def avg_price(self) -> float:
        return self._avg_price

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def entry_time(self) -> Optional[datetime]:
        return self._entry_time

    @property
    def max_position(self) -> int:
        """Absolute ceiling = lots × (1 + MaxAvg + MaxPyr)."""
        return self.p.lots * (1 + self.p.max_average_levels + self.p.max_pyramid_levels)

    # ---------- Time checks ----------

    def _is_night(self, now: datetime) -> bool:
        msk = now.astimezone(MSK)
        return (msk.hour >= 23 and msk.minute >= 50) or msk.hour < 7

    def _is_clearing(self, now: datetime) -> bool:
        msk = now.astimezone(MSK)
        return msk.hour == 14 and msk.minute < 5

    def _is_entry_locked(self) -> bool:
        return time.time() < self._entry_lock_until

    def _lock_entry(self, seconds: float = 60.0):
        self._entry_lock_until = time.time() + seconds

    def _force_unlock(self):
        """Reset entry lock immediately (for manual stop/restart)."""
        self._entry_lock_until = 0.0

    # ---------- Stale price ----------

    def _is_price_stale(self, max_age_sec: float = 180) -> bool:
        """Check if price data is stale (> 3 min default)."""
        if self._last_price_update <= 0:
            return True
        return (time.time() - self._last_price_update) > max_age_sec

    def _init_vwema(self):
        """Create or recreate VWEMA filter from current params."""
        if self.p.use_vwema:
            self.vwema = VWEMARegime(
                fast=self.p.vwema_fast,
                slow=self.p.vwema_slow,
                flat_th=self.p.vwema_flat_th,
            )
            log.info(f"VWEMA filter enabled: fast={self.p.vwema_fast} slow={self.p.vwema_slow} flat_th={self.p.vwema_flat_th}")
        else:
            self.vwema = None

    def _init_vp(self):
        """Create or recreate Volume Profile calculator from current params."""
        if self.p.use_vah_val:
            self.vp = VolumeProfileCalculator(
                bin_size=self.p.vah_val_bin_size,
                value_area_pct=self.p.vah_val_pct,
            )
            log.info(f"Volume Profile (VAH/VAL) enabled: pct={self.p.vah_val_pct}%")
        else:
            self.vp = None

    def update_price(self, price: float):
        """Update current price and mark freshness."""
        self._current_price = price
        self._last_price_update = time.time()

    def is_outside_va(self, price: float) -> bool:
        """Check if price is outside Value Area. Only for 'range' mode.
        Returns False if VAH/VAL not ready or not enabled."""
        if not self.p.use_vah_val or self.p.vah_val_mode != "range":
            return False
        if not self.vp or not self.vp.ready:
            return False
        vah = self.vp.vah
        val = self.vp.val
        if vah <= 0 or val <= 0:
            return False
        return price < val or price > vah

    # ---------- Daily PnL ----------

    def _check_daily_reset(self, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl_date = today
            self._daily_pnl = 0.0

    def _daily_stop_hit(self) -> bool:
        if not self.p.enable_daily_stop:
            return False
        return self._daily_pnl <= -self.p.stop_loss_value

    # ---------- OB Filter ----------

    def _ob_filter_passes(self, ob: OBMetrics) -> bool:
        """Check if orderbook has enough liquidity for entry."""
        if not self.p.enable_ob_filter:
            return True
        near_total = ob.near_buy + ob.near_sell
        return near_total >= self.p.ob_min_lots

    # ---------- PnL ----------

    def unrealized_pnl(self, price: float) -> float:
        """Current unrealized PnL — uses strategy avg_price (calculated from lot_queue)."""
        if self._dir == FLAT or self._total_lots == 0:
            return 0.0
        return (price - self._avg_price) * self._dir * self._total_lots

    def pnl_per_lot(self, price: float) -> float:
        """PnL per lot in points — uses strategy avg_price (calculated from lot_queue)."""
        if self._total_lots == 0:
            return 0.0
        return (price - self._avg_price) * self._dir

    def _stop_loss_hit(self, price: float) -> bool:
        """Check stop-loss condition based on selected mode."""
        if self._dir == FLAT:
            return False
        unreal = self.unrealized_pnl(price)
        if self.p.stop_loss_mode == "rub":
            return unreal <= -self.p.stop_loss_value
        elif self.p.stop_loss_mode == "pct":
            margin = self.p.margin_per_lot * self._total_lots
            if margin <= 0:
                return False
            return (unreal / margin) <= -self.p.stop_loss_value / 100.0
        elif self.p.stop_loss_mode == "pts":
            return self.pnl_per_lot(price) <= -self.p.stop_loss_value
        return False

    def _tp_full_hit(self, price: float) -> bool:
        """Check full TP condition."""
        if self._total_lots == 0:
            return False
        return self.pnl_per_lot(price) >= self.p.min_profit_per_lot

    def _timeout_hit(self, now: datetime) -> bool:
        if self._entry_time is None:
            return False
        held = (now - self._entry_time).total_seconds() / 60.0
        return held >= self.p.max_hold_minutes

    # ---------- Main tick processing ----------

    def on_bar_close(self, metrics: BarMetrics, bar_high: float, bar_low: float, bar_close: float):
        """Called when a bar closes. Feed metrics into signal engine and VWEMA."""
        self._last_delta = metrics.delta
        # Check dm_wall_agree for UI
        sig_dm = self.signals.check_dm_wall_agree(self.ob_tracker, self._current_price)
        if sig_dm:
            arrow = "🟢 LONG" if sig_dm.direction == 1 else "🔴 SHORT"
            self._last_dm_wall = f"{arrow} ({sig_dm.strength:.0%})"
        else:
            self._last_dm_wall = "—"
        self.signals.add_bar(metrics, bar_high, bar_low, bar_close)
        if self.vwema:
            self.vwema.update(bar_close, bar_high, bar_low, volume=metrics.delta)
            d = self.vwema.direction
            if d != 0:
                log.debug(f"VWEMA trend: {'UP' if d > 0 else 'DOWN'} ({self.vwema.state})")

    def process_tick(self, current_price: float, now: datetime) -> list[dict]:
        """Main strategy loop — called on each price update.

        Returns list of action dicts:
          {'action': 'entry', 'side': 'buy'/'sell', 'qty': N, 'price': P}
          {'action': 'average', 'side': ..., 'qty': N, 'price': P}
          {'action': 'pyramid', 'side': ..., 'qty': N, 'price': P}
          {'action': 'partial_tp', 'side': ..., 'qty': N, 'price': P}
          {'action': 'close_all', 'side': ..., 'qty': N, 'reason': '...'}
          [] — no action
        """
        with self._lock:
            self._current_price = current_price
            self.update_price(current_price)
            actions = []

            # Daily reset
            self._check_daily_reset(now)
            self.trades.check_daily_reset(now)

            # Time guards
            if self._is_night(now) or self._is_clearing(now):
                return []

            # Stale data guard
            if self._is_price_stale():
                return []

            # If in position — check close_price_all FIRST (manual price target)
            if self.in_position and self.p.close_price_all > 0:
                hit = False
                if self._dir == LONG and current_price >= self.p.close_price_all:
                    hit = True
                elif self._dir == SHORT and current_price <= self.p.close_price_all:
                    hit = True
                if hit:
                    self.p.close_price_all = 0.0  # reset after trigger
                    log.info(f"CLOSE_PRICE_ALL triggered: price={current_price:.0f} dir={self._dir}")
                    action = self._close_all(current_price, "close_price_all")
                    actions.append(action)
                    self._last_action_time = time.time()
                    return actions

            # If in position — partial TP FIRST (scalp priority)
            if self.in_position:
                # Check partial TP first — scalp by 1 lot (ЗАКОН)
                tp_actions = self._check_partial_tp(current_price)
                if tp_actions:
                    actions.extend(tp_actions)
                    self._last_action_time = time.time()
                    return actions

                # Then check exits (SL, reverse signal, tp_full, timeout)
                exit_action = self._check_exits(current_price, now)
                if exit_action:
                    actions.append(exit_action)
                    self._last_action_time = time.time()
                    return actions  # Exit takes priority over averaging

                # Check averaging
                # Catch-up: adds ALL missed levels, not just one
                avg_actions = self._check_averaging(current_price)
                if avg_actions:
                    actions.extend(avg_actions)
                    return actions

                # Check pyramiding
                pyr_action = self._check_pyramiding(current_price)
                if pyr_action:
                    actions.append(pyr_action)
                    return actions

            else:
                # Not in position — check for entry
                if not self._is_entry_locked():
                    entry_action = self._check_entry(current_price, now)
                    if entry_action:
                        actions.append(entry_action)
                        return actions

            if actions:
                self._last_action_time = time.time()
            return []

    def sync_from_broker(self, broker_data: dict):
        """Sync ONLY prices from broker for PnL display.

        NEVER touches lots, dir, lot_queue — position is tracked internally.
        Updates _avg_price to match broker for accurate PnL.
        """
        broker_avg = broker_data.get('avg_price', 0.0)
        broker_cur = broker_data.get('current_price', 0.0)

        if broker_cur > 0:
            self._broker_current_price = broker_cur
        if broker_avg > 0:
            self._broker_avg_price = broker_avg
        self._broker_sync_time = time.time()

    def update_fill_price(self, fill_price: float, action: str):
        """Update entry prices with REAL broker fill. Exit trades recorded in main_of."""
        if fill_price <= 0:
            return
        if action in ("entry", "average", "pyramid"):
            if action == "entry":
                self._entry_price = fill_price
                self._avg_price = fill_price
                log.info(f"ENTRY price updated from broker: {fill_price:.0f}")
            else:
                old_cost = self._avg_price * (self._total_lots - self.p.lots)
                added = fill_price * self.p.lots
                self._avg_price = (old_cost + added) / self._total_lots if self._total_lots > 0 else fill_price
                log.info(f"{action.upper()} price updated from broker: {fill_price:.0f} → avg={self._avg_price:.0f}")
            if self._lot_queue:
                self._lot_queue[-1] = LotEntry(price=fill_price, side=self._lot_queue[-1].side, lots=self._lot_queue[-1].lots)

    def _check_entry(self, price: float, now: datetime) -> Optional[dict]:
        """Check for entry signal."""
        if not self.signals.has_enough_data:
            return None

        # Daily stop
        if self._daily_stop_hit():
            return None

        # Get OB metrics
        ob = self.ob_tracker.get_metrics(price)
        self._last_ob_imbalance = ob.imbalance

        # OB filter
        if not self._ob_filter_passes(ob):
            return None

        # === VAH/VAL GATE ===
        entry_zone = None  # None | "VAH" | "VAL"
        if self.p.use_vah_val and self.vp and self.vp.ready:
            vah = self.vp.vah
            val = self.vp.val
            if vah > 0 and val > 0:
                if self.p.vah_val_mode == "range":
                    # RANGE mode: trade ONLY inside VA, block outside
                    if val <= price <= vah:
                        log.info(f"VAH/VAL RANGE: price={price:.0f} inside VA (VAL={val:.0f}..VAH={vah:.0f}) → PASS")
                    else:
                        log.info(f"VAH/VAL RANGE: price={price:.0f} OUTSIDE VA (VAL={val:.0f}..VAH={vah:.0f}) → BLOCK")
                        return None
                else:
                    # FADE / BREAKOUT mode (original logic): entry at VAH/VAL edges
                    if price >= vah:
                        entry_zone = "VAH"
                        log.info(f"VAH/VAL GATE: price={price:.0f} >= VAH={vah:.0f} (VAL={val:.0f}) → zone=VAH → PASS")
                    elif price <= val:
                        entry_zone = "VAL"
                        log.info(f"VAH/VAL GATE: price={price:.0f} <= VAL={val:.0f} (VAH={vah:.0f}) → zone=VAL → PASS")
                    else:
                        log.info(f"VAH/VAL GATE: price={price:.0f} inside VA (VAL={val:.0f}..VAH={vah:.0f}) → BLOCK")
                        return None  # inside Value Area → no entry
                    if self.p.vah_val_mode == "breakout":
                        pass  # Breakout: already past edge — always pass
                    # fade mode: touch of edge is enough — already pass
            else:
                log.info(f"VAH/VAL GATE: VP vah={vah:.0f} val={val:.0f} — zero values → BLOCK")
                return None
        elif self.p.use_vah_val:
            vp_ready = self.vp.ready if self.vp else False
            log.info(f"VAH/VAL GATE: use_vah_val=True but vp.ready={vp_ready} → BLOCK")
            return None

        # === Generate OF signals ONLY AFTER VAH/VAL gate passes ===
        sigs = self.signals.generate_signals(ob, price, ob_tracker=self.ob_tracker,
                                              use_dm_wall=self.p.use_dm_wall,
                                              use_cvd=self.p.use_cvd,
                                              use_ob_imbalance=False,
                                              use_cvd_accel=self.p.use_cvd_accel)

        if len(sigs) < self.p.signal_confirm_count:
            return None

        # All signals must agree on direction
        directions = set(s.direction for s in sigs)
        if len(directions) > 1:
            return None  # Conflicting signals, skip

        direction = sigs[0].direction

        # Direction filter — block disallowed side
        if self.p.direction_filter == "long" and direction == SHORT:
            log.info("ENTRY BLOCKED by direction_filter=long (signal=SHORT)")
            return None
        if self.p.direction_filter == "short" and direction == LONG:
            log.info("ENTRY BLOCKED by direction_filter=short (signal=LONG)")
            return None

        # VWEMA regime filter — block counter-trend entries
        if self.vwema and self.vwema.ready and self.p.vwema_block_counter:
            td = self.vwema.direction
            if td != 0 and direction != td:
                log.info(f"ENTRY BLOCKED by VWEMA: signal={direction} trend={td} | {self.vwema.state}")
                return None

        # Determine entry type for journal
        if entry_zone:
            entry_type = f"{entry_zone}-Confirm" if direction == (1 if entry_zone == "VAH" else -1) else f"{entry_zone}-Fade"
        else:
            entry_type = "signal"
        side = "buy" if direction == LONG else "sell"
        signal_types = ", ".join(s.signal_type for s in sigs)

        # Execute entry
        self._dir = direction
        self._entry_price = price
        self._avg_price = price
        self._total_lots = self.p.lots
        self._peak_lots = self.p.lots
        self._average_levels = 0
        self._pyramid_levels = 0
        self._last_average_price = price
        self._last_pyramid_price = price
        self._entry_time = now
        self._signal_type = signal_types
        self._lot_queue.append(LotEntry(price=price, side=direction, lots=self.p.lots, added_ts=time.monotonic()))

        self._lock_entry(10.0)  # 10 sec entry lock

        log.info(f"ENTRY {side} {self.p.lots} @ {price:.0f} | signals: {signal_types}")

        return {
            'action': 'entry',
            'side': side,
            'qty': self.p.lots,
            'price': price,
            'signal': signal_types,
        }

    def _check_exits(self, price: float, now: datetime) -> Optional[dict]:
        """Check all exit conditions."""

        # a) Stop-loss
        if self._stop_loss_hit(price):
            return self._close_all(price, f"stop_loss_{self.p.stop_loss_mode}")

        # a2) VWEMA avg-exit: усреднённая позиция против тренда VWEMA → закрыть немедленно (B2)
        if (self.p.vwema_avg_exit and self._average_levels > 0
                and self.vwema and self.vwema.ready):
            td = self.vwema.direction
            if td != 0 and td != self._dir:
                log.info(f"VWEMA AVG EXIT: trend={td} vs pos={self._dir} | avg_lvl={self._average_levels} | {self.vwema.state}")
                return self._close_all(price, "vwema_avg_exit")

        # b) Reverse signal
        ob = self.ob_tracker.get_metrics(price)
        if self.signals.check_reverse_signal(ob, self._dir, price, ob_tracker=self.ob_tracker,
                                              confirm_count=self.p.signal_confirm_exit,
                                              use_dm_wall=self.p.use_dm_wall,
                                              use_cvd=self.p.use_cvd,
                                              use_ob_imbalance=False,
                                              use_cvd_accel=self.p.use_cvd_accel):
            contra = [s.signal_type for s in self.signals.generate_signals(ob, price, ob_tracker=self.ob_tracker,
                                                                          use_dm_wall=self.p.use_dm_wall,
                                                                          use_cvd=self.p.use_cvd,
                                                                          use_ob_imbalance=False,
                                                                          use_cvd_accel=self.p.use_cvd_accel) if s.direction != self._dir]
            log.info(f"REVERSE exit signals: {', '.join(contra) or 'unknown'}")
            return self._close_all(price, "reverse_signal")

        # c) Full TP
        if self._tp_full_hit(price):
            return self._close_all(price, "tp_full")

        # d) Timeout
        if self._timeout_hit(now):
            return self._close_all(price, "timeout")

        return None

    def _check_averaging(self, price: float) -> list[dict]:
        """Check if we should average (add against position).

        Catch-up mode: if price has moved multiple steps beyond the last level,
        immediately average ALL missed levels at current price.
        Returns a list of actions (empty if nothing to do).
        """
        if self.p.enable_max_levels and self._average_levels >= self.p.max_average_levels:
            return []

        # B2: не усреднять против тренда VWEMA
        if self.p.vwema_avg_exit and self.vwema and self.vwema.ready:
            td = self.vwema.direction
            if td != 0 and td != self._dir:
                return []

        # Use extreme price from queue to prevent re-averaging on same levels
        if self._lot_queue:
            if self._dir == SHORT:
                effective_price = max(e.price for e in self._lot_queue)
            else:
                effective_price = min(e.price for e in self._lot_queue)
        else:
            return []

        step = self._effective_step(average=True)
        if self._dir == LONG:
            distance = effective_price - price  # how much price fell
        else:
            distance = price - effective_price  # how much price rose

        if distance < step:
            return []

        # Catch-up: calculate how many levels were missed
        missed = int(distance // step)

        # Cap by max levels
        if self.p.enable_max_levels:
            remaining = self.p.max_average_levels - self._average_levels
            missed = min(missed, remaining)

        # Safety cap: max 10 per tick to avoid runaway
        missed = min(missed, 10)

        if missed <= 0:
            return []

        side = "buy" if self._dir == LONG else "sell"
        actions = []

        for _ in range(missed):
            old_lots = self._total_lots
            old_cost = self._avg_price * old_lots

            self._total_lots += self.p.lots
            self._peak_lots = max(self._peak_lots, self._total_lots)
            self._avg_price = (old_cost + price * self.p.lots) / self._total_lots
            self._average_levels += 1
            self._last_average_price = price
            self._lot_queue.append(LotEntry(price=price, side=self._dir, lots=self.p.lots, added_ts=time.monotonic()))

            actions.append({
                'action': 'average',
                'side': side,
                'qty': self.p.lots,
                'price': price,
                'level': self._average_levels,
            })

        if missed > 1:
            log.info(f"AVERAGE CATCHUP {side} {missed}×{self.p.lots} @ {price:.0f} | missed={missed} levels | lvl={self._average_levels}/{self.p.max_average_levels} | avg={self._avg_price:.0f} lots={self._total_lots}")
        else:
            log.info(f"AVERAGE {side} {self.p.lots} @ {price:.0f} | lvl {self._average_levels}/{self.p.max_average_levels} | avg={self._avg_price:.0f} lots={self._total_lots}")

        return actions

    def sync_lot_count(self, broker_lots: int):
        """Synchronize lot queue with actual broker position.

        Removes oldest entries (FIFO) if internal count exceeds broker.
        Handles manual closes by the user.
        """
        if broker_lots < 0:
            broker_lots = 0
        diff = self._total_lots - broker_lots
        if diff <= 0:
            return
        # Remove oldest entries (FIFO) to match broker
        while len(self._lot_queue) > 0 and diff > 0:
            removed = self._lot_queue.popleft()
            log.info(f"SYNC: removed lot @ {removed.price:.0f} (broker has fewer lots)")
            diff -= 1
        # Recalculate avg and total from remaining queue
        self._total_lots = sum(e.lots for e in self._lot_queue)
        if self._lot_queue:
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            self._avg_price = total_cost / self._total_lots if self._total_lots > 0 else 0
            if self._dir == SHORT:
                self._last_average_price = max(e.price for e in self._lot_queue)
            else:
                self._last_average_price = min(e.price for e in self._lot_queue)
            # Cap average_levels to not exceed remaining queue minus entry lot
            self._average_levels = min(self._average_levels, max(0, self._total_lots - 1))
        else:
            self._reset_position()
        log.info(f"SYNC complete: broker={broker_lots} internal={self._total_lots} avg={self._avg_price:.0f} levels={self._average_levels}")

    def _check_pyramiding(self, price: float) -> Optional[dict]:
        """Check if we should pyramid (add in profit direction)."""
        if self._pyramid_levels >= self.p.max_pyramid_levels:
            return None

        # Must be in profit
        if self.unrealized_pnl(price) <= 0:
            return None

        # Step distance check
        step = self._effective_step(average=False)
        if self._dir == LONG:
            distance = price - self._last_pyramid_price  # how much price rose
        else:
            distance = self._last_pyramid_price - price  # how much price fell

        if distance < step:
            return None

        # Execute pyramiding
        side = "buy" if self._dir == LONG else "sell"
        old_lots = self._total_lots
        old_cost = self._avg_price * old_lots

        self._total_lots += self.p.lots
        self._peak_lots = max(self._peak_lots, self._total_lots)
        self._avg_price = (old_cost + price * self.p.lots) / self._total_lots
        self._pyramid_levels += 1
        self._last_pyramid_price = price
        self._lot_queue.append(LotEntry(price=price, side=self._dir, lots=self.p.lots, added_ts=time.monotonic()))

        log.info(f"PYRAMID {side} {self.p.lots} @ {price:.0f} | lvl {self._pyramid_levels}/{self.p.max_pyramid_levels} | avg={self._avg_price:.0f} lots={self._total_lots}")

        return {
            'action': 'pyramid',
            'side': side,
            'qty': self.p.lots,
            'price': price,
            'level': self._pyramid_levels,
        }

    def _check_partial_tp(self, price: float) -> list[dict]:
        """Partial TP — close ALL profitable lots (LIFO — most recent first).

        Catch-up mode: if multiple lots are in profit beyond spread (e.g. after
        restart/gap), closes them all at once instead of one per tick.

        close_half_pct mode: partial_tp closes lots one by one (LIFO) until
        total_lots <= max(2, floor(peak * pct/100)), then closes ALL remaining
        at once via close_all. Minimum threshold = 2 lots.
        If close_half_pct=0 (OFF): normal partial_tp for all lots.
        Returns a list of actions (empty if nothing to close).
        """
        if not self.p.partial_tp or len(self._lot_queue) == 0:
            return []

        # Check X% threshold — close remaining batch when lots <= threshold
        pct = self.p.close_half_pct
        if pct > 0 and self._peak_lots > 0:
            threshold = max(2, int(self._peak_lots * pct / 100.0))
            if self._total_lots <= threshold:
                # Check if LIFO lot meets spread condition
                last = self._lot_queue[-1]
                if last.added_ts > 0 and (time.monotonic() - last.added_ts) < 2.0:
                    return []
                pnl_pts = (price - last.price) * last.side
                if pnl_pts >= self.p.spread:
                    side = "sell" if self._dir == LONG else "buy"
                    qty = self._total_lots
                    log.info(f"CLOSE_HALF_PCT({pct}%) {side} {qty} @ {price:.0f} | peak={self._peak_lots} threshold={threshold}")
                    return [self._close_all(price, f"close_half_{pct}pct")]
                else:
                    return []

        actions = []

        while self._lot_queue:
            last = self._lot_queue[-1]

            # Grace period: don't TP a lot that was just added (2 sec)
            if last.added_ts > 0 and (time.monotonic() - last.added_ts) < 2.0:
                break

            pnl_pts = (price - last.price) * last.side

            if pnl_pts < self.p.spread:
                break

            # Close this lot — SELL if LONG, BUY if SHORT
            side = "sell" if self._dir == LONG else "buy"

            # Trade and PnL recorded in main_of.py after real broker fill
            self._total_lots -= last.lots
            self._lot_queue.pop()  # LIFO — remove from end

            actions.append({
                'action': 'partial_tp',
                'side': side,
                'qty': last.lots,
                'price': price,
                'entryPrice': last.price,
                'entrySide': last.side,
                'signal': self._signal_type,
            })

        if not actions:
            return []

        # Update state after all closes
        if self._lot_queue:
            if self._dir == SHORT:
                self._last_average_price = max(e.price for e in self._lot_queue)
            else:
                self._last_average_price = min(e.price for e in self._lot_queue)
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            total_lots = sum(e.lots for e in self._lot_queue)
            self._avg_price = total_cost / total_lots if total_lots > 0 else 0
        else:
            # All closed via partial TP
            self._last_average_price = self._avg_price
            self._reset_position()
            self._lock_entry(10.0)
            self._round_trips += 1

        if len(actions) > 1:
            log.info(f"PARTIAL_TP CATCHUP {len(actions)} lots @ {price:.0f} | remaining={self._total_lots}")
        else:
            a = actions[0]
            log.info(f"PARTIAL_TP {a['side']} {a['qty']} @ {price:.0f} | remaining={self._total_lots}")

        return actions

    def _close_all(self, price: float, reason: str) -> dict:
        """Close entire position. Trade and PnL recorded in main_of.py after real broker fill."""
        side = "sell" if self._dir == LONG else "buy"
        qty = self._total_lots
        self._round_trips += 1

        log.info(f"CLOSE_ALL {side} {qty} @ {price:.0f} | reason={reason}")

        # Reset happens in main_of._execute_action AFTER order fill
        self._lock_entry(10.0)

        return {
            'action': 'close_all',
            'side': side,
            'qty': qty,
            'price': price,
            'reason': reason,
        }

    def rollback_pending_entry(self, qty: int):
        """Remove the last qty lots from queue (order was not placed/filled)."""
        removed = 0
        while self._lot_queue and removed < qty:
            lot = self._lot_queue.pop()
            removed += lot.lots
            self._total_lots -= lot.lots
        if self._total_lots <= 0:
            self._total_lots = 0
            self._dir = FLAT
            self._lot_queue.clear()
            self._entry_time = None
            self._signal_type = ""
        elif self._total_lots > 0:
            # Recalculate avg from remaining lots
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            self._avg_price = total_cost / self._total_lots
        log.info(f"ROLLBACK: removed {removed} lot(s) | remaining={self._total_lots}")

    def _reset_position(self):
        """Reset all position state."""
        self._dir = FLAT
        self._entry_price = 0.0
        self._avg_price = 0.0
        self._total_lots = 0
        self._peak_lots = 0
        self._average_levels = 0
        self._pyramid_levels = 0
        self._last_average_price = 0.0
        self._last_pyramid_price = 0.0
        self._lot_queue.clear()
        self._entry_time = None
        self._signal_type = ""
        self._broker_avg_price = 0.0

    def _effective_step(self, average: bool = True) -> int:
        """Get effective step (with optional ATR adaptation)."""
        if not self.p.step_atr:
            return self.p.step_average if average else self.p.step_pyramid
        # ATR-adaptive: step = base × (ATR / 50), clamped to [base/2, base×3]
        atr = self.signals._get_atr()
        base = self.p.step_average if average else self.p.step_pyramid
        adapted = int(base * (atr / 50.0))
        return max(base // 2, min(adapted, base * 3))

    # ---------- State persistence ----------

    def get_state(self) -> dict:
        """Get state dict for persistence."""
        return {
            "dir": self._dir,
            "entryPrice": self._entry_price,
            "avgPrice": self._avg_price,
            "totalLots": self._total_lots,
            "averageLevels": self._average_levels,
            "pyramidLevels": self._pyramid_levels,
            "lastAveragePrice": self._last_average_price,
            "lastPyramidPrice": self._last_pyramid_price,
            "peakLots": self._peak_lots,
            "lotQueue": [{"price": e.price, "side": e.side, "lots": e.lots} for e in self._lot_queue],
            "roundTrips": self._round_trips,
            "realizedPnL": self._realized_pnl,
            "dailyPnL": self._daily_pnl,
            "dailyPnLDate": self._daily_pnl_date,
            "entryTime": self._entry_time.isoformat() if self._entry_time else "",
            "signalType": self._signal_type,
            "tradeHistory": self._trade_history[-200:],  # Last 200 trades
        }

    def load_state(self, state: dict):
        """Load state from dict."""
        self._dir = state.get("dir", FLAT)
        self._entry_price = state.get("entryPrice", 0.0)
        self._avg_price = state.get("avgPrice", 0.0)
        self._total_lots = state.get("totalLots", 0)
        self._average_levels = state.get("averageLevels", 0)
        self._pyramid_levels = state.get("pyramidLevels", 0)
        self._last_average_price = state.get("lastAveragePrice", 0.0)
        self._last_pyramid_price = state.get("lastPyramidPrice", 0.0)
        self._peak_lots = state.get("peakLots", 0)
        # Fallback: if peak_lots not saved (old state), recalculate from queue
        if self._peak_lots == 0 and self._total_lots > 0:
            self._peak_lots = self._total_lots
        self._round_trips = state.get("roundTrips", 0)
        self._realized_pnl = state.get("realizedPnL", 0.0)
        self._daily_pnl = state.get("dailyPnL", 0.0)
        self._daily_pnl_date = state.get("dailyPnLDate")
        et = state.get("entryTime", "")
        if et:
            self._entry_time = datetime.fromisoformat(et)
            # Ensure timezone-aware (MSK)
            if self._entry_time.tzinfo is None:
                self._entry_time = self._entry_time.replace(tzinfo=MSK)
        else:
            self._entry_time = None
        self._signal_type = state.get("signalType", "")
        self._trade_history = state.get("tradeHistory", [])

        # Restore lot queue from state (exact)
        lot_q = state.get("lotQueue", [])
        if lot_q:
            for e in lot_q:
                self._lot_queue.append(LotEntry(
                    price=e.get("price", 0.0),
                    side=e.get("side", LONG),
                    lots=e.get("lots", 0),
                    added_ts=0.0,  # No grace for restored lots
                ))
            log.info(f"Restored position: dir={self._dir} lots={self._total_lots} avg={self._avg_price:.0f} lotEntries={len(self._lot_queue)}")
        elif self._total_lots > 0 and self._avg_price > 0:
            # Fallback for old state files without lotQueue
            self._lot_queue.append(LotEntry(
                price=self._avg_price,
                side=self._dir if self._dir != 0 else LONG,
                lots=self._total_lots,
                added_ts=0.0,  # No grace for restored lots
            ))
            log.warning(f"Restored position without lotQueue — single entry: dir={self._dir} lots={self._total_lots}")

    # ---------- Status ----------

    def get_status(self) -> dict:
        """Get full status for UI/API."""
        return {
            "dir": self._dir,
            "direction": "LONG" if self._dir == LONG else ("SHORT" if self._dir == SHORT else "FLAT"),
            "entryPrice": self._entry_price,
            "avgPrice": self._avg_price,
            "totalLots": self._total_lots,
            "peakLots": self._peak_lots,
            "averageLevels": self._average_levels,
            "pyramidLevels": self._pyramid_levels,
            "maxAverageLevels": self.p.max_average_levels,
            "maxPyramidLevels": self.p.max_pyramid_levels,
            "lotQueueSize": len(self._lot_queue),
            "roundTrips": self._round_trips,
            "realizedPnL": self._realized_pnl,
            "unrealizedPnL": self.unrealized_pnl(self._current_price) if self._dir != FLAT and self._current_price > 0 else 0.0,
            "holdMinutes": int((datetime.now(MSK).timestamp() - self._entry_time.timestamp()) / 60) if self._entry_time and self._dir != FLAT else 0,
            "dailyPnL": self._daily_pnl,
            "entryTime": self._entry_time.isoformat() if self._entry_time else "",
            "signalType": self._signal_type,
            "tradeHistory": self._trade_history[-200:],
            "lastDelta": self._last_delta,
            "cvd": self.trades.cvd,
            "cvdTrend": {
                "direction": self.signals.get_cvd_trend_direction(),
                "emaFast": round(self.signals._cvd_ema_fast, 0) if self.signals._cvd_trend_ready else None,
                "emaSlow": round(self.signals._cvd_ema_slow, 0) if self.signals._cvd_trend_ready else None,
                "ready": self.signals._cvd_trend_ready,
            },
            "cvdAccel": {
                "value": round(self.signals._bar_history[-1].cvd - self.signals._bar_history[-(self.signals._cvd_accel_period + 1)].cvd, 0) if len(self.signals._bar_history) > self.signals._cvd_accel_period else None,
                "threshold": self.signals._cvd_accel_threshold,
                "aggRatio": round(self.signals.get_agg_ratio(), 2),
                "aggThreshold": self.signals._agg_ratio_threshold,
            },
            "barsReady": self.signals.bars_ready,
            "entryLocked": self._is_entry_locked(),
            "priceStale": self._is_price_stale(),
            "lastPriceUpdate": self._last_price_update,
            "broker": {
                "avgPrice": self._broker_avg_price,
                "currentPrice": self._broker_current_price,
                "syncAgeSec": round(time.time() - self._broker_sync_time, 1) if self._broker_sync_time > 0 else None,
            },
            "vwema": self.vwema.state if self.vwema else None,
            "vp": self.vp.state if self.vp else None,
            "directionFilter": self.p.direction_filter,
            "dmWallAgree": self._last_dm_wall,
        }
