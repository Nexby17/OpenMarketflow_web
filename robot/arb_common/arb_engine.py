"""Arbitrage Engine — basis calculation, Z-score, L2 orderbook tracking."""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np

log = logging.getLogger("arb_engine")


@dataclass
class L2Quote:
    """Top-of-book snapshot."""
    bid: float = 0.0
    ask: float = 0.0
    bid_vol: int = 0
    ask_vol: int = 0
    ts: float = 0.0


class OrderBookTracker:
    """Tracks L2 orderbook for a single instrument — best bid/ask + depth."""

    def __init__(self, depth_levels: int = 10):
        self._lock = threading.Lock()
        self._bid_prices: list[float] = []
        self._bid_volumes: list[int] = []
        self._ask_prices: list[float] = []
        self._ask_volumes: list[int] = []
        self._depth = depth_levels
        self._ts: float = 0.0

    def update(self, rows: list[tuple]):
        """Update from FinamPy OrderBook rows.
        rows: [(price, buy_size, sell_size, action), ...]
        action: 1=insert, 2=update, 3=delete
        """
        with self._lock:
            bids = {}  # price → volume
            asks = {}
            for price, buy_vol, sell_vol, action in rows:
                if price <= 0:
                    continue
                # buy_size = bid side, sell_size = ask side
                if buy_vol > 0:
                    bids[price] = bids.get(price, 0) + buy_vol
                if sell_vol > 0:
                    asks[price] = asks.get(price, 0) + sell_vol
                if action == 3:  # delete
                    bids.pop(price, None)
                    asks.pop(price, None)

            self._bid_prices = sorted(bids.keys(), reverse=True)[:self._depth]
            self._bid_volumes = [bids[p] for p in self._bid_prices]
            self._ask_prices = sorted(asks.keys())[:self._depth]
            self._ask_volumes = [asks[p] for p in self._ask_prices]
            self._ts = time.time()

    @property
    def best_bid(self) -> float:
        with self._lock:
            return self._bid_prices[0] if self._bid_prices else 0.0

    @property
    def best_ask(self) -> float:
        with self._lock:
            return self._ask_prices[0] if self._ask_prices else 0.0

    @property
    def mid(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        if bb <= 0 or ba <= 0:
            return 0.0
        return (bb + ba) / 2.0

    @property
    def bid_vol(self) -> int:
        with self._lock:
            return self._bid_volumes[0] if self._bid_volumes else 0

    @property
    def ask_vol(self) -> int:
        with self._lock:
            return self._ask_volumes[0] if self._ask_volumes else 0

    @property
    def total_bid_vol(self) -> int:
        with self._lock:
            return sum(self._bid_volumes)

    @property
    def total_ask_vol(self) -> int:
        with self._lock:
            return sum(self._ask_volumes)

    @property
    def total_volume(self) -> int:
        return self.total_bid_vol + self.total_ask_vol

    @property
    def has_data(self) -> bool:
        with self._lock:
            return len(self._bid_prices) > 0 and len(self._ask_prices) > 0

    @property
    def ts(self) -> float:
        return self._ts

    def get_quote(self) -> L2Quote:
        return L2Quote(
            bid=self.best_bid, ask=self.best_ask,
            bid_vol=self.bid_vol, ask_vol=self.ask_vol,
            ts=self._ts,
        )


class BasisCalculator:
    """Calculates basis and Z-score from fair value (cost of carry).

    Z-score measures deviation of actual spread from theoretical fair spread,
    normalised by rolling std of deviations. Fair spread = spot × contract_size × rate × T/365.
    """

    def __init__(self, lookback: int = 50, hedge_ratio: float = 10.0,
                 risk_free_rate: float = 0.16, expiration_date: str = "",
                 contract_size: int = 100):
        self._lock = threading.Lock()
        self._lookback = lookback
        self._hedge_ratio = hedge_ratio
        self._rate = risk_free_rate
        self._expiration_date = expiration_date
        self._contract_size = contract_size
        self._basis_history: deque[float] = deque(maxlen=lookback * 5)
        self._deviation_history: deque[float] = deque(maxlen=lookback * 5)
        self._spread_history: deque[float] = deque(maxlen=lookback * 5)
        self._price_a: float = 0.0
        self._price_b: float = 0.0
        self._bid_a: float = 0.0
        self._ask_a: float = 0.0
        self._bid_b: float = 0.0
        self._ask_b: float = 0.0
        self._ts: float = 0.0
        # === Fair-value deviation mode (Fix #1/#2, 2026-09-10) ===
        # Deviation history is pushed at a slow cadence (minutes); lookback counts pushes.
        self._dev_lookback: int = 2500         # pushes (~5 trading days @ 60s push)
        self._dev_push_interval: float = 60.0  # seconds between deviation pushes
        self._last_dev_push: float = 0.0
        # Dividend calendar: [("YYYY-MM-DD", rub_per_share), ...] ex-dividend dates
        self._dividends: list[tuple[str, float]] = []

    @property
    def lookback(self) -> int:
        return self._lookback

    @lookback.setter
    def lookback(self, val: int):
        with self._lock:
            self._lookback = max(10, int(val))

    @property
    def hedge_ratio(self) -> float:
        return self._hedge_ratio

    @hedge_ratio.setter
    def hedge_ratio(self, val: float):
        with self._lock:
            self._hedge_ratio = val

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, val: float):
        with self._lock:
            self._rate = val

    @property
    def expiration_date(self) -> str:
        return self._expiration_date

    @expiration_date.setter
    def expiration_date(self, val: str):
        with self._lock:
            self._expiration_date = val

    @property
    def contract_size(self) -> int:
        return self._contract_size

    @contract_size.setter
    def contract_size(self, val: int):
        with self._lock:
            self._contract_size = int(val)

    @property
    def dev_lookback(self) -> int:
        return self._dev_lookback

    @dev_lookback.setter
    def dev_lookback(self, val: int):
        with self._lock:
            self._dev_lookback = max(10, int(val))
            self._deviation_history = deque(self._deviation_history, maxlen=self._dev_lookback * 5)

    @property
    def dev_push_interval(self) -> float:
        return self._dev_push_interval

    @dev_push_interval.setter
    def dev_push_interval(self, val: float):
        with self._lock:
            self._dev_push_interval = max(1.0, float(val))

    def set_dividends(self, dividends) -> None:
        """Set dividend calendar for fair value (Fix #2).
        dividends: list of {"date": "YYYY-MM-DD", "amount": rub_per_share}
        or list of (date_str, amount) tuples. Ex-dividend dates on or before
        expiration reduce the theoretical carry premium."""
        parsed: list[tuple[str, float]] = []
        for d in dividends or []:
            if isinstance(d, dict):
                date_s = str(d.get("date", "")).strip()
                amount = float(d.get("amount", 0))
            else:
                date_s, amount = str(d[0]), float(d[1])
            if date_s and amount:
                parsed.append((date_s, amount))
        parsed.sort(key=lambda x: x[0])
        with self._lock:
            self._dividends = parsed

    @property
    def dividends(self) -> list[tuple[str, float]]:
        return list(self._dividends)

    def _dividends_before_expiration(self) -> float:
        """Sum of rub-per-share dividends with ex-date <= expiration."""
        if not self._dividends or not self._expiration_date:
            return 0.0
        try:
            exp = datetime.strptime(self._expiration_date, "%Y-%m-%d")
        except Exception:
            return 0.0
        total = 0.0
        for date_s, amount in self._dividends:
            try:
                ex = datetime.strptime(date_s, "%Y-%m-%d")
            except Exception:
                continue
            if ex <= exp:
                total += amount
        return total

    def update_price_a(self, price: float):
        with self._lock:
            if price > 0:
                self._price_a = price
                self._ts = time.time()

    def update_price_b(self, price: float):
        with self._lock:
            if price > 0:
                self._price_b = price
                self._ts = time.time()

    def update_market(self, bid_a: float = 0, ask_a: float = 0,
                      bid_b: float = 0, ask_b: float = 0):
        """Update best bid/ask for market spread calculation."""
        with self._lock:
            if bid_a > 0: self._bid_a = bid_a
            if ask_a > 0: self._ask_a = ask_a
            if bid_b > 0: self._bid_b = bid_b
            if ask_b > 0: self._ask_b = ask_b
            self._ts = time.time()

    @property
    def price_a(self) -> float:
        return self._price_a

    @property
    def price_b(self) -> float:
        return self._price_b

    @property
    def bid_a(self) -> float:
        return self._bid_a

    @property
    def ask_a(self) -> float:
        return self._ask_a

    @property
    def bid_b(self) -> float:
        return self._bid_b

    @property
    def ask_b(self) -> float:
        return self._ask_b

    @property
    def basis(self) -> float:
        """basis = price_b - price_a × hedge_ratio."""
        if self._price_a <= 0 or self._price_b <= 0:
            return 0.0
        return self._price_b - self._price_a * self._hedge_ratio

    @property
    def spread_rub(self) -> float:
        """Realizable SHORT spread: bid_B - ask_A × hedge_ratio.
        Falls back to LAST price mid if no OB data."""
        if self._bid_b > 0 and self._ask_a > 0:
            return self._bid_b - self._ask_a * self._hedge_ratio
        if self._price_a <= 0 or self._price_b <= 0:
            return 0.0
        return self._price_b - self._price_a * self._hedge_ratio

    @property
    def spread_long_rub(self) -> float:
        """Realizable LONG-basis spread (Fix #3): ask_B - bid_A × hedge_ratio.
        Long basis entry = sell stock A @ bid, buy futures B @ ask.
        Falls back to LAST prices if no OB data."""
        if self._ask_b > 0 and self._bid_a > 0:
            return self._ask_b - self._bid_a * self._hedge_ratio
        if self._price_a <= 0 or self._price_b <= 0:
            return 0.0
        return self._price_b - self._price_a * self._hedge_ratio

    @property
    def spread_pct(self) -> float:
        """Spread as % of spot value."""
        spot_value = self._price_a * self._contract_size
        if spot_value <= 0:
            return 0.0
        return (self.spread_rub / spot_value) * 100

    # === Fair value (cost of carry) ===

    def _days_to_expiration(self) -> int:
        """Calendar days to futures expiration."""
        if not self._expiration_date:
            return 0
        try:
            from datetime import datetime
            exp = datetime.strptime(self._expiration_date, "%Y-%m-%d")
            today = datetime.now()
            delta = (exp - today).days
            return max(0, delta)
        except Exception:
            return 0

    @property
    def fair_spread(self) -> float:
        """Theoretical fair premium in ₽ per contract (cost of carry, Fix #2):
        fair = S × CS × (r × T/365) − CS × Σ dividends (ex-date ≤ expiration).
        Futures holder does not receive dividends, so they reduce fair premium.
        Can be negative near expiration with big upcoming dividends."""
        if self._price_a <= 0:
            return 0.0
        days = self._days_to_expiration()
        carry = self._price_a * self._contract_size * self._rate * days / 365.0
        div_part = self._dividends_before_expiration() * self._contract_size
        return carry - div_part

    @property
    def deviation(self) -> float:
        """Deviation from fair value = actual spread - fair spread.
        Positive = futures expensive, negative = futures cheap."""
        return self.spread_rub - self.fair_spread

    def _push_deviation(self, force: bool = False):
        """Push current deviation from fair value to history (Fix #1).

        Called at a SLOW cadence (default 60s, dev_push_interval) so the
        deviation mean/std span days, not seconds. The 1-second spread push
        in main_arb must NOT feed these stats — that made Z useless.
        """
        now = time.time()
        if not force and (now - self._last_dev_push) < self._dev_push_interval:
            return
        self._last_dev_push = now
        d = self.deviation
        if d != 0.0:
            self._deviation_history.append(d)

    def warmup_dev_history(self, spot_bars: list, fut_bars: list) -> int:
        """F-025: pre-fill deviation history from historical H1 bars so the
        robot can trade right after restart (instead of 50 minutes of
        collection). spot/fut_bars: [{close: float, time: iso}, ...] aligned
        by timestamp (intersects). Deviation per bar uses the same fair calc
        with days_to_expiration computed at that bar time."""
        from datetime import datetime
        import math

        def _bar_close(b):
            """Finam REST bar: close = {'value': '95.08'} or raw number."""
            c = b.get("close")
            if isinstance(c, dict):
                c = c.get("value")
            try:
                return float(c)
            except (TypeError, ValueError):
                return 0.0

        def _bar_min(b):
            """bar timestamp -> minute string normalized to 'YYYY-MM-DD HH:MM'."""
            t = b.get("timestamp") or b.get("time") or b.get("begin") or ""
            t = str(t).replace("T", " ")[:16]
            return t

        if not spot_bars or not fut_bars:
            return 0
        fmap = {}
        for b in fut_bars:
            t = _bar_min(b)
            c = _bar_close(b)
            if t and c:
                fmap[t] = c
        pushes = 0
        for b in spot_bars:
            t = _bar_min(b)
            pa = _bar_close(b)
            if not t or not pa or t not in fmap:
                continue
            pb = fmap[t]
            if pa <= 0 or pb <= 0:
                continue
            spread = pb - pa * self._hedge_ratio
            # fair at that moment: T from bar time to expiration
            try:
                bt = datetime.strptime(t, "%Y-%m-%d %H:%M")
                exp = datetime.strptime(self._expiration_date, "%Y-%m-%d")
                days = max((exp - bt).days, 0)
            except Exception:
                days = 0
            carry = pa * self._contract_size * self._rate * days / 365.0
            div_part = self._dividends_before_expiration() * self._contract_size
            fair = carry - div_part
            dev = spread - fair
            if dev != 0.0 and not math.isnan(dev):
                self._deviation_history.append(dev)
                pushes += 1
        # simulate: interval respected (backfill counts as history)
        if pushes:
            self._last_dev_push = __import__("time").time()
        return pushes

    def _deviation_stats(self) -> tuple[float, float]:
        """Returns (mean, std) of deviation from fair value over dev_lookback pushes."""
        if len(self._deviation_history) < self._dev_lookback:
            arr = np.array(self._deviation_history)
        else:
            arr = np.array(list(self._deviation_history)[-self._dev_lookback:])
        if len(arr) < 2:
            return 0.0, 0.0
        return float(np.mean(arr)), float(np.std(arr))

    def _deviation_extremes(self) -> dict:
        """Returns spread_max, spread_min, avg_max, avg_min over lookback.
        Used as reference for Variant 1 (absolute thresholds)."""
        if len(self._spread_history) < self._lookback:
            arr = np.array(self._spread_history)
        else:
            arr = np.array(list(self._spread_history)[-self._lookback:])
        if len(arr) < 2:
            return {"dev_max": 0.0, "dev_min": 0.0, "avg_max": 0.0, "avg_min": 0.0}

        # Split into chunks to find local extremes
        chunk = max(1, len(arr) // 10)
        local_maxs, local_mins = [], []
        for i in range(0, len(arr), chunk):
            seg = arr[i:i + chunk]
            local_maxs.append(float(np.max(seg)))
            local_mins.append(float(np.min(seg)))

        return {
            "dev_max": float(np.max(arr)),
            "dev_min": float(np.min(arr)),
            "avg_max": float(np.mean(local_maxs)),
            "avg_min": float(np.mean(local_mins)),
        }

    # === Z-score properties (Variant A: from absolute spread_rub) ===

    def _push_spread(self):
        """Push current spread_rub to history."""
        s = self.spread_rub
        if s != 0.0:
            self._spread_history.append(s)

    def _spread_stats(self) -> tuple[float, float]:
        """Returns (mean, std) of spread_rub over lookback."""
        if len(self._spread_history) < self._lookback:
            arr = np.array(self._spread_history)
        else:
            arr = np.array(list(self._spread_history)[-self._lookback:])
        if len(arr) < 2:
            return 0.0, 0.0
        return float(np.mean(arr)), float(np.std(arr))

    @property
    def zscore(self) -> float:
        """Z-score from absolute spread: (spread_rub - mean_spread) / std_spread."""
        with self._lock:
            self._push_spread()
            mean, std = self._spread_stats()
            if std < 1e-9:
                return 0.0
            return (self.spread_rub - mean) / std

    @property
    def zscore_no_push(self) -> float:
        """Z-score from absolute spread without pushing (for monitoring)."""
        with self._lock:
            mean, std = self._spread_stats()
            if std < 1e-9:
                return 0.0
            return (self.spread_rub - mean) / std

    @property
    def basis_mean(self) -> float:
        """Mean of spread_rub over lookback."""
        with self._lock:
            mean, _ = self._spread_stats()
            return mean

    @property
    def basis_std(self) -> float:
        """Std of spread_rub over lookback."""
        with self._lock:
            _, std = self._spread_stats()
            return std

    @property
    def has_enough_data(self) -> bool:
        # Mode-aware (Fix #1): zscore mode needs deviation history at dev cadence;
        # spread_rub mode needs raw spread history at 1s cadence.
        if getattr(self, '_history_mode', 'dev') == 'spread':
            return len(self._spread_history) >= self._lookback
        return len(self._deviation_history) >= min(self._dev_lookback, 50)

    @property
    def data_points(self) -> int:
        return len(self._deviation_history)

    # === Deviation-based Z and annualized carry (Fix #1) ===

    def _deviation_z(self, push: bool) -> float:
        if push:
            self._push_deviation()
        with self._lock:
            mean, std = self._deviation_stats()
            if std < 1e-9:
                return 0.0
            return (self.deviation - mean) / std

    @property
    def zscore_dev(self) -> float:
        """Z-score of deviation-from-fair at slow push cadence.
        This is the CORRECT signal Z: deviation is stationary around 0
        (unlike raw spread which trends with carry to expiration)."""
        return self._deviation_z(push=True)

    @property
    def zscore_dev_no_push(self) -> float:
        return self._deviation_z(push=False)

    @property
    def dev_ann_pct(self) -> float:
        """Annualized deviation in % of spot notional.
        Answers 'how expensive is the futures vs fair' in yield terms:
        dev_ann = deviation / (spot_value) / days_to_exp * 365 * 100.
        Entry thresholds live in these units (comparable across expiries)."""
        spot_value = self._price_a * self._contract_size
        days = self._days_to_expiration()
        if spot_value <= 0 or days <= 0:
            return 0.0
        return self.deviation / spot_value / days * 365.0 * 100.0

    def get_state(self) -> dict:
        ext = self._deviation_extremes()
        fs = round(self.fair_spread, 1)
        return {
            "price_a": self._price_a,
            "price_b": self._price_b,
            "basis": round(self.basis, 2),
            "spread_rub": round(self.spread_rub, 2),
            "spread_long_rub": round(self.spread_long_rub, 2),
            "spread_pct": round(self.spread_pct, 3),
            "fair_spread": fs,
            "deviation": round(self.deviation, 2),
            "dev_ann_pct": round(self.dev_ann_pct, 2),
            "zscore": round(self.zscore_dev_no_push, 3),
            "zscore_dev": round(self.zscore_dev_no_push, 3),
            "zscore_spread": round(self.zscore_no_push, 3),
            "dev_mean": round(self._deviation_stats()[0], 2),
            "dev_std": round(self._deviation_stats()[1], 2),
            "basis_mean": round(self.basis_mean, 2),
            "basis_std": round(self.basis_std, 2),
            "data_points": self.data_points,
            "dev_points": len(self._deviation_history),
            "lookback": self._lookback,
            "dev_lookback": self._dev_lookback,
            "hedge_ratio": self._hedge_ratio,
            "rate": self._rate,
            "expiration_date": self._expiration_date,
            "contract_size": self._contract_size,
            "days_to_exp": self._days_to_expiration(),
            "dividends": [{"date": d, "amount": a} for d, a in self._dividends],
            "div_sum": round(self._dividends_before_expiration(), 2),
            "dev_extremes": ext,
            "suggested_high": round(ext["dev_max"], 1),
            "suggested_low": round(ext["dev_min"], 1),
            "bid_a": round(self.bid_a, 4),
            "ask_a": round(self.ask_a, 4),
            "bid_b": round(self.bid_b, 4),
            "ask_b": round(self.ask_b, 4),
        }
