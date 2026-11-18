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

def fmt_price(p) -> str:
    """Format price with up to 2 decimals, trailing zeros stripped.
    Si/MX (integer ticks): 86397.0 -> '86397'. BR (0.01 ticks): 97.51 -> '97.51'."""
    try:
        s = f"{float(p):.2f}"
        return s.rstrip("0").rstrip(".") if "." in s else s
    except Exception:
        return str(p)


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
    # F-198 (18.11.2026): догон пропущенных partial TP после обрыва данных.
    # Если связь пропадала дольше catchup_tp_min_gap_sec и за это окно цена проходила
    # уровень тейка LIFO-лота, при восстановлении связи лот закрывается — но только
    # если профит по нему ещё есть (не фиксируем убыток и не закрываем «в минус»).
    catchup_tp_enabled: bool = True
    catchup_tp_min_gap_sec: int = 30
    # F-199 (18.11.2026): периодическая проверка пробоя уровня тейка по M1-барам.
    # Даже без обрыва связи отдельные тики теряются (в 15:40 цена коснулась 246800,
    # робот тик не получил -> partial TP не сработал). Раз в scan_sec робот смотрит
    # последние scan_window_sec минутных баров: если high (LONG) / low (SHORT) пробил
    # уровень LIFO-лота и профит ещё есть — лот закрывается.
    tp_bar_scan_enabled: bool = True
    tp_bar_scan_sec: int = 60
    tp_bar_scan_window_sec: int = 240
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
    usd_rate: float = 80.0       # F-055: USD/RUB rate — BR margin is USD-denominated (PnL = dUSD * rate)
    direction_filter: str = "both"  # "both" | "long" | "short"
    # Custom trading window (HH:MM MSK). Empty = full session (07:00-23:50, legacy).
    # Outside the window: no new entries; position is force-closed (session_close).
    session_start: str = ""       # e.g. "10:30"
    session_end: str = ""         # e.g. "18:30"
    # F-064: Торговый тайминг с семантикой «додержать позицию» (Дмитрий 12.10.2026).
    # use_timing ON + окно задано: внутри окна — полная торговля; после end — НОВЫЕ
    # циклы запрещены, но открытая позиция продолжает вестись штатно (усреднение/
    # пирамида/выходы) до закрытия. Если позиция закрылась уже после старта нового
    # дня → торговля продолжается (стейтлесс-гейт, без флагов). Legacy session_*
    # (форс-закрытие) — независимый механизм, не трогаем.
    use_timing: bool = False
    timing_start: str = ""        # "09:50"
    timing_end: str = ""          # "17:55"
    # F-073: Pyramid Full Exit (Дмитрий 16.10.2026, Si-роботы).
    # При позиции ≥ 3 лотов (1 вход + ≥2 пирамиды) выходы только два:
    #   (1) откат от цены ПОСЛЕДНЕГО пирамид-входа на 1 pyr-уровень → close_all;
    #   (2) PnL/лот ≥ min_profit_per_lot → close_all (tp_full, как обычно).
    # При 1-2 лотах правило не действует — обычная торговля (усреднение, LIFO-тейки).
    # Выключено по умолчанию; включается в конфиге конкретного робота.
    pyramid_full_exit: bool = False
    # F-198: режим Trailing SL — прибыль не фиксируется маркетом (partial TP/tp_full отключены),
    # а запирается брокерским стопом, который трейлится за ценой.
    trail_mode: bool = False
    trail_sl_pts: float = 12.0     # дистанция стопа от Bid (LONG) / Ask (SHORT), пт
    trail_step_pts: float = 5.0    # шаг подтяжки стопа, пт
    # VAH/VAL Volume Profile filter
    use_vah_val: bool = False      # enable VAH/VAL entry filter
    vah_val_pct: int = 70          # value area percentage (70%)
    vah_val_bin_size: int = 5      # VP bin size in points
    vah_val_mode: str = "range"     # "fade" = counter-trend at VAH/VAL, "breakout" = trend with VAH/VAL
    vah_val_block_inside: bool = False  # F-091: True = внутри VA торговля запрещена полностью (Range v2)
    # VWEMA regime filter
    use_vwema: bool = False       # enable VWEMA trend filter
    vwema_fast: int = 20          # fast VWEMA period
    vwema_slow: int = 40          # slow VWEMA period
    vwema_flat_th: float = 1.0    # flat zone threshold (ATR multiples)
    vwema_block_counter: bool = True  # block counter-trend entries
    vwema_avg_exit: bool = False    # B2: не усреднять против VWEMA + закрыть усреднённую позицию при развороте VWEMA
    # HL filter (only_strict): вход лонгом только у лоу окна, шортом только у хая окна
    hl_filter: bool = False        # OFF по умолчанию — поведение как раньше
    hl_window: int = 24            # окно в барах (24 × M5 = 2 часа)
    hl_delta: float = 150.0        # «близко к экстремуму» в пунктах
    # F-191: HL — «фейдовый» фильтр (вход у экстремума окна). Трендовый CVD Trend
    # по определению запаздывает (цена уже ушла на 1+ ATR), поэтому к нему HL по
    # умолчанию НЕ применяем — иначе вход по кроссоверу структурно невозможен
    # (инцидент 11.11: SHORT-кроссовер @83459.5 при хае 83614, Δ=55 → блок,
    # хотя ATR=151 и сигнал был верный). True = старое поведение (HL для всех).
    hl_apply_cvd_trend: bool = False
    # F-191: сколько секунд живёт отложенный сигнал CVD Trend (0 = бессрочно).
    # Сигнал больше НЕ съедается гейтами: ждёт первого тика, где прошли все фильтры.
    pending_signal_ttl_sec: float = 300.0
    # F-067: Phase Zones — зонный фильтр направления (Donchian-канал, трети).
    # phase_filter ON + окно: цена в нижней трети канала → только LONG,
    # в верхней трети → только SHORT, в середине (обе границы не задеты) → оба.
    # off по умолчанию (R-002). Гистерезис phase_hysteresis: выход из зоны происходит
    # на hysteresis*range дальше границы (против «дребезга» на границе третей).
    phase_filter: bool = False
    phase_window: int = 2000         # окно канала в барах (96 × M5 = 8 часов)
    phase_lower: float = 0.33      # граница нижней зоны (доля канала)
    phase_upper: float = 0.67      # граница верхней зоны (доля канала)
    phase_hysteresis: float = 0.05 # гистерезис выхода из зоны (доля канала)


