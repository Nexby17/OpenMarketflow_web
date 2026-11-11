"""MTLR strategy engine: SAR×EMA cross + pullback signals, RSI/regime filters,
TP/SL/Trailing risk management in pct/pts/rub. Indicator-driven, bar-close signals,
tick-level exits (SL/TP/trail checked against live last price).

Signal logic (per Samurai, 11.11.2026):
- use_cross (checkbox): entry immediately on SAR×EMA regime flip (osc sign change)
- use_pullback (checkbox): independent signal — pullback to EMA along EMA slope
- both on: pullback AFTER cross — wait for pullback near EMA/zero-osc then reversal
- both off: no entries
- Filters: RSI (long blocked when RSI>=upper, short when RSI<=lower),
  regime oscillator (long when osc>thr, short when osc<-thr) — each with checkbox.
- Regime flip against position always closes the position.
Exits priority on same tick: SL (if enabled) > TP > trailing.
"""

import logging
import threading
from typing import Optional, List, Dict

from indicators import ema, psar, atr, rsi, regime_osc
from config_mtlr import MtlrParams, TF_SECONDS

log = logging.getLogger("mtlr.strategy")

LONG = 1
SHORT = -1
FLAT = 0


class StrategyEngine:
    """Thread-safe engine: indicators + signals + position state + journal."""

    def __init__(self, params: MtlrParams):
        self.p = params
        self.lock = threading.RLock()
        # indicators cache (aligned to self.candles)
        self.candles: List[Dict] = []
        self.ema_v: List[Optional[float]] = []
        self.sar_v: List[Optional[float]] = []
        self.atr_v: List[Optional[float]] = []
        self.rsi_v: List[Optional[float]] = []
        self.osc_v: List[Optional[float]] = []
        self.last_bar_t: int = 0          # последняя полностью обработанная свеча
        # pending pullback entry (после кросса)
        self.pending: Optional[Dict] = None   # {side, born_t, deadline_t}
        # position
        self.position: Optional[Dict] = None  # {side, qty, entry, entry_t, tp, sl, peak, trail_active}
        self.journal: List[Dict] = []
        self.realized_pnl: float = 0.0
        self.commission_paid: float = 0.0
        self.peak_pnl: float = 0.0
        self.max_dd: float = 0.0
        self.last_signal: str = ""
        self.unrealized: float = 0.0
        self._open_trade: Optional[Dict] = None  # черновик сделки до закрытия

    # ================= indicators =================
    def recompute(self, candles: List[Dict]) -> None:
        with self.lock:
            self.candles = candles
            if not candles:
                return
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            self.ema_v = ema(closes, self.p.ema_period)
            self.sar_v = psar(highs, lows, self.p.sar_start, self.p.sar_step, self.p.sar_max)
            self.atr_v = atr(highs, lows, closes, self.p.atr_period)
            self.rsi_v = rsi(closes, self.p.rsi_period)
            self.osc_v = regime_osc(self.ema_v, self.sar_v, self.atr_v)

    # ================= helpers =================
    def _regime(self, i: int) -> int:
        o = self.osc_v[i]
        if o is None or o == 0:
            return FLAT
        return LONG if o > 0 else SHORT

    def _filters_ok(self, i: int, side: int) -> bool:
        if self.p.use_rsi and self.rsi_v[i] is not None:
            r = self.rsi_v[i]
            if side == LONG and r >= self.p.rsi_upper:
                return False
            if side == SHORT and r <= self.p.rsi_lower:
                return False
        if self.p.use_regime and self.osc_v[i] is not None:
            o = self.osc_v[i]
            if side == LONG and o <= self.p.regime_threshold:
                return False
            if side == SHORT and o >= -self.p.regime_threshold:
                return False
        return True

    def _qty_for(self, side: int, price: float) -> int:
        """Количество для входа:
        - оба поля (лонг и шорт) пустые/0 → АВТО: весь max_capital (qty = капитал/цена)
        - поле задано (>0) → это количество (урезается до max_capital — потолок)
        - поле 0 при заданном другом → направление запрещено"""
        lq, sq = self.p.long_qty, self.p.short_qty
        if price <= 0 or self.p.max_capital <= 0:
            return 0
        auto = (lq <= 0 and sq <= 0)
        if auto:
            return int(self.p.max_capital / price)
        qty = lq if side == LONG else sq
        if qty <= 0:
            return 0
        if qty * price > self.p.max_capital:
            qty = int(self.p.max_capital / price)
        return qty

    # ================= signal scan (closed bars only) =================
    def scan(self) -> List[Dict]:
        """Обрабатывает все новые ЗАКРЫТЫЕ свечи. Возвращает список событий
        [{action: 'open'|'close', side, qty, reason}] для исполнения."""
        events: List[Dict] = []
        with self.lock:
            n = len(self.candles)
            if n < 3:
                return events
            start_i = 0
            if self.last_bar_t:
                for idx in range(n - 1, -1, -1):
                    if self.candles[idx]["t"] == self.last_bar_t:
                        start_i = idx + 1
                        break
                else:
                    start_i = max(0, n - 2)
            else:
                start_i = max(1, n - 2)   # первая инициализация: обработать только последнюю закрытую
            for i in range(start_i, n):
                # последняя свеча в списке может быть ещё формирующейся — обрабатываем все,
                # кроме самой последней, если её время окончания не наступило
                if i == n - 1 and not self._bar_closed(i):
                    break
                evs = self._on_bar(i)
                events.extend(evs)
                self.last_bar_t = self.candles[i]["t"]
            return events

    def _bar_closed(self, i: int) -> bool:
        import time as _t
        tf = TF_SECONDS.get(self.p.timeframe, 300)
        return _t.time() >= self.candles[i]["t"] + tf

    def _on_bar(self, i: int) -> List[Dict]:
        events: List[Dict] = []
        if self.osc_v[i] is None or self.osc_v[i - 1] is None:
            return events
        prev_regime = LONG if self.osc_v[i - 1] > 0 else SHORT
        regime = self._regime(i)
        flip = regime != FLAT and prev_regime != FLAT and regime != prev_regime

        # --- 1) флип против позиции -> закрыть ---
        if self.position and flip and self.position["side"] != regime:
            events.append({"action": "close", "reason": "flip",
                           "side": self.position["side"], "qty": self.position["qty"]})

        # --- 2) сигналы входа ---
        if regime != FLAT:
            if self.p.use_cross and not self.p.use_pullback:
                # вход сразу по кроссу
                if flip and not self.position:
                    if self._filters_ok(i, regime):
                        qty = self._qty_for(regime, self.candles[i]["c"])
                        if qty > 0:
                            self.last_signal = f"cross→{'LONG' if regime==LONG else 'SHORT'}"
                            events.append({"action": "open", "side": regime, "qty": qty,
                                           "reason": "cross"})
                    else:
                        self.last_signal = "cross filtered"
            elif self.p.use_pullback:
                if self.p.use_cross:
                    # откат после кросса: создать/обслужить pending
                    if flip and not self.position:
                        tf = TF_SECONDS.get(self.p.timeframe, 300)
                        self.pending = {"side": regime, "born_t": self.candles[i]["t"],
                                        "deadline_t": self.candles[i]["t"] + self.p.pb_bars * tf}
                # проверка pending (в т.ч. созданного раньше)
                if self.pending and not self.position:
                    ev = self._check_pending(i)
                    if ev:
                        events.append(ev)
            # else: оба выкл — ничего

        # самостоятельный pullback (use_cross выключен)
        if self.p.use_pullback and not self.p.use_cross and not self.position:
            ev = self._check_standalone_pb(i)
            if ev:
                events.append(ev)

        return events

    def _check_pending(self, i: int) -> Optional[Dict]:
        """Откат после кросса: цена вернулась к EMA (в пределах depth×ATR) и осциллятор
        разворачивается от нуля в сторону режима."""
        pd_ = self.pending
        side = pd_["side"]
        c = self.candles[i]
        e, a, o, o_prev = self.ema_v[i], self.atr_v[i], self.osc_v[i], self.osc_v[i - 1]
        if e is None or a is None or o is None:
            return None
        # режим пропал или дедлайн -> отмена
        if self._regime(i) != side:
            self.pending = None
            return None
        if c["t"] > pd_["deadline_t"]:
            self.pending = None
            self.last_signal = "pullback window expired"
            return None
        zone = self.p.pb_depth_atr * a
        if side == LONG:
            touched = c["l"] <= e + zone
            turning = o > o_prev
            if (touched or self.p.pb_touch_only and c["l"] <= e) and turning and c["c"] > e:
                if self._filters_ok(i, LONG):
                    qty = self._qty_for(LONG, c["c"])
                    if qty > 0:
                        self.pending = None
                        self.last_signal = "cross+pullback→LONG"
                        return {"action": "open", "side": LONG, "qty": qty, "reason": "cross+pb"}
        else:
            touched = c["h"] >= e - zone
            turning = o < o_prev
            if (touched or self.p.pb_touch_only and c["h"] >= e) and turning and c["c"] < e:
                if self._filters_ok(i, SHORT):
                    qty = self._qty_for(SHORT, c["c"])
                    if qty > 0:
                        self.pending = None
                        self.last_signal = "cross+pullback→SHORT"
                        return {"action": "open", "side": SHORT, "qty": qty, "reason": "cross+pb"}
        return None

    def _check_standalone_pb(self, i: int) -> Optional[Dict]:
        """Самостоятельный откат: пуллбэк к EMA по её наклону после импульса."""
        if self.ema_v[i] is None or self.ema_v[i - 1] is None or self.atr_v[i] is None:
            return None
        c = self.candles[i]
        e = self.ema_v[i]
        a = self.atr_v[i]
        zone = self.p.pb_depth_atr * a
        j0 = max(0, i - self.p.pb_bars)
        # импульс перед откатом
        if e > self.ema_v[i - 1]:   # EMA растёт -> лонг
            impulse = max((self.candles[j]["c"] for j in range(j0, i)), default=0)
            if impulse > e + zone and c["l"] <= e + zone and c["c"] > e:
                if self._filters_ok(i, LONG):
                    qty = self._qty_for(LONG, c["c"])
                    if qty > 0:
                        self.last_signal = "pullback→LONG"
                        return {"action": "open", "side": LONG, "qty": qty, "reason": "pb"}
        elif e < self.ema_v[i - 1]:
            impulse = min((self.candles[j]["c"] for j in range(j0, i)), default=0)
            if impulse < e - zone and c["h"] >= e - zone and c["c"] < e:
                if self._filters_ok(i, SHORT):
                    qty = self._qty_for(SHORT, c["c"])
                    if qty > 0:
                        self.last_signal = "pullback→SHORT"
                        return {"action": "open", "side": SHORT, "qty": qty, "reason": "pb"}
        return None

    # ================= position / risk =================
    def _dist(self, unit: str, value: float, entry: float, qty: int) -> float:
        """Расстояние в рублях цены для единицы измерения."""
        if unit == "pct":
            return entry * value / 100.0
        if unit == "pts":
            return value * self.p.price_step
        if unit == "rub":
            return value / qty if qty else 0.0
        return 0.0

    def on_open_fill(self, side: int, qty: int, price: float, ts: float, reason: str) -> None:
        with self.lock:
            tp = None
            if self.p.tp_enabled:
                d = self._dist(self.p.tp_unit, self.p.tp_value, price, qty)
                tp = price + d if side == LONG else price - d
            sl = None
            if self.p.sl_enabled:
                d = self._dist(self.p.sl_unit, self.p.sl_value, price, qty)
                sl = price - d if side == LONG else price + d
            self.position = {"side": side, "qty": qty, "entry": price, "entry_t": ts,
                             "tp": tp, "sl": sl, "peak": price, "trail_active": False}
            self._open_trade = {"side": "LONG" if side == LONG else "SHORT", "qty": qty,
                                "entry_price": price, "entry_time": ts, "reason_in": reason}

    def on_close_fill(self, price: float, ts: float, reason: str) -> Optional[Dict]:
        with self.lock:
            if not self.position or not self._open_trade:
                return None
            pos = self.position
            trade = dict(self._open_trade)
            trade["exit_price"] = price
            trade["exit_time"] = ts
            trade["reason_out"] = reason
            gross = (price - pos["entry"]) * pos["qty"] * pos["side"]
            commission = (pos["entry"] + price) * pos["qty"] * self.p.commission_pct / 100.0
            trade["pnl"] = round(gross - commission, 2)
            trade["commission"] = round(commission, 2)
            self.journal.append(trade)
            if len(self.journal) > 5000:
                self.journal = self.journal[-5000:]
            self.realized_pnl += trade["pnl"]
            self.commission_paid += trade["commission"]
            self.peak_pnl = max(self.peak_pnl, self.realized_pnl)
            self.max_dd = max(self.max_dd, self.peak_pnl - self.realized_pnl)
            self.position = None
            self._open_trade = None
            self.unrealized = 0.0
            return trade

    def on_tick(self, last: float) -> Optional[Dict]:
        """Проверка TP/SL/Trailing по live-цене. Возвращает {action:'close', reason} или None."""
        with self.lock:
            if not self.position or last <= 0:
                return None
            pos = self.position
            side = pos["side"]
            pos["peak"] = max(pos["peak"], last) if side == LONG else min(pos["peak"], last)
            self.unrealized = (last - pos["entry"]) * pos["qty"] * side
            # trail activation
            if self.p.trail_enabled and self.p.trail_activate_pct > 0:
                move_pct = (pos["peak"] - pos["entry"]) / pos["entry"] * 100 * side
                if move_pct >= self.p.trail_activate_pct:
                    pos["trail_active"] = True
            elif self.p.trail_enabled:
                pos["trail_active"] = True
            # SL
            if self.p.sl_enabled and pos["sl"] is not None:
                if side == LONG and last <= pos["sl"]:
                    return {"action": "close", "reason": "sl", "side": side, "qty": pos["qty"]}
                if side == SHORT and last >= pos["sl"]:
                    return {"action": "close", "reason": "sl", "side": side, "qty": pos["qty"]}
            # TP
            if self.p.tp_enabled and pos["tp"] is not None:
                if side == LONG and last >= pos["tp"]:
                    return {"action": "close", "reason": "tp", "side": side, "qty": pos["qty"]}
                if side == SHORT and last <= pos["tp"]:
                    return {"action": "close", "reason": "tp", "side": side, "qty": pos["qty"]}
            # Trailing
            if self.p.trail_enabled and pos["trail_active"]:
                d = self._dist(self.p.trail_unit, self.p.trail_value, pos["entry"], pos["qty"])
                ts_price = pos["peak"] - d if side == LONG else pos["peak"] + d
                if side == LONG and last <= ts_price:
                    return {"action": "close", "reason": "trail", "side": side, "qty": pos["qty"]}
                if side == SHORT and last >= ts_price:
                    return {"action": "close", "reason": "trail", "side": side, "qty": pos["qty"]}
            return None

    # ================= journal stats =================
    def stats(self) -> Dict:
        with self.lock:
            trades = self.journal
            n = len(trades)
            wins = [t for t in trades if t["pnl"] > 0]
            losses = [t for t in trades if t["pnl"] <= 0]
            wr = len(wins) / n * 100 if n else 0.0
            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
            avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
            ev = wr / 100 * avg_win - (1 - wr / 100) * avg_loss
            gp = sum(t["pnl"] for t in wins)
            gl = abs(sum(t["pnl"] for t in losses))
            return {
                "trades": n, "wins": len(wins), "losses": len(losses),
                "win_rate": round(wr, 1),
                "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
                "ev": round(ev, 2),                      # матожидание ₽/сделку
                "profit_factor": round(gp / gl, 2) if gl > 0 else 0.0,
                "realized": round(self.realized_pnl, 2),
                "commission": round(self.commission_paid, 2),
                "max_dd": round(self.max_dd, 2),
            }

    # ================= state =================
    def state_dict(self) -> Dict:
        with self.lock:
            return {
                "position": self.position,
                "pending": self.pending,
                "open_trade": self._open_trade,
                "journal": self.journal[-2000:],
                "realized_pnl": self.realized_pnl,
                "commission_paid": self.commission_paid,
                "peak_pnl": self.peak_pnl,
                "max_dd": self.max_dd,
                "last_bar_t": self.last_bar_t,
            }

    def load_state(self, st: Dict) -> None:
        with self.lock:
            self.position = st.get("position")
            self.pending = st.get("pending")
            self._open_trade = st.get("open_trade")
            self.journal = st.get("journal", [])
            self.realized_pnl = st.get("realized_pnl", 0.0)
            self.commission_paid = st.get("commission_paid", 0.0)
            self.peak_pnl = st.get("peak_pnl", 0.0)
            self.max_dd = st.get("max_dd", 0.0)
            self.last_bar_t = st.get("last_bar_t", 0)