@dataclass
class LotEntry:
    """Single lot in the position queue (for LIFO partial TP)."""
    price: float
    side: int     # LONG or SHORT (buy=1, sell=-1)
    lots: int     # how many lots at this price
    added_ts: float = 0.0  # time.monotonic() when lot was added (grace period)
    uid: int = 0          # F-198: стабильный id слоя (ключ реестра трейлинг-стопов)


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
        bin_key = price // self.bin_size * self.bin_size
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
        self._avg_va_block_logged: bool = False
        # F-067: Phase Zones state (гистерезис направления зоны)
        self._phase_dir: int = 0           # 1 = LONG-зона, -1 = SHORT-зона, 0 = BOTH/не готово
        self._phase_last_log: str = ""
        self._pending_cvd_trend: Optional[dict] = None   # F-067b: сигнал кроссовера, ждущий входа
        self._blind_since: Optional[float] = None       # F-198: начало «слепого окна» (epoch)
        self._tp_scan_ts: float = 0.0                   # F-199: время последнего скана баров
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

        # F-073 (fix 19.10): цена входа в последний ПИРАМИД-уровень. Обновляется только
        # в _check_pyramiding; усреднение/manual_adjust её НЕ трогают. 0 = пирамид нет.
        self._last_pyr_entry_price: float = 0.0

        # F-198: реестр трейлинг-стопов. Ключ — uid слоя (LotEntry.uid).
        # {uid: {order_id, stop_px, qty, best, ts}}; agg — консолидированный стоп
        # на весь объём (Close % All или пирамида от 3 лотов).
        self._trail_stops: dict[int, dict] = {}
        self._agg_trail: Optional[dict] = None
        self._trail_uid_seq: int = 0
        self._trail_closed_lots: int = 0   # сколько лотов закрыто трейлинг-стопами

        # F-087 fix: фазовый канал из /api/candles (единый источник с UI-графиком).
        # Обновляется раз в 60с в _phase_channel_refresh; живые бары его не вытесняют.
        self._phase_chan_high: float = 0.0
        self._phase_chan_low: float = 0.0
        self._phase_chan_ts: float = 0.0

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

    def _is_outside_session(self, now: datetime) -> bool:
        """Custom trading window check (session_start/session_end in MSK, HH:MM).
        Empty params = disabled (legacy full-session behavior)."""
        if not self.p.session_start or not self.p.session_end:
            return False
        msk = now.astimezone(MSK)
        cur = msk.hour * 60 + msk.minute
        try:
            h1, m1 = self.p.session_start.split(":")
            h2, m2 = self.p.session_end.split(":")
        except ValueError:
            return False
        return cur < int(h1) * 60 + int(m1) or cur > int(h2) * 60 + int(m2)

    @staticmethod
    def _parse_hhmm(s: str) -> Optional[int]:
        """'09:50' → 590 (минуты от полуночи). None если пусто/битое."""
        try:
            s = (s or "").strip()
            if not s or ":" not in s:
                return None
            h, m = s.split(":", 1)
            h, m = int(h), int(m)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return h * 60 + m
        except Exception:
            return None

    def _timing_gate_closed(self, now: datetime) -> bool:
        """F-064: True → новые торговые циклы сейчас запрещены.

        Стейтлесс (чистая функция времени) — рестарты и ночные кейсы обрабатываются
        автоматически, без флагов в состоянии:
          - после timing_end флэт-робот ждёт до следующего дня (timing_start);
          - открытая позиция торгует как обычно (гейт блокирует только НОВЫЕ входы);
          - позиция закрылась после старта нового дня → окно уже открыто, робот
            продолжает торговать; закрылась до старта → ждёт старта.
        Битое/пустое время → гейт неактивен (как в legacy session-проверке).
        """
        if not self.p.use_timing:
            return False
        start = self._parse_hhmm(self.p.timing_start)
        end = self._parse_hhmm(self.p.timing_end)
        if start is None or end is None:
            return False
        msk = now.astimezone(MSK)
        cur = msk.hour * 60 + msk.minute
        if start <= end:
            return not (start <= cur <= end)
        return not (cur >= start or cur <= end)  # окно через полночь (22:00→03:00)

    def is_timing_gate_closed(self) -> bool:
        """F-064: состояние гейта прямо сейчас (для UI /status)."""
        return self._timing_gate_closed(datetime.now(MSK))

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

    def apply_cvd_trend_params(self) -> bool:
        """F-192: пересчитать периоды/альфы CVD Trend после изменения cvd_ema_fast/slow на ходу.

        /params раньше обновлял только params и НЕ трогал альфы движка: UI показывал
        новые периоды, а EMA считались по старым (заметно только при смене на лету).
        Возвращает True, если периоды реально изменились и были применены.
        EMAs сбрасываются в неготовое состояние → на следующем баре INIT (без ложного кроссовера).
        """
        try:
            f = max(1, int(self.p.cvd_ema_fast))
            s = max(2, int(self.p.cvd_ema_slow))
        except (TypeError, ValueError):
            log.warning(f"CVD Trend params invalid: fast={self.p.cvd_ema_fast} slow={self.p.cvd_ema_slow} — ignored")
            return False
        cur_f = int(getattr(self.signals, "_cvd_trend_ema_fast_period", 0) or 0)
        cur_s = int(getattr(self.signals, "_cvd_trend_ema_slow_period", 0) or 0)
        if (f, s) == (cur_f, cur_s):
            return False
        self.signals._cvd_trend_ema_fast_period = f
        self.signals._cvd_trend_ema_slow_period = s
        self.signals._cvd_trend_alpha_f = 2.0 / (f + 1)
        self.signals._cvd_trend_alpha_s = 2.0 / (s + 1)
        # re-seed, чтобы смена периодов не дала ложный кроссовер на старых EMA
        self.signals._cvd_trend_ready = False
        self.signals._cvd_trend_dir = 0
        self.signals._cvd_trend_reverse_count = 0
        log.info(f"CVD Trend params applied: EMA {f}/{s} (alpha {2.0/(f+1):.4f}/{2.0/(s+1):.4f})")
        return True

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

    def _mk_lot_uid(self) -> int:
        """F-198: следующий uid слоя."""
        self._trail_uid_seq += 1
        return self._trail_uid_seq

    def _mk_lot(self, price: float, side: int, lots: int,
                added_ts: float = 0.0, uid: Optional[int] = None) -> LotEntry:
        """F-198: создать слой с уникальным uid (ключ реестра трейлинг-стопов)."""
        if uid is None or int(uid) <= 0:
            uid = self._mk_lot_uid()
        else:
            self._trail_uid_seq = max(self._trail_uid_seq, int(uid))
        lot = LotEntry(price=price, side=side, lots=lots, added_ts=added_ts, uid=int(uid))
        self._lot_queue.append(lot)
        return lot

    def _last_pyramid_entry_price(self) -> Optional[float]:
        """F-073 (fix 19.10): цена входа в ПОСЛЕДНИЙ ПИРАМИД-уровень.

        ВАЖНО: усреднение и пирамида пишут в lot_queue без маркировки, поэтому
        брать последний слой очереди НЕЛЬЗЯ — там может быть AVERAGE-слой
        (инцидент 19.10 12:02: позиция 1 entry + 2 AVERAGE закрылась как
        pyramid_full_exit). Теперь цена хранится в _last_pyr_entry_price и
        обновляется ТОЛЬКО в _check_pyramiding.
        """
        if self._last_pyr_entry_price <= 0:
            return None
        return self._last_pyr_entry_price

    def _pyramid_pullback_hit(self, price: float) -> bool:
        """F-073: откат от последнего pyr-входа на 1 pyr-уровень (позиция ≥ 3 лотов).

        LONG: цена упала ниже (last_pyr_entry − step_pyramid) → True.
        SHORT: цена выросла выше (last_pyr_entry + step_pyramid) → True.
        """
        if not self.p.pyramid_full_exit:
            return False
        if not self.in_position or self._total_lots < 3:
            return False
        ref = self._last_pyramid_entry_price()
        if ref is None or ref <= 0:
            return False
        step = self._effective_step(average=False)
        if step <= 0:
            return False
        if self._dir == LONG:
            return price <= ref - step
        return price >= ref + step

    def sync_pyramid_broker_stop(self, price: float, place_stop_fn=None, cancel_fn=None) -> None:
        """F-094: держит у брокера STOP-ордер на закрывающую сторону при активном
        pyramid_full_exit и позиции ≥3 лота с пирамидами.

        Уровень: last_pyr_entry ± 1 pyr-step (тот же триггер, что pullback).
        Переставляется при каждом новом pyr-входе; снимается при закрытии позиции,
        выключении правила или когда пирамид нет (pyr=0 — закон не действует).

        place_stop_fn(side, qty, stop_price, tag) / cancel_fn(order_id) — колбэки
        main_of (брокерские вызовы), вызываются только при изменении состояния.
        """
        # F-198: в режиме Trailing SL старый PYR STOP не используется — его роль
        # играет трейлинг-стоп (sync_trail_stops). Старую заявку снимаем.
        if self.p.trail_mode:
            stop_id = getattr(self, "_pyr_stop_order_id", None)
            if stop_id and cancel_fn:
                try:
                    cancel_fn(stop_id)
                except Exception:
                    pass
            self._pyr_stop_order_id = None
            return

        if not self.p.pyramid_full_exit:
            need = False
        elif not self.in_position or self._pyramid_levels <= 0:
            # F-193: стоп ставится сразу с ПЕРВЫМ уровнем пирамиды.
            # Раньше требовалось _total_lots >= 3 — в результате позиция
            # «1 вход + 1 пирамида» (2 лота) оставалась без брокерского стопа
            # на все 4 минуты до следующего долива (инцидент 11.11 15:25-15:29:
            # SHORT 2 лота, стоп появился только на 3-м лоте).
            # Порог 3 лота остаётся только для правила pullback-close (F-073),
            # но не для защиты уровней пирамидинга.
            need = False
        else:
            ref = self._last_pyramid_entry_price()
            need = ref is not None and ref > 0

        stop_id = getattr(self, "_pyr_stop_order_id", None)

        if not need:
            if stop_id and cancel_fn:
                try:
                    cancel_fn(stop_id)
                    log.info(f"PYR STOP canceled: {stop_id} (rule inactive)")
                except Exception as e:
                    log.warning(f"PYR STOP cancel failed: {e}")
            if stop_id:
                self._pyr_stop_order_id = None
            return

        ref = self._last_pyramid_entry_price()
        step = self._effective_step(average=False)
        stop_px = (ref - step) if self._dir == LONG else (ref + step)
        qty = self._total_lots
        close_side = "sell" if self._dir == LONG else "buy"

        # F-194: НЕ ставить стоп, если его уровень уже ПРОЙДЕН рынком.
        # Инцидент 11.11 15:46 (Si): после рестарта цена (83401) была уже ниже
        # уровня стопа (83443.5) — LAST_DOWN-стоп исполнился в ту же секунду
        # рыночной продажей 4 лотов, одновременно отработал pullback-close_all
        # → позиция перевернулась в ШОРТ (−6). Стоп с ценой за рынком — это
        # не защита, а рыночный ордер; в таком случае позицию закрывает
        # штатное правило pullback (F-073), а стоп не выставляется.
        cur = float(self._current_price or price or 0.0)
        if cur > 0:
            already_passed = ((self._dir == LONG and stop_px >= cur)
                              or (self._dir == SHORT and stop_px <= cur))
            if already_passed:
                if stop_id and cancel_fn:
                    try:
                        cancel_fn(stop_id)
                    except Exception:
                        pass
                self._pyr_stop_order_id = None
                self._pyr_stop_price = 0.0
                self._pyr_stop_qty = 0
                log.warning(
                    f"PYR STOP skipped (F-194): level {fmt_price(stop_px)} already passed "
                    f"(price={fmt_price(cur)}, dir={self._dir}) — no instant market trigger")
                return

        # перестановка нужна если: стопа нет, или уровень/объём изменились
        prev_px = getattr(self, "_pyr_stop_price", 0.0)
        prev_qty = getattr(self, "_pyr_stop_qty", 0)
        if stop_id and abs(prev_px - stop_px) < 1e-9 and prev_qty == qty:
            return  # актуален

        if stop_id and cancel_fn:
            try:
                cancel_fn(stop_id)
                log.info(f"PYR STOP re-place: cancel {stop_id}")
            except Exception as e:
                log.warning(f"PYR STOP re-place cancel failed: {e}")

        if place_stop_fn:
            placed = place_stop_fn(close_side, qty, stop_px, "pyr_full_exit_stop")
            new_id = getattr(placed, "order_id", None) if placed else None
            self._pyr_stop_order_id = new_id
            self._pyr_stop_price = stop_px
            self._pyr_stop_qty = qty
            import time as _t
            self._pyr_stop_placed_ts = _t.time()  # F-094 guard: возраст стопа для FLAT-синка
            log.info(f"PYR STOP placed: {close_side} {qty} @ {fmt_price(stop_px)} (ref={fmt_price(ref)} step={fmt_price(step)}) id={new_id}")

    # ---------- F-198: Trailing SL (по-лотовый + агрегированный) ----------

    def _trail_price_basis(self, bid: float, ask: float):
        """База и уровень стопа: для LONG считаем от Bid, для SHORT — от Ask."""
        sl = float(self.p.trail_sl_pts)
        if self._dir == LONG:
            return bid, bid - sl
        return ask, ask + sl

    def _trail_level_ok(self, base: float, stop_px: float) -> bool:
        """F-194: стоп за рынком не выставляем (иначе мгновенный рыночный триггер)."""
        if stop_px <= 0 or base <= 0:
            return False
        return stop_px < base if self._dir == LONG else stop_px > base

    def _trail_agg_needed(self) -> bool:
        """Нужен ли ОДИН стоп на весь объём.

        F-198: (1) пирамида от 3 лотов — сразу; (2) Close % All — когда после
        закрытия части лотов стопами остаток <= max(2, peak*close_half_pct/100).
        """
        if self._total_lots >= 3 and self._pyramid_levels > 0:
            return True
        pct = int(self.p.close_half_pct or 0)
        if pct <= 0 or self._peak_lots <= 0:
            return False
        threshold = max(2, int(self._peak_lots * pct / 100.0))
        return self._total_lots <= threshold and self._trail_closed_lots > 0

    def _cancel_all_trail(self, cancel_fn=None) -> None:
        if cancel_fn:
            for rec in list(self._trail_stops.values()):
                oid = rec.get("order_id")
                if oid:
                    try:
                        cancel_fn(oid)
                    except Exception:
                        pass
            if self._agg_trail and self._agg_trail.get("order_id"):
                try:
                    cancel_fn(self._agg_trail["order_id"])
                except Exception:
                    pass
        self._trail_stops.clear()
        self._agg_trail = None

    def sync_trail_stops(self, bid: float, ask: float,
                         place_stop_fn=None, cancel_fn=None) -> None:
        """F-198: держит трейлинг-стопы. Вызывается каждый тик (live) из main_of.

        Режим off → снимает всё. Позиции нет → снимает всё.
        Агрегат (пирамида 3+/Close % All) — один стоп на весь объём,
        иначе — свой стоп на каждый слой (по 1 лоту), порог активации = spread.
        """
        if not self.p.trail_mode:
            if self._trail_stops or self._agg_trail:
                self._cancel_all_trail(cancel_fn)
            return
        if not self.in_position or self._total_lots <= 0:
            self._cancel_all_trail(cancel_fn)
            return
        if bid <= 0 or ask <= 0 or place_stop_fn is None:
            return
        if self._trail_agg_needed():
            self._sync_agg_trail(bid, ask, place_stop_fn, cancel_fn)
        else:
            self._sync_lot_trails(bid, ask, place_stop_fn, cancel_fn)

    def _sync_lot_trails(self, bid, ask, place_stop_fn, cancel_fn) -> None:
        base, stop_px = self._trail_price_basis(bid, ask)
        if not self._trail_level_ok(base, stop_px):
            return
        if self._agg_trail:
            oid = self._agg_trail.get("order_id")
            if oid and cancel_fn:
                try:
                    cancel_fn(oid)
                except Exception:
                    pass
            self._agg_trail = None

        live = {e.uid: e for e in self._lot_queue if e.uid}
        for uid in list(self._trail_stops):
            if uid not in live:
                rec = self._trail_stops.pop(uid)
                if rec.get("order_id") and cancel_fn:
                    try:
                        cancel_fn(rec["order_id"])
                    except Exception:
                        pass

        now = time.monotonic()
        side = "sell" if self._dir == LONG else "buy"
        for uid, e in live.items():
            rec = self._trail_stops.get(uid)
            if rec is None:
                pnl_pts = (base - e.price) * e.side
                if pnl_pts < float(self.p.spread):
                    continue
                placed = place_stop_fn(side, e.lots, stop_px, "of_trail_sl")
                oid = getattr(placed, "order_id", None) if placed else None
                self._trail_stops[uid] = {"order_id": oid, "stop_px": stop_px,
                                          "qty": e.lots, "best": base, "ts": now}
                log.info(f"TRAIL SL armed: lot#{uid} {side} {e.lots} @ {fmt_price(stop_px)} "
                         f"(lot={fmt_price(e.price)} base={fmt_price(base)}) id={oid}")
                continue
            if now - float(rec.get("ts", 0) or 0) < 1.5:
                continue
            best = float(rec.get("best", base) or base)
            improved = (base - best) if self._dir == LONG else (best - base)
            if improved < float(self.p.trail_step_pts):
                continue
            if rec.get("order_id") and cancel_fn:
                try:
                    cancel_fn(rec["order_id"])
                except Exception:
                    pass
            placed = place_stop_fn(side, e.lots, stop_px, "of_trail_sl")
            oid = getattr(placed, "order_id", None) if placed else None
            rec.update({"order_id": oid, "stop_px": stop_px, "qty": e.lots,
                        "best": base, "ts": now})
            log.info(f"TRAIL SL moved: lot#{uid} {side} {e.lots} @ {fmt_price(stop_px)} id={oid}")

    def _sync_agg_trail(self, bid, ask, place_stop_fn, cancel_fn) -> None:
        base, stop_px = self._trail_price_basis(bid, ask)
        if not self._trail_level_ok(base, stop_px):
            return
        # по-лотовые больше не нужны — снимаем
        for uid, rec in list(self._trail_stops.items()):
            if rec.get("order_id") and cancel_fn:
                try:
                    cancel_fn(rec["order_id"])
                except Exception:
                    pass
            self._trail_stops.pop(uid, None)

        qty = int(self._total_lots)
        now = time.monotonic()
        side = "sell" if self._dir == LONG else "buy"
        rec = self._agg_trail
        if rec is None or int(rec.get("qty", 0)) != qty:
            if rec and rec.get("order_id") and cancel_fn:
                try:
                    cancel_fn(rec["order_id"])
                except Exception:
                    pass
            placed = place_stop_fn(side, qty, stop_px, "of_trail_sl_agg")
            oid = getattr(placed, "order_id", None) if placed else None
            self._agg_trail = {"order_id": oid, "stop_px": stop_px, "qty": qty,
                               "best": base, "ts": now}
            log.info(f"TRAIL SL AGG armed: {side} {qty} @ {fmt_price(stop_px)} "
                     f"(base={fmt_price(base)}) id={oid}")
            return
        if now - float(rec.get("ts", 0) or 0) < 1.5:
            return
        best = float(rec.get("best", base) or base)
        improved = (base - best) if self._dir == LONG else (best - base)
        if improved < float(self.p.trail_step_pts):
            return
        if rec.get("order_id") and cancel_fn:
            try:
                cancel_fn(rec["order_id"])
            except Exception:
                pass
        placed = place_stop_fn(side, qty, stop_px, "of_trail_sl_agg")
        oid = getattr(placed, "order_id", None) if placed else None
        rec.update({"order_id": oid, "stop_px": stop_px, "qty": qty, "best": base, "ts": now})
        log.info(f"TRAIL SL AGG moved: {side} {qty} @ {fmt_price(stop_px)} id={oid}")

    def trail_stop_for_order(self, order_id: str) -> Optional[dict]:
        """F-198: опознать трейл-стоп по order_id (для WS ORDERS)."""
        if not order_id:
            return None
        oid = str(order_id)
        if self._agg_trail and str(self._agg_trail.get("order_id")) == oid:
            return {"kind": "agg"}
        for uid, rec in self._trail_stops.items():
            if str(rec.get("order_id")) == oid:
                return {"kind": "lot", "uid": uid}
        return None

    def apply_trail_stop_fill(self, order_id: str, fill_price: float) -> Optional[dict]:
        """F-198: обработать исполнение трейл-стопа. Возвращает action для журнала."""
        hit = self.trail_stop_for_order(order_id)
        if not hit:
            return None
        if hit["kind"] == "agg":
            qty = int(self._total_lots)
            entry = self._avg_price
            entry_side = self._dir
            self._agg_trail = None
            self._trail_stops.clear()
            self._reset_position()
            log.info(f"TRAIL SL AGG filled @ {fmt_price(fill_price)} qty={qty}")
            return {"action": "close_all", "qty": qty, "avgPrice": entry,
                    "entrySide": entry_side,
                    "price": fill_price, "reason": "trail_sl_agg"}
        uid = hit["uid"]
        self._trail_stops.pop(uid, None)
        lot = next((e for e in self._lot_queue if e.uid == uid), None)
        if lot is None:
            return None
        qty, entry, side = lot.lots, lot.price, lot.side
        self._lot_queue = [e for e in self._lot_queue if e.uid != uid]
        self._total_lots = max(0, self._total_lots - qty)
        self._trail_closed_lots += qty
        if self._total_lots <= 0:
            self._reset_position()
        else:
            total = sum(e.lots for e in self._lot_queue)
            self._avg_price = (sum(e.price * e.lots for e in self._lot_queue) / total) if total else 0.0
        log.info(f"TRAIL SL filled: lot#{uid} qty={qty} @ {fmt_price(fill_price)} "
                 f"| remaining={self._total_lots}")
        return {"action": "partial_tp", "qty": qty, "entryPrice": entry,
                "entrySide": side, "price": fill_price, "reason": "trail_sl"}

    def trail_active_order_ids(self) -> set:
        """F-198: id живых трейл-заявок (для сверки с брокером при старте/ресинке)."""
        ids = {str(r.get("order_id")) for r in self._trail_stops.values() if r.get("order_id")}
        if self._agg_trail and self._agg_trail.get("order_id"):
            ids.add(str(self._agg_trail["order_id"]))
        return ids

    def trail_prune_dead(self, alive_ids) -> None:
        """F-198: выкинуть из реестра стопы, которых больше нет у брокера."""
        alive = {str(x) for x in (alive_ids or [])}
        for uid in list(self._trail_stops):
            oid = str(self._trail_stops[uid].get("order_id") or "")
            if oid and oid not in alive:
                self._trail_stops.pop(uid, None)
        if (self._agg_trail and self._agg_trail.get("order_id")
                and str(self._agg_trail["order_id"]) not in alive):
            self._agg_trail = None

    def _trail_registry_payload(self) -> dict:
        return {
            "stops": {str(uid): rec for uid, rec in self._trail_stops.items()},
            "agg": self._agg_trail,
            "closedLots": self._trail_closed_lots,
            "uidSeq": self._trail_uid_seq,
        }

    def _trail_registry_load(self, data: dict) -> None:
        self._trail_stops = {}
        stops = (data or {}).get("stops") or {}
        for k, rec in stops.items():
            try:
                self._trail_stops[int(k)] = dict(rec)
            except (TypeError, ValueError):
                continue
        self._agg_trail = dict((data or {}).get("agg")) if (data or {}).get("agg") else None
        try:
            self._trail_closed_lots = int((data or {}).get("closedLots", 0) or 0)
        except (TypeError, ValueError):
            self._trail_closed_lots = 0
        try:
            self._trail_uid_seq = max(self._trail_uid_seq, int((data or {}).get("uidSeq", 0) or 0))
        except (TypeError, ValueError):
            pass

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
        # F-064 fix (17.10.2026): CVD Trend EMA должен обновляться на КАЖДОМ закрытом баре,
        # независимо от тайминг-гейта/позиции. Раньше EMA жил только внутри generate_signals
        # (путь входа), который вне окна не вызывался → CVD Trend «замерзал» (ready=False,
        # emaFast=None) и после открытия окна первые 15 баров индикатор был холодным.
        # F-067b fix (20.10): сигнал кроссовера больше НЕ выбрасывается! Раньше on_bar_close
        # поглощал его, и если кроссовер случился вне окна/в позиции — вход по CVD Trend
        # терялся навсегда (баг Дмитрия 20.10: «сигнал есть, входа нет»). Теперь сигнал
        # сохраняется в pending_cvd_trend и забирается первым же process_tick, где штатно
        # пройдут все гейты (тайминг/фаза/направление).
        try:
            _sig = self.signals.check_cvd_trend(self._current_price)
            if _sig is not None:
                self._pending_cvd_trend = {
                    "direction": _sig.direction,
                    "signal_type": _sig.signal_type,
                    "ts": _t.time() if (_t := __import__("time")) else 0,
                    "price": _sig.price,
                }
                log.info(f"PENDING CVD Trend entry saved: {self._pending_cvd_trend}")
        except Exception:
            pass

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
            # F-198: заметить «слепое окно» ДО обновления метки цены
            try:
                _now_ts = time.time()
                _gap_limit = float(getattr(self.p, "catchup_tp_min_gap_sec", 30) or 30)
                if (self._last_price_update > 0
                        and (_now_ts - self._last_price_update) > _gap_limit
                        and self._blind_since is None):
                    self._blind_since = self._last_price_update
                    log.warning(f"BLIND GAP: no price for {_now_ts - self._last_price_update:.0f}s "
                                f"— missed TP will be checked on reconnect")
            except Exception:
                pass
            self._current_price = current_price
            self.update_price(current_price)
            actions = []

            # Daily reset
            self._check_daily_reset(now)
            self.trades.check_daily_reset(now)

            # Time guards
            if self._is_night(now) or self._is_clearing(now):
                return []

            # Session window: outside window → close position, no entries (bactest EOD-equivalent)
            if self._is_outside_session(now):
                if self.in_position:
                    action = self._close_all(current_price, "session_close")
                    actions.append(action)
                    self._last_action_time = time.time()
                    log.info(f"SESSION window end: closed position at {current_price}")
                    return actions
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
                    log.info(f"CLOSE_PRICE_ALL triggered: price={fmt_price(current_price)} dir={self._dir}")
                    action = self._close_all(current_price, "close_price_all")
                    actions.append(action)
                    self._last_action_time = time.time()
                    return actions

            # If in position — partial TP FIRST (scalp priority)
            if self.in_position:
                # F-198: сначала проверить, не был ли пройден тейк, пока данных не было
                _catch = self._catchup_missed_tp(current_price)
                if _catch:
                    actions.extend(_catch)
                    self._last_action_time = time.time()
                    return actions

                # F-199: периодический скан M1-баров на пропущенный пробой уровня тейка
                _scan = self._scan_missed_tp(current_price)
                if _scan:
                    actions.extend(_scan)
                    self._last_action_time = time.time()
                    return actions
                # F-073 (сужено 19.10 по Дмитрию): правило действует ТОЛЬКО если в позиции
                # есть хотя бы ОДИН реальный пирамид-уровень (_pyramid_levels > 0).
                # Позиция из входа+усреднений (pyr=0) живёт по ОБЫЧНЫМ правилам:
                # LIFO-частичные тейки (TP от усреднения), tp_full, SL и т.д.
                # При ≥3 лотах и наличии пирамиды: выходы только (1) откат от последнего
                # pyr-входа на 1 pyr-шаг → close_all; (2) PnL/лот → close_all.
                if (self.p.pyramid_full_exit and self._total_lots >= 3
                        and self._pyramid_levels > 0):
                    if self._pyramid_pullback_hit(current_price):
                        log.info(f"PYRAMID_FULL_EXIT: pullback 1 pyr-step from last pyr entry "
                                 f"{fmt_price(self._last_pyramid_entry_price() or 0)} @ {fmt_price(current_price)} | lots={self._total_lots}")
                        action = self._close_all(current_price, "pyramid_full_exit")
                        actions.append(action)
                        self._last_action_time = time.time()
                        return actions
                    if not self.p.trail_mode and self._tp_full_hit(current_price):
                        action = self._close_all(current_price, "tp_full")
                        actions.append(action)
                        self._last_action_time = time.time()
                        return actions
                    # стоп-лосс/VWEMA-avg-exit/reverse/timeout остаются доступными
                    exit_action = self._check_exits(current_price, now)
                    if exit_action:
                        actions.append(exit_action)
                        self._last_action_time = time.time()
                        return actions
                    # усреднение при просадке разрешено (это пирамид-логика Дмитрия:
                    # робот добирает против отката до 3+ лотов, но при активном правиле
                    # усреднение против позиции — отдельный вопрос; усреднение работает)
                    avg_actions = self._check_averaging(current_price)
                    if avg_actions:
                        actions.extend(avg_actions)
                        return actions
                    pyr_action = self._check_pyramiding(current_price)
                    if pyr_action:
                        actions.append(pyr_action)
                        return actions
                    return []

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
                # F-064: тайминг-гейт — вне окна новые циклы запрещены (флэт → ждём старта).
                # Открытая позиция сюда не доходит: усреднение/пирамида/выходы выше работают штатно.
                if self._timing_gate_closed(now):
                    return []
                if not self._is_entry_locked():
                    entry_action = self._check_entry(current_price, now)
                    if entry_action:
                        actions.append(entry_action)
                        return actions

            if actions:
                self._last_action_time = time.time()
            return []

    def sync_from_broker(self, broker_data: dict, full: bool = False):
        """Sync state from broker.

        full=False (default): ONLY prices for PnL display — lots/dir/queue не трогаются.
        full=True (F-095 «робот = позиция счёта»): робот ПРИНИМАЕТ фактическую позицию
        счёта как свою (dir/lots/avg), очередь очищается и заполняется одним слоем
        по брокерской средней. Вызывается из десинк-монитора при любом расхождении.
        """
        broker_avg = broker_data.get('avg_price', 0.0)
        broker_cur = broker_data.get('current_price', 0.0)
        broker_lots = int(broker_data.get('lots', 0) or 0)
        # F-200: get_broker_position отдаёт lots АБСОЛЮТОМ, направление — в 'dir'.
        # Раньше знак брался из lots → любая SHORT-позиция после full-resync
        # превращалась в LONG (инцидент 18.11 16:20: шорт −2 стал «LONG 2»).
        broker_dir = int(broker_data.get('dir', 0) or 0)
        if broker_dir == 0 and broker_lots == 0:
            signed_lots = 0
        else:
            signed_lots = abs(broker_lots) * (broker_dir if broker_dir else 1)

        if broker_cur > 0:
            self._broker_current_price = broker_cur
        if broker_avg > 0:
            self._broker_avg_price = broker_avg
        self._broker_sync_time = time.time()

        if full:
            old_lots = self._total_lots * (1 if self._dir == LONG else -1)
            old_avg = self._avg_price
            prev_dir = self._dir
            prev_pyr_levels = self._pyramid_levels
            prev_last_pyr = self._last_pyr_entry_price
            prev_last_pyr_add = self._last_pyramid_price
            self._lot_queue.clear()
            self._average_levels = 0
            self._pyr_stop_order_id = None
            self._pyr_stop_price = 0.0
            self._pyr_stop_qty = 0
            if signed_lots == 0:
                self._dir = FLAT
                self._total_lots = 0
                self._avg_price = 0.0
                self._entry_price = 0.0
                self._pyramid_levels = 0
                self._last_pyr_entry_price = 0.0
            else:
                self._dir = LONG if signed_lots > 0 else SHORT
                self._total_lots = abs(signed_lots)
                self._avg_price = broker_avg
                self._entry_price = broker_avg
                self._lot_queue.append(LotEntry(price=broker_avg, side=self._dir, lots=self._total_lots, added_ts=time.monotonic()))
                # F-198: слой после full-resync получает свежий uid; трейл-реестр очищается
                self._trail_stops.clear()
                self._agg_trail = None
                if self._lot_queue:
                    self._lot_queue[-1].uid = self._mk_lot_uid()
                # F-193: ресинк НЕ должен стирать пирамид-состояние той же позиции.
                # F-095 обнулял _pyramid_levels/_last_pyr_entry_price при любом
                # расхождении — после этого позиция с пирамидами оставалась без
                # брокерского стопа (и без pullback-правила) до следующего долива.
                if prev_dir == self._dir and prev_pyr_levels > 0:
                    self._pyramid_levels = prev_pyr_levels
                    self._last_pyr_entry_price = prev_last_pyr
                    self._last_pyramid_price = prev_last_pyr_add
                    log.info(f"RESYNC: pyramid state kept (levels={self._pyramid_levels}, "
                             f"last_pyr={fmt_price(self._last_pyr_entry_price)}) — stop will be re-placed")
                else:
                    self._pyramid_levels = 0
                    self._last_pyr_entry_price = 0.0
            self._peak_lots = max(self._peak_lots, self._total_lots)
            log.info(f"BROKER FULL SYNC: was {old_lots:+d}@{old_avg:.1f} → now {broker_lots:+d}@{broker_avg:.1f}")

    def update_fill_price(self, fill_price: float, action: str, signal_price: float = 0.0):
        """Update entry prices with REAL broker fill. Exit trades recorded in main_of.

        F-0xx fix (19.10): средняя пересчитывается СТРОГО от очереди fill-цен.
        F-093 fix (22.10): при пачке catch-up усреднений fill заменяет слой,
        соответствующий СИГНАЛЬНОЙ цене действия (а не просто последний) —
        иначе вся пачка перезаписывает один и тот же слой очереди.
        """
        if fill_price <= 0:
            return
        if action in ("entry", "average", "pyramid"):
            if action == "entry":
                self._entry_price = fill_price
            replaced = False
            if self._lot_queue:
                # F-093: ищем слой по сигнальной цене (пачка усреднений создаёт слои решётки)
                target = signal_price if signal_price > 0 else self._lot_queue[-1].price
                for i in range(len(self._lot_queue) - 1, -1, -1):
                    if abs(self._lot_queue[i].price - target) < 1e-9:
                        self._lot_queue[i] = LotEntry(price=fill_price, side=self._lot_queue[i].side, lots=self._lot_queue[i].lots, uid=self._lot_queue[i].uid)
                        replaced = True
                        break
                if not replaced:
                    self._lot_queue[-1] = LotEntry(price=fill_price, side=self._lot_queue[-1].side, lots=self._lot_queue[-1].lots, uid=self._lot_queue[-1].uid)
                total_cost = sum(e.price * e.lots for e in self._lot_queue)
                total_lots = sum(e.lots for e in self._lot_queue)
                if total_lots > 0:
                    self._avg_price = total_cost / total_lots
            else:
                self._avg_price = fill_price
            log.info(f"{action.upper()} price updated from broker: {fmt_price(fill_price)} → avg={fmt_price(self._avg_price)} (from fill queue)")

    def _check_entry(self, price: float, now: datetime) -> Optional[dict]:
        """Check for entry signal."""
        if not self.signals.has_enough_data:
            return None

        # Daily stop — close position AND block new entries (наш фикс из main, R-00x)
        if self._daily_stop_hit():
            if self._dir != 0:
                return self._close_all(price, "daily_stop_loss")
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
                    # F-091 RANGE v2 (Дмитрий 20.10): зона-фильтр направления.
                    # У VAH/выше → только SHORT; у VAL/ниже → только LONG.
                    # Внутри VA → оба, если vah_val_block_inside=False; при True — блок.
                    # Сам по себе край вход НЕ даёт: ждём сигнал (CVD Trend/dm_wall/Accel)
                    # и проверяем его направление на соответствие зоне ниже.
                    # Усреднение существующей позиции в этот гейт НЕ входит (вариант А).
                    if price >= vah:
                        entry_zone = "VAH"
                        log.info(f"VAH/VAL RANGE v2: price={fmt_price(price)} >= VAH={fmt_price(vah)} → зона SHORT-only (ждём сигнал)")
                    elif price <= val:
                        entry_zone = "VAL"
                        log.info(f"VAH/VAL RANGE v2: price={fmt_price(price)} <= VAL={fmt_price(val)} → зона LONG-only (ждём сигнал)")
                    else:
                        if self.p.vah_val_block_inside:
                            log.info(f"VAH/VAL RANGE v2: inside VA (VAL={fmt_price(val)}..VAH={fmt_price(vah)}) + block_inside → BLOCK")
                            return None
                        log.info(f"VAH/VAL RANGE v2: inside VA → оба направления разрешены")
                else:
                    # FADE / BREAKOUT mode (original logic): entry at VAH/VAL edges
                    if price >= vah:
                        entry_zone = "VAH"
                        log.info(f"VAH/VAL GATE: price={fmt_price(price)} >= VAH={fmt_price(vah)} (VAL={fmt_price(val)}) → zone=VAH → PASS")
                    elif price <= val:
                        entry_zone = "VAL"
                        log.info(f"VAH/VAL GATE: price={fmt_price(price)} <= VAL={fmt_price(val)} (VAH={fmt_price(val)}) → zone=VAL → PASS")
                    else:
                        log.info(f"VAH/VAL GATE: price={fmt_price(price)} inside VA (VAL={fmt_price(val)}..VAH={fmt_price(vah)}) → BLOCK")
                        return None  # inside Value Area → no entry
                    if self.p.vah_val_mode == "breakout":
                        pass  # Breakout: already past edge — always pass
                    # fade mode: touch of edge is enough — already pass
            else:
                log.info(f"VAH/VAL GATE: VP vah={fmt_price(vah)} val={fmt_price(val)} — zero values → BLOCK")
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

        # F-067b: pending-сигнал CVD Trend (сохранён в on_bar_close, если кроссовер случился
        # вне окна/в позиции). Если обычные сигналы пусты — используем pending как входной сигнал.
        # F-191: сигнал НЕ съедается здесь — он живёт до фактического входа (или TTL).
        # Раньше `_pending_cvd_trend = None` стоял ДО гейтов, и любой отказ фильтра
        # (HL/phase/direction) уничтожал сигнал навсегда: вход 15:15 11.11 был отбит HL
        # по цене, хотя через 2 минуты HL уже разрешал — повторной попытки не было.
        if not sigs and self._pending_cvd_trend:
            from orderflow_engine import OFSignal
            import time as _t
            pd_ = self._pending_cvd_trend
            age = _t.time() - float(pd_.get("ts") or 0)
            ttl = float(getattr(self.p, "pending_signal_ttl_sec", 0) or 0)
            if ttl > 0 and age > ttl:
                log.info(f"PENDING CVD Trend expired: age={age:.0f}s > ttl={ttl:.0f}s → dropped")
                self._pending_cvd_trend = None
            else:
                sigs = [OFSignal(signal_type="cvd_trend", direction=pd_["direction"],
                                 strength=0.9, price=price, timestamp=_t.time())]
                log.info(f"PENDING CVD Trend -> entry signal used: {pd_['direction']} (age={age:.0f}s)")

        if len(sigs) < self.p.signal_confirm_count:
            # F-080b: прозрачность — сигнал(ы) есть, но их меньше confirm-порога
            if sigs:
                types = ", ".join(f"{s.signal_type}({'LONG' if s.direction == 1 else 'SHORT'})" for s in sigs)
                log.info(f"ENTRY SIGNALS below confirm ({len(sigs)}/{self.p.signal_confirm_count}): {types}")
            return None

        # All signals must agree on direction
        directions = set(s.direction for s in sigs)
        if len(directions) > 1:
            # F-080: диагностика — конфликт молча съедал сигналы (инцидент 19.10: CVD Trend LONG
            # в 11:40 не дошёл до входа, вероятный конфликт с dm_wall). Логируем разово на бар.
            types = ", ".join(f"{s.signal_type}({'LONG' if s.direction == 1 else 'SHORT'})" for s in sigs)
            log.info(f"ENTRY CONFLICT: signals disagree → skip | {types}")
            return None  # Conflicting signals, skip

        direction = sigs[0].direction

        # Direction filter — block disallowed side
        if self.p.direction_filter == "long" and direction == SHORT:
            log.info("ENTRY BLOCKED by direction_filter=long (signal=SHORT)")
            return None
        if self.p.direction_filter == "short" and direction == LONG:
            log.info("ENTRY BLOCKED by direction_filter=short (signal=LONG)")
            return None

        # F-091: VAH/VAL RANGE v2 — зона-фильтр направления (у VAH только SHORT, у VAL только LONG,
        # внутри VA оба; при vah_val_block_inside — внутри VA входы запрещены полностью).
        if self.p.use_vah_val and self.vp and self.vp.ready and self.p.vah_val_mode == "range":
            vah, val = self.vp.vah, self.vp.val
            if vah > 0 and val > 0:
                if price >= vah and direction == LONG:
                    log.info(f"ENTRY BLOCKED by VAH/VAL zone: LONG у VAH запрещён (VAH={fmt_price(vah)}) — только SHORT")
                    return None
                if price <= val and direction == SHORT:
                    log.info(f"ENTRY BLOCKED by VAH/VAL zone: SHORT у VAL запрещён (VAL={fmt_price(val)}) — только LONG")
                    return None

        # F-067: Phase Zones — зонный гейт направления (низ LONG / верх SHORT / середина оба)
        if self.p.phase_filter:
            allowed, reason = self._phase_allows(direction, price)
            if not allowed:
                log.info(f"ENTRY BLOCKED by phase_filter: {reason} (dir={direction})")
                return None

        # HL filter (only_strict): LONG только у лоу окна, SHORT только у хая окна.
        # Середина диапазона — блок входа. Выкл (hl_filter=False) → как раньше.
        # F-191: для трендового CVD Trend фильтр отключаем (hl_apply_cvd_trend=False):
        # кроссовер EMA по определению приходит с задержкой 1+ ATR, и «у экстремума»
        # он оказаться не может — HL резал 100% трендовых входов.
        # Импульсные (cvd_accel/agg) и прочие фильтруем как раньше.
        if self.p.hl_filter:
            _has_trend_sig = any(getattr(s, "signal_type", "") == "cvd_trend" for s in sigs)
            _hl_skip = _has_trend_sig and not bool(getattr(self.p, "hl_apply_cvd_trend", False))
            hl = self.signals.get_hl_window(self.p.hl_window)
            if hl is None:
                if _hl_skip:
                    log.info("HL FILTER: window not ready — skipped for cvd_trend (F-191)")
                else:
                    log.info("HL FILTER: window not ready → BLOCK")
                    return None
            else:
                wh, wl = hl
                _would_block = (
                    (direction == LONG and (price - wl) > self.p.hl_delta)
                    or (direction == SHORT and (wh - price) > self.p.hl_delta)
                )
                if _would_block:
                    if _hl_skip:
                        log.info(
                            f"HL FILTER: skipped for cvd_trend (F-191) — "
                            f"уровень был бы отбит: price={fmt_price(price)} hh={fmt_price(wh)} "
                            f"ll={fmt_price(wl)} Δ={fmt_price(self.p.hl_delta)}")
                    else:
                        if direction == LONG:
                            log.info(f"HL FILTER: LONG blocked — не у лоу окна (wl={fmt_price(wl)} price={fmt_price(price)} Δ={fmt_price(self.p.hl_delta)})")
                        else:
                            log.info(f"HL FILTER: SHORT blocked — не у хая окна (wh={fmt_price(wh)} price={fmt_price(price)} Δ={fmt_price(self.p.hl_delta)})")
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
        # F-198: uid слою (ключ реестра трейлинг-стопов)
        self._lot_queue[-1].uid = self._mk_lot_uid()

        self._lock_entry(10.0)  # 10 sec entry lock

        log.info(f"ENTRY {side} {self.p.lots} @ {fmt_price(price)} | signals: {signal_types}")

        # F-191: pending снимается только теперь — когда вход реально разрешён всеми гейтами.
        if self._pending_cvd_trend:
            log.info("PENDING CVD Trend consumed by entry (F-191)")
            self._pending_cvd_trend = None

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
        if not self.p.trail_mode and self._tp_full_hit(price):
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

        # VP RANGE mode: вне VA не усредняемся (по команде Самурая 07.09.2026).
        # Позиция не закрывается — выход только по обычным правилам (partial TP,
        # Min Profit/Lot, Close % All, дневной SL). При возврате цены внутрь VA
        # штатный catch-up доберёт ВСЕ пропущенные уровни по текущей цене.
        # F-091 (20.10, вариант А): при RANGE v2 усреднение НЕ гейтится зоной —
        # Dmitry: «вариант А для усреднения» (усреднение всегда разрешено).
        range_v2 = self.p.vah_val_mode == "range"
        if (not range_v2) and self.is_outside_va(price):
            if not self._avg_va_block_logged:
                log.info(f"AVG VA BLOCK: price={fmt_price(price)} outside VA — averaging paused, missed levels accumulate for catch-up")
                self._avg_va_block_logged = True
            return []
        elif self._avg_va_block_logged:
            log.info("AVG VA BLOCK lifted: price back inside VA — catch-up averaging resumes")
            self._avg_va_block_logged = False

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

        # F-093 fix (22.10): catch-up раскладывает пропущенные уровни ПО РЕШЁТКЕ
        # от лучшей цены (каждый следующий слой на step ниже/выше предыдущего),
        # а не все по текущей цене. Раньше 6-10 слоёв вставали на одну цену →
        # следующий отскок закрывал их пачкой TP по одной цене → «пила TP→AVG
        # по одной цене» (жалоба Дмитрия 22.10).
        for i in range(missed):
            level_price = (price - step * i) if self._dir == LONG else (price + step * i)
            old_lots = self._total_lots
            old_cost = self._avg_price * old_lots

            self._total_lots += self.p.lots
            self._peak_lots = max(self._peak_lots, self._total_lots)
            self._avg_price = (old_cost + level_price * self.p.lots) / self._total_lots
            self._average_levels += 1
            self._last_average_price = level_price
            self._lot_queue.append(LotEntry(price=level_price, side=self._dir, lots=self.p.lots, added_ts=time.monotonic()))
            # F-198: uid слою (ключ реестра трейлинг-стопов)
            self._lot_queue[-1].uid = self._mk_lot_uid()

            actions.append({
                'action': 'average',
                'side': side,
                'qty': self.p.lots,
                'price': level_price,
                'level': self._average_levels,
            })

        if missed > 1:
            log.info(f"AVERAGE CATCHUP {side} {missed}×{self.p.lots} @ {fmt_price(price)} | missed={missed} levels | lvl={self._average_levels}/{self.p.max_average_levels} | avg={fmt_price(self._avg_price)} lots={self._total_lots}")
        else:
            log.info(f"AVERAGE {side} {self.p.lots} @ {fmt_price(price)} | lvl {self._average_levels}/{self.p.max_average_levels} | avg={fmt_price(self._avg_price)} lots={self._total_lots}")

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
            log.info(f"SYNC: removed lot @ {fmt_price(removed.price)} (broker has fewer lots)")
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
        log.info(f"SYNC complete: broker={broker_lots} internal={self._total_lots} avg={fmt_price(self._avg_price)} levels={self._average_levels}")

    def _pos_snapshot(self) -> dict:
        """Compact position snapshot for adjust reports."""
        return {"dir": self._dir, "lots": self._total_lots, "avg": round(self._avg_price, 2)}

    def manual_adjust(self, lots: int, price: float) -> dict:
        """Manual position adjustment (state-only, NO orders sent).

        Human opens/closes position in terminal, robot adopts it.
        lots > 0 = LONG add, lots < 0 = SHORT add, at given price.
        - Same side / FLAT: append LotEntry (VWAP recalc).
        - Opposite side: LIFO-close from queue (returned for PnL record),
          crossing zero -> open new opposite position with remainder.
        All derived state (avg, levels, peak) recomputed from queue.
        """
        new_side = LONG if lots > 0 else SHORT
        adj_qty = abs(lots)
        before = self._pos_snapshot()
        closed = []  # LotEntry copies closed by this adjust (for main to record PnL)

        if self._dir == FLAT or self._dir == new_side:
            # Add lots to position
            self._lot_queue.append(LotEntry(
                price=price, side=new_side, lots=adj_qty,
                added_ts=time.monotonic(),  # 2s grace vs instant partial TP
                uid=self._mk_lot_uid(),
            ))
            if self._dir == FLAT:
                self._dir = new_side
                self._entry_price = price
                self._entry_time = datetime.now(MSK)
            self._signal_type = "manual_adjust"
        else:
            # Opposite side: LIFO-close, possibly cross zero
            remaining = adj_qty
            while remaining > 0 and self._lot_queue:
                last = self._lot_queue[-1]
                if last.lots <= remaining:
                    self._lot_queue.pop()
                    closed.append(LotEntry(price=last.price, side=last.side, lots=last.lots))
                    remaining -= last.lots
                else:
                    # Split entry: close part of it
                    closed.append(LotEntry(price=last.price, side=last.side, lots=remaining))
                    self._lot_queue[-1] = LotEntry(price=last.price, side=last.side,
                                                   lots=last.lots - remaining,
                                                   added_ts=last.added_ts, uid=last.uid)
                    remaining = 0

            if remaining > 0:
                # Crossed zero: open opposite position with remainder
                self._dir = new_side
                self._entry_price = price
                self._entry_time = datetime.now(MSK)
                self._signal_type = "manual_adjust"
                self._lot_queue.clear()
                self._lot_queue.append(LotEntry(
                    price=price, side=new_side, lots=remaining,
                    added_ts=time.monotonic(),
                    uid=self._mk_lot_uid(),
                ))

        # --- Recompute derived state from queue (same invariants as sync_lot_count) ---
        self._total_lots = sum(e.lots for e in self._lot_queue)
        if self._total_lots > 0:
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            self._avg_price = total_cost / self._total_lots
            if self._dir == SHORT:
                self._last_average_price = max(e.price for e in self._lot_queue)
            else:
                self._last_average_price = min(e.price for e in self._lot_queue)
            self._average_levels = max(0, self._total_lots - 1)
            self._pyramid_levels = min(self._pyramid_levels, self._total_lots - 1) if self._total_lots > 1 else 0
            self._peak_lots = max(self._peak_lots, self._total_lots)
            # F-055 fix: after FLAT->position adjust, _last_pyramid_price stayed at
            # _reset_position() 0.0 -> distance = price - 0 = huge -> instant bogus PYRAMID
            # on the very next tick (14:14:29 buy @100.25 right after 13:55 ADJUST +4).
            # Initialize from current price — first pyramid then requires a real step move.
            if before["dir"] == 0 or self._last_pyramid_price <= 0:
                self._last_pyramid_price = price
        else:
            # Fully closed by hand
            self._round_trips += 1
            self._reset_position()

        after = self._pos_snapshot()
        log.info(f"ADJUST: {before} -> {after} "
                 f"(add {lots:+d} @ {fmt_price(price)}, closed {sum(c.lots for c in closed)} lots)")
        return {"closed": [{"price": c.price, "side": c.side, "lots": c.lots} for c in closed],
                "before": before, "after": after}

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
        self._last_pyr_entry_price = price  # F-073: для pullback-закрытия — только пирамиды обновляют
        self._lot_queue.append(LotEntry(price=price, side=self._dir, lots=self.p.lots, added_ts=time.monotonic()))
        # F-198: uid слою (ключ реестра трейлинг-стопов)
        self._lot_queue[-1].uid = self._mk_lot_uid()

        log.info(f"PYRAMID {side} {self.p.lots} @ {fmt_price(price)} | lvl {self._pyramid_levels}/{self.p.max_pyramid_levels} | avg={fmt_price(self._avg_price)} lots={self._total_lots}")

        return {
            'action': 'pyramid',
            'side': side,
            'qty': self.p.lots,
            'price': price,
            'level': self._pyramid_levels,
        }

    # ---------- F-198: догон пропущенных partial TP ----------

    def _fetch_recent_bars(self, since_ts: float):
        """M1-бары с C#-сервера за сегодня; возвращает [(ts, high, low)] или []."""
        import json as _json
        import urllib.request as _uq
        ticker = str(getattr(self, "symbol", "") or "").split("@")[0]
        if not ticker:
            return []
        url = f"http://localhost:5050/api/candles?ticker={ticker}&tf=1&days=1"
        with _uq.urlopen(url, timeout=6) as r:
            data = _json.loads(r.read())
        if not isinstance(data, list):
            return []
        return [(float(b["t"]), float(b["h"]), float(b["l"])) for b in data if float(b.get("t", 0)) >= since_ts]

    def _next_tp_level(self):
        """F-199: уровень partial TP для LIFO-лота (лот ± spread)."""
        if not self._lot_queue:
            return None
        last = self._lot_queue[-1]
        return round(last.price + self.p.spread * last.side, 4)

    def _next_avg_level(self):
        """F-199: цена следующего усреднения (крайний лот ± step_average)."""
        if not self._lot_queue or self._dir == FLAT:
            return None
        step = self._effective_step(average=True)
        if self._dir == LONG:
            return round(min(e.price for e in self._lot_queue) - step, 4)
        return round(max(e.price for e in self._lot_queue) + step, 4)

    def _scan_missed_tp(self, price: float) -> list[dict]:
        """F-199: раз в tp_bar_scan_sec проверить по M1-барам, не пробил ли рынок уровень
        тейка LIFO-лота, пока робот этот тик не получил. Закрывает лот, только если
        профит по нему ЕЩЁ есть (pnl >= min_profit_per_lot)."""
        if not getattr(self.p, "tp_bar_scan_enabled", True):
            return []
        if not self.p.partial_tp or not self._lot_queue or self._dir == FLAT:
            return []
        now = time.time()
        period = float(getattr(self.p, "tp_bar_scan_sec", 60) or 60)
        if now - self._tp_scan_ts < period:
            return []
        self._tp_scan_ts = now

        last = self._lot_queue[-1]
        if last.added_ts > 0 and (time.monotonic() - last.added_ts) < 2.0:
            return []

        level = last.price + self.p.spread * last.side
        window = float(getattr(self.p, "tp_bar_scan_window_sec", 240) or 240)
        since = now - window

        try:
            bars = self._fetch_recent_bars(since)
        except Exception as e:
            log.warning(f"TP BAR SCAN: bars fetch failed: {str(e)[:120]}")
            return []
        if not bars:
            return []

        if self._dir == LONG:
            passed = any(h >= level for _ts, h, _l in bars)
        else:
            passed = any(l <= level for _ts, _h, l in bars)
        if not passed:
            return []

        pnl_pts = (price - last.price) * last.side
        min_profit = float(getattr(self.p, "min_profit_per_lot", 0) or 0)
        if pnl_pts < min_profit:
            log.info(f"TP BAR SCAN: level {fmt_price(level)} was touched, but price back "
                     f"(pnl={pnl_pts:.0f} < min_profit={min_profit:.0f}) — keep lot")
            return []

        side = "sell" if self._dir == LONG else "buy"
        self._total_lots -= last.lots
        self._lot_queue.pop()

        action = {
            "action": "partial_tp",
            "side": side,
            "qty": last.lots,
            "price": price,
            "entryPrice": last.price,
            "entrySide": last.side,
            "signal": self._signal_type,
            "reason": "tp_bar_scan",
        }

        if self._lot_queue:
            if self._dir == SHORT:
                self._last_average_price = max(e.price for e in self._lot_queue)
            else:
                self._last_average_price = min(e.price for e in self._lot_queue)
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            total_lots = sum(e.lots for e in self._lot_queue)
            self._avg_price = total_cost / total_lots if total_lots > 0 else 0
        else:
            self._last_average_price = self._avg_price
            self._reset_position()
            self._lock_entry(10.0)
            self._round_trips += 1

        log.info(f"TP BAR SCAN: level {fmt_price(level)} (lot {fmt_price(last.price)}) touched within "
                 f"{window:.0f}s — closing {last.lots} @ {fmt_price(price)} (pnl={pnl_pts:.0f})")
        return [action]

    def _catchup_missed_tp(self, price: float) -> list[dict]:
        """F-198: закрыть LIFO-лот, если его тейк был пройден в «слепое окно».

        Условия (все обязательны):
          1) было окно без данных длиннее catchup_tp_min_gap_sec;
          2) в этом окне high (LONG) / low (SHORT) дошёл до уровня лота ± spread;
          3) профит по лоту ЕЩЁ есть (pnl >= min_profit_per_lot) — не закрываем в минус;
          4) лот не в 2-секундном grace после добавления.
        После проверки окно сбрасывается (одноразово).
        """
        if not getattr(self.p, "catchup_tp_enabled", True):
            self._blind_since = None
            return []
        blind_since = self._blind_since
        self._blind_since = None
        if blind_since is None or not self.p.partial_tp or not self._lot_queue or self._dir == FLAT:
            return []

        gap = time.time() - blind_since
        gap_limit = float(getattr(self.p, "catchup_tp_min_gap_sec", 30) or 30)
        if gap < gap_limit:
            return []

        last = self._lot_queue[-1]
        if last.added_ts > 0 and (time.monotonic() - last.added_ts) < 2.0:
            return []

        level = last.price + self.p.spread * last.side   # LONG: +spread, SHORT: −spread

        try:
            bars = self._fetch_recent_bars(blind_since)
        except Exception as e:
            log.warning(f"CATCHUP TP: bars fetch failed: {str(e)[:120]}")
            return []
        if not bars:
            return []

        if self._dir == LONG:
            passed = any(h >= level for _ts, h, _l in bars)
        else:
            passed = any(l <= level for _ts, _h, l in bars)
        if not passed:
            log.info(f"CATCHUP TP: blind {gap:.0f}s, level {fmt_price(level)} NOT reached — nothing to do")
            return []

        pnl_pts = (price - last.price) * last.side
        min_profit = float(getattr(self.p, "min_profit_per_lot", 0) or 0)
        if pnl_pts < min_profit:
            log.info(f"CATCHUP TP skipped: level {fmt_price(level)} passed during blind {gap:.0f}s, "
                     f"but price already back (pnl={pnl_pts:.0f} < min_profit={min_profit:.0f})")
            return []

        side = "sell" if self._dir == LONG else "buy"
        self._total_lots -= last.lots
        self._lot_queue.pop()

        action = {
            "action": "partial_tp",
            "side": side,
            "qty": last.lots,
            "price": price,
            "entryPrice": last.price,
            "entrySide": last.side,
            "signal": self._signal_type,
            "reason": "catchup_tp",
        }

        if self._lot_queue:
            if self._dir == SHORT:
                self._last_average_price = max(e.price for e in self._lot_queue)
            else:
                self._last_average_price = min(e.price for e in self._lot_queue)
            total_cost = sum(e.price * e.lots for e in self._lot_queue)
            total_lots = sum(e.lots for e in self._lot_queue)
            self._avg_price = total_cost / total_lots if total_lots > 0 else 0
        else:
            self._last_average_price = self._avg_price
            self._reset_position()
            self._lock_entry(10.0)
            self._round_trips += 1

        log.info(f"CATCHUP TP: blind {gap:.0f}s, lot {fmt_price(last.price)} level {fmt_price(level)} was passed "
                 f"— closing {last.lots} @ {fmt_price(price)} (pnl={pnl_pts:.0f})")
        return [action]

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
        # F-198: в режиме Trailing SL прибыль фиксируется стопом, а не маркетом
        if self.p.trail_mode:
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
                    log.info(f"CLOSE_HALF_PCT({pct}%) {side} {qty} @ {fmt_price(price)} | peak={self._peak_lots} threshold={threshold}")
                    return [self._close_all(price, f"close_half_{pct}pct")]
                else:
                    return []

        # ЗАКОН Дмитрия (20.10): TP строго ПО ОДНОМУ ЛОТУ за тик. После каждого
        # закрытия — пересчёт на следующем тике (грейс 2с + цена пересчитается).
        # Пачечное закрытие всех прибыльных слоёв за один тик запрещено.
        if not self._lot_queue:
            return []

        last = self._lot_queue[-1]

        # Grace period: don't TP a lot that was just added (2 sec)
        if last.added_ts > 0 and (time.monotonic() - last.added_ts) < 2.0:
            return []

        pnl_pts = (price - last.price) * last.side
        if pnl_pts < self.p.spread:
            return []

        # Close this lot — SELL if LONG, BUY if SHORT
        side = "sell" if self._dir == LONG else "buy"

        # Trade and PnL recorded in main_of.py after real broker fill
        self._total_lots -= last.lots
        self._lot_queue.pop()  # LIFO — remove from end

        actions = [{
            'action': 'partial_tp',
            'side': side,
            'qty': last.lots,
            'price': price,
            'entryPrice': last.price,
            'entrySide': last.side,
            'signal': self._signal_type,
        }]

        # Update state after close (recompute avg from remaining queue)
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

        a = actions[0]
        log.info(f"PARTIAL_TP {a['side']} {a['qty']} @ {fmt_price(price)} | remaining={self._total_lots}")

        return actions

    def _close_all(self, price: float, reason: str) -> dict:
        """Close entire position. Trade and PnL recorded in main_of.py after real broker fill."""
        side = "sell" if self._dir == LONG else "buy"
        qty = self._total_lots
        self._round_trips += 1

        log.info(f"CLOSE_ALL {side} {qty} @ {fmt_price(price)} | reason={reason}")

        # Reset happens in main_of._execute_action AFTER order fill
        self._lock_entry(10.0)

        return {
            'action': 'close_all',
            'side': side,
            'qty': qty,
            'price': price,
            'reason': reason,
            # F-062: снимок слоёв LIFO-очереди ДО сброса — журнал считает PnL по реальным ценам входа
            'layers': [[e.price, e.lots] for e in self._lot_queue],
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
        self._last_pyr_entry_price = 0.0  # F-073
        self._pyr_stop_order_id = None  # F-094: брокерский стоп снят/пересчитан при закрытии
        self._pyr_stop_price = 0.0
        self._pyr_stop_qty = 0
        self._lot_queue.clear()
        self._entry_time = None
        self._signal_type = ""
        self._broker_avg_price = 0.0
        # F-198: сбрасываем реестр трейлинг-стопов (заявки снимает main_of при close_all)
        self._trail_stops.clear()
        self._agg_trail = None
        self._trail_closed_lots = 0

    def _effective_step(self, average: bool = True) -> float:
        """Get effective step (with optional ATR adaptation).

        F-055 fix: BR steps are fractional (0.35-0.40$); the old int() math
        (int(base * atr/50)) collapsed them to 0 -> ZeroDivisionError in
        _check_averaging. Keep float math and clamp to a 1-tick minimum (0.01).
        SI behavior unchanged for integer steps (ATR ~ step scale)."""
        base = self.p.step_average if average else self.p.step_pyramid
        if not self.p.step_atr:
            return base
        # ATR-adaptive: step = base × (ATR / 50), clamped to [base/2, base×3]
        atr = self.signals._get_atr()
        if not atr or atr <= 0:
            return base
        adapted = base * (atr / 50.0)
        clamped = max(base / 2.0, min(adapted, base * 3.0))
        # Never below one price tick (BR 0.01; SI integer steps unaffected)
        return max(clamped, 0.01)

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
            # F-193: без этого после рестарта терялся уровень стопа пирамиды
            "lastPyrEntryPrice": self._last_pyr_entry_price,
            "peakLots": self._peak_lots,
            "lotQueue": [{"price": e.price, "side": e.side, "lots": e.lots} for e in self._lot_queue],
            "roundTrips": self._round_trips,
            "realizedPnL": self._realized_pnl,
            "dailyPnL": self._daily_pnl,
            "dailyPnLDate": self._daily_pnl_date,
            "entryTime": self._entry_time.isoformat() if self._entry_time else "",
            "signalType": self._signal_type,
            "tradeHistory": self._trade_history[-200:],  # Last 200 trades
            "trail": self._trail_registry_payload(),   # F-198
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
        # F-193: восстанавливаем цену последнего пирамид-входа — иначе после
        # рестарта позиция с пирамидами остаётся без стопа на уровень
        self._last_pyr_entry_price = state.get("lastPyrEntryPrice", 0.0)
        if (self._last_pyr_entry_price <= 0 and self._pyramid_levels > 0
                and self._last_pyramid_price > 0):
            # миграция старых state: лучшая оценка — цена последнего пирамид-долива
            self._last_pyr_entry_price = self._last_pyramid_price
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
        self._trail_registry_load(state.get("trail") or {})   # F-198
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
                    uid=int(e.get("uid", 0) or 0),
                ))
            log.info(f"Restored position: dir={self._dir} lots={self._total_lots} avg={fmt_price(self._avg_price)} lotEntries={len(self._lot_queue)}")
        elif self._total_lots > 0 and self._avg_price > 0:
            # Fallback for old state files without lotQueue
            self._lot_queue.append(LotEntry(
                price=self._avg_price,
                side=self._dir if self._dir != 0 else LONG,
                lots=self._total_lots,
                added_ts=0.0,  # No grace for restored lots
                uid=self._mk_lot_uid(),
            ))
            log.warning(f"Restored position without lotQueue — single entry: dir={self._dir} lots={self._total_lots}")

    # ---------- F-067: Phase Zones (Donchian thirds) ----------

    def _phase_channel_refresh(self, price: float):
        """Раз в 60с синхронизирует фазовый канал с /api/candles C#-сервера —
        ЕДИНЫЙ ИСТОЧНИК с графиком UI (F-067, 16.10: «робот пишет лонг, график — другое»). 
        F-087 fix (19.10): канал хранится в ОТДЕЛЬНЫХ полях _phase_chan_high/low,
        а НЕ в общем deque-буфере — живые бары вытесняли 777-барную историю
        (maxlen 2000 + get(777) от хвоста) → роботский канал сужался до последних
        ~130 баров (84384) и расходился с графиком UI (85490). При недоступности
        сервера — тихо пользуемся последним известным каналом."""
        import time as _t
        now = _t.time()
        if now - getattr(self, "_phase_chan_ts", 0) < 60:
            return
        self._phase_chan_ts = now
        try:
            import json as _json
            import urllib.request as _uq
            symbol = getattr(self.p, "symbol", None) or getattr(self, "symbol", "")
            ticker = str(symbol).split("@")[0]
            url = (f"http://localhost:5050/api/candles?ticker={ticker}"
                   f"&tf=5&days=30")  # F-087: фиксированно M5 — как график UI по умолчанию (не зависит от рабочего TF робота)
            with _uq.urlopen(url, timeout=5) as r:
                data = _json.loads(r.read())
            if not isinstance(data, list) or len(data) < 30:
                return
            win = max(10, int(self.p.phase_window))
            seg = data[-win:]
            wh = max(b["h"] for b in seg)
            wl = min(b["l"] for b in seg)
            self._phase_chan_high = float(wh)
            self._phase_chan_low = float(wl)
            log.info(f"PHASE CHAN refresh: {len(data)} bars, win={win} → {fmt_price(wl)}..{fmt_price(wh)}")
        except Exception as _e:
            log.warning(f"PHASE CHAN refresh FAILED: {_e}")  # держим последний известный канал



    def _phase_eval(self, price: float) -> dict:
        """Оценить фазу рынка по положению цены в Donchian-канале (окно phase_window).

        Вход в зону: price <= lo + range*lower → LONG-зона;
                     price >= lo + range*upper → SHORT-зона;
                     иначе BOTH.
        Выход из зоны с гистерезисом (phase_hysteresis доля канала) — против дребезга.
        Возвращает слепок для гейта и /status.
        """
        self._phase_channel_refresh(price)         # F-067: единый источник с графиком (60с кэш)
        win = max(10, int(self.p.phase_window))
        # F-087 fix: канал из полей refresh (единый источник с UI), не из общего deque-буфера,
        # который живые бары вытесняли → канал робота сужался и расходился с графиком.
        ch_high = getattr(self, "_phase_chan_high", 0.0)
        ch_low = getattr(self, "_phase_chan_low", 0.0)
        if ch_high > 0 and ch_low > 0:
            hl = (ch_high, ch_low)
        else:
            hl = self.signals.get_phase_channel(win)   # fallback: внутренний буфер
        if hl is None:
            return {"enabled": bool(self.p.phase_filter), "ready": False,
                    "window": win, "dir": 0}
        wh, wl = hl
        rng = wh - wl
        if rng <= 0:
            return {"enabled": bool(self.p.phase_filter), "ready": False,
                    "window": win, "dir": 0}
        lo_bound = wl + rng * self.p.phase_lower
        hi_bound = wl + rng * self.p.phase_upper
        hyst = rng * self.p.phase_hysteresis

        prev = self._phase_dir
        if price <= lo_bound:
            self._phase_dir = 1                       # LONG-зона
        elif price >= hi_bound:
            self._phase_dir = -1                      # SHORT-зона
        elif prev == 1 and price <= lo_bound + hyst:
            self._phase_dir = 1                       # гистерезис: остаёмся в LONG-зоне
        elif prev == -1 and price >= hi_bound - hyst:
            self._phase_dir = -1                      # гистерезис: остаёмся в SHORT-зоне
        else:
            self._phase_dir = 0                       # BOTH

        if prev != self._phase_dir:
            names = {1: "LONG", -1: "SHORT", 0: "BOTH"}
            log.info(f"PHASE ZONE: {names.get(prev, '?')} → {names.get(self._phase_dir, '?')} | "
                     f"price={fmt_price(price)} канал {fmt_price(wl)}..{fmt_price(wh)} "
                     f"границы {fmt_price(lo_bound)}/{fmt_price(hi_bound)}")
        return {"enabled": bool(self.p.phase_filter), "ready": True, "window": win,
                "chanHigh": round(wh, 2), "chanLow": round(wl, 2),
                "boundLo": round(lo_bound, 2), "boundHi": round(hi_bound, 2),
                "pos": round((price - wl) / rng, 3) if rng > 0 else None,
                "dir": self._phase_dir}

    def _phase_allows(self, direction: int, price: float) -> tuple[bool, str]:
        """Гейт входа по фазе. Возвращает (allowed, reason)."""
        if not self.p.phase_filter:
            return True, "off"
        st = self._phase_eval(price)
        if not st.get("ready"):
            return True, "not_ready"                  # канал не готов — не мешаем
        d = st.get("dir", 0)
        if d == 0:
            return True, "both"
        if d == 1 and direction == LONG:
            return True, "long_zone"
        if d == -1 and direction == SHORT:
            return True, "short_zone"
        names = {1: "LONG-зона", -1: "SHORT-зона", 0: "BOTH"}
        return False, f"фаза {names.get(d)} (канал {st.get('chanLow')}..{st.get('chanHigh')})"

    def _phase_state(self) -> dict:
        """Слепок для /status (UI-вердикт)."""
        if not self.p.phase_filter:
            return {"enabled": False}
        return self._phase_eval(self._current_price)

    # ---------- Status ----------

    def _hl_state(self) -> dict:
        """HL filter state for /status: window high/low, near flags, verdict."""
        if not self.p.hl_filter:
            return {"enabled": False}
        hl = self.signals.get_hl_window(self.p.hl_window)
        if hl is None:
            return {"enabled": True, "ready": False, "window": self.p.hl_window, "delta": self.p.hl_delta}
        wh, wl = hl
        p = self._current_price
        near_high = (wh - p) <= self.p.hl_delta
        near_low = (p - wl) <= self.p.hl_delta
        if near_high and near_low:
            verdict = "both"  # окно у́же Δ — LONG и SHORT оба в зоне допуска (фильтр нейтрален)
        elif near_high:
            verdict = "short_only"
        elif near_low:
            verdict = "long_only"
        else:
            verdict = "blocked"
        return {"enabled": True, "ready": True, "window": self.p.hl_window, "delta": self.p.hl_delta,
                "winHigh": round(wh, 2), "winLow": round(wl, 2),
                "nearHigh": near_high, "nearLow": near_low, "verdict": verdict}

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
            "nextTpLevel": self._next_tp_level(),
            "nextAvgLevel": self._next_avg_level(),
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
            "hl": self._hl_state(),
            "phase": self._phase_state(),
            "dmWallAgree": self._last_dm_wall,
        }

