"""MTLR strategy engine: SAR×EMA cross + pullback signals, RSI/regime filters,
TP/SL/Trailing risk management in pct/pts/rub. Indicator-driven, bar-close signals,
tick-level exits (SL/TP/trail checked against live last price).

Signal logic (по Самураю, 16.11.2026 — кросс стал отдельным сигналом):
- use_cross (checkbox): отдельный независимый сигнал — вход сразу по флипу режима
  (osc sign change), маркетом по закрытию бара
- use_pullback (checkbox): отдельный независимый сигнал — пуллбэк к EMA по её наклону
- оба включены: ДВА независимых сигнала, вход по первому сработавшему
  (зеркало бэктеста tick_cross_or_pb.py); приоритет на одном баре — кросс
- оба выкл: нет входов
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
        # тренд последнего закрытого дня: +1/-1; None = нет данных (при use_d1 вход блокируется)
        self.d1_dir: Optional[int] = None
        # legacy: pending pullback после кросса — не используется с 16.11.2026
        # (кросс и пуллбэк — независимые сигналы); поле оставлено для совместимости state
        self.pending: Optional[Dict] = None
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
    def _d1_ok(self, side: int) -> bool:
        """D1-фильтр (заказ Самурая 16.11.2026): вход только по направлению тренда
        последнего ЗАКРЫТОГО дня (close vs EMA дневки). Отсутствие данных (None)
        при включённом фильтре = вход запрещён (fail-safe). Выходы НЕ фильтрует."""
        if not self.p.use_d1:
            return True
        return self.d1_dir is not None and self.d1_dir == side

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
        """Количество для входа — семантика Самурая (per-направление):
        - поле ПУСТОЕ (None) → авто: qty = floor(max_capital / цена)
        - поле = 0 → направление запрещено
        - поле = N (>0) → фиксированное количество, урезается потолком max_capital"""
        if price <= 0 or self.p.max_capital <= 0:
            return 0
        qty = self.p.long_qty if side == LONG else self.p.short_qty
        lot = getattr(self.p, "lot_size", 1) or 1
        if qty is None:               # пусто → авто на весь капитал, кратно лоту
            return int(self.p.max_capital / price) // lot * lot
        if qty <= 0:                  # 0 → запрет направления
            return 0
        if qty * price > self.p.max_capital:
            qty = int(self.p.max_capital / price)
        return qty

    # ================= signal scan (closed bars only) =================
    def scan(self) -> List[Dict]:
        """Обрабатывает все новые ЗАКРЫТЫЕ свечи. Возвращает список событий
        [{action: 'open'|'close', side, qty, reason}] для исполнения.

        Гарантии (фикс 12.11.2026):
        - Никаких пропусков: обрабатываются ВСЕ бары новее last_bar_t по порядку
          (catch-up после провалов данных — флипы не теряются).
        - Последний бар списка обрабатывается только когда данные по нему долетели:
          либо появился следующий бар, либо прошло >= 2×tf с его закрытия (fallback
          на случай остановки торгов/паузы между сессиями)."""
        events: List[Dict] = []
        with self.lock:
            n = len(self.candles)
            if n < 3:
                return events
            tf = TF_SECONDS.get(self.p.timeframe, 300)
            start_i = 0
            if self.last_bar_t:
                # catch-up: первый бар СТРОГО новее last_bar_t, без пропусков
                for idx in range(n):
                    if self.candles[idx]["t"] > self.last_bar_t:
                        start_i = idx
                        break
                else:
                    return events            # новых баров нет
            else:
                start_i = max(1, n - 2)   # первая инициализация: обработать только последнюю закрытую
            import time as _t
            for i in range(start_i, n):
                if i == n - 1:
                    # последняя свеча: должна быть закрыта...
                    if not self._bar_closed(i):
                        break
                    # ...и данные по ней должны долететь (следующий бар уже идёт,
                    # либо прошло 2×tf — тогда обрабатываем как есть)
                    if _t.time() < self.candles[i]["t"] + 2 * tf:
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

        # --- 0) валидность бара для закрытия (фикс 22.11.2026) ---
        # catch-up бары, закрывшиеся ДО входа в позицию, не могут её закрывать
        # (старт с галкой из stopped: догонял вчерашней историей и закрывал
        # свежую позицию по «flip»/«regime≠pos» на баре, старше самого входа).
        close_valid = True
        if self.position and self.position.get("entry_t") is not None:
            tf = TF_SECONDS.get(self.p.timeframe, 300)
            close_valid = self.candles[i]["t"] + tf > self.position["entry_t"]

        # --- 1) флип против позиции -> закрыть (КРОМЕ дончиан: у него выход только люстра) ---
        if self.position and flip and self.position["side"] != regime \
                and self.position.get("src") != "donch" and close_valid:
            events.append({"action": "close", "reason": "flip",
                           "side": self.position["side"], "qty": self.position["qty"]})

        # --- 1b) позиция против АКТИВНОГО регима -> закрыть по состоянию (18.11) ---
        # pb-вход при уже развёрнутом противоположном режиме не имеет ни SL
        # (SAR не с той стороны), ни флип-события (смена была до входа) — висел без защиты.
        if self.position and regime != FLAT and self.position["side"] != regime \
                and self.position.get("src") != "donch" and close_valid \
                and not any(ev.get("action") == "close" for ev in events):
            self.last_signal = f"regime≠pos→{'LONG' if regime == LONG else 'SHORT'}"
            events.append({"action": "close", "reason": "regime",
                           "side": self.position["side"], "qty": self.position["qty"]})

        # --- 2) сигналы входа: два НЕЗАВИСИМЫХ источника (16.11.2026) ---
        if regime != FLAT:
            # кросс: отдельный сигнал — вход сразу по флипу режима
            if self.p.use_cross:
                if flip and not self.position:
                    if self._filters_ok(i, regime) and self._d1_ok(regime):
                        qty = self._qty_for(regime, self.candles[i]["c"])
                        if qty > 0:
                            self.last_signal = f"cross→{'LONG' if regime==LONG else 'SHORT'}"
                            events.append({"action": "open", "side": regime, "qty": qty,
                                           "reason": "cross"})
                    else:
                        self.last_signal = "cross filtered"
            # дончиан: отдельный сигнал — пробой канала N баров (16.11)
            if self.p.use_donchian and not self.position and i >= self.p.donch_n:
                nb = self.p.donch_n
                hh = max(self.candles[j]["h"] for j in range(i - nb, i))
                ll = min(self.candles[j]["l"] for j in range(i - nb, i))
                prev_in = self.candles[i - 1]["h"] <= hh and self.candles[i - 1]["l"] >= ll
                if prev_in:
                    b_ = self.candles[i]
                    side = 1 if b_["h"] > hh else (-1 if b_["l"] < ll else 0)
                    if side and self._filters_ok(i, side) and self._d1_ok(side):
                        qty = self._qty_for(side, b_["c"])
                        if qty > 0:
                            self.last_signal = f"donch{'↑' if side == 1 else '↓'} N{nb}"
                            events.append({"action": "open", "side": side, "qty": qty,
                                           "reason": "donch"})
            # пуллбэк: отдельный сигнал, работает независимо от use_cross
            if self.p.use_pullback and not self.position:
                ev = self._check_standalone_pb(i)
                if ev:
                    events.append(ev)
            # оба выкл — ничего

        return events

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
            # галка «Касание EMA»: True — строго low<=EMA (видимое касание линии);
            # False — вход в зону EMA+depth×ATR. Импульс всегда по зоне.
            pb_ok = (c["l"] <= e) if self.p.pb_touch_only else (c["l"] <= e + zone)
            if impulse > e + zone and pb_ok and c["c"] > e:
                if self._filters_ok(i, LONG) and self._d1_ok(LONG):
                    qty = self._qty_for(LONG, c["c"])
                    if qty > 0:
                        self.last_signal = "pullback→LONG"
                        return {"action": "open", "side": LONG, "qty": qty, "reason": "pb"}
        elif e < self.ema_v[i - 1]:
            impulse = min((self.candles[j]["c"] for j in range(j0, i)), default=0)
            pb_ok = (c["h"] >= e) if self.p.pb_touch_only else (c["h"] >= e - zone)
            if impulse < e - zone and pb_ok and c["c"] < e:
                if self._filters_ok(i, SHORT) and self._d1_ok(SHORT):
                    qty = self._qty_for(SHORT, c["c"])
                    if qty > 0:
                        self.last_signal = "pullback→SHORT"
                        return {"action": "open", "side": SHORT, "qty": qty, "reason": "pb"}
        return None

    def entry_limit_signal(self) -> Optional[Dict]:
        """Уровень постановки entry-лимитки на касании EMA (заказ 14.11: вход без
        ожидания закрытия бара; 16.11 — работает и при включённом кроссе:
        вход «или так или так»: pb→лимитка на EMA, кросс→маркет по флипу).
        Проверяется на ЗАКРЫТЫХ барах после пересчёта:
        EMA меняет наклон в сторону сигнала + импульс (для лонга max close за
        pb_bars выше EMA+depth×ATR). Касание/подтверждение close исполняет биржа.
        Возвращает {side, level(EMA, 2 знака), qty} или None."""
        if not self.p.use_pullback or not self.p.entry_by_limit:
            return None
        with self.lock:
            i = len(self.candles) - 1
            if i < 1 or self.position:
                return None
            e, e_prev, a = self.ema_v[i], self.ema_v[i - 1], self.atr_v[i]
            if e is None or e_prev is None or a is None:
                return None
            zone = self.p.pb_depth_atr * a
            j0 = max(0, i - self.p.pb_bars)
            if e > e_prev:
                impulse = max((self.candles[j]["c"] for j in range(j0, i)), default=0)
                if impulse > e + zone and self._filters_ok(i, LONG) and self._d1_ok(LONG):
                    qty = self._qty_for(LONG, e)
                    if qty > 0:
                        return {"side": LONG, "level": round(e, 2), "qty": qty}
            elif e < e_prev:
                impulse = min((self.candles[j]["c"] for j in range(j0, i)), default=0)
                if impulse < e - zone and self._filters_ok(i, SHORT) and self._d1_ok(SHORT):
                    qty = self._qty_for(SHORT, e)
                    if qty > 0:
                        return {"side": SHORT, "level": round(e, 2), "qty": qty}
        return None

    def live_flip_side(self, live_bar: Dict) -> Optional[int]:
        """Внутричасовой кросс SAR×EMA (заказ Самурая 16.11.2026): режим с учётом
        ЖИВОГО (незакрытого) бара. Пересчитываем индикаторы на закрытых барах
        + живой бар; если знак осциллятора сменился относительно последнего
        ЗАКРЫТОГО бара — это момент кросса. Возвращает сторону нового режима
        (LONG/SHORT) или None. Входы по-прежнему только по закрытым барам —
        метод только для закрытия позиции в момент кросса."""
        with self.lock:
            if not self.candles or len(self.candles) < 2:
                return None
            tf = TF_SECONDS.get(self.p.timeframe, 300)
            # живой бар должен быть СЛЕДУЮЩИМ за последним закрытым
            if live_bar.get("t") != self.candles[-1]["t"] + tf:
                return None
            candles = list(self.candles) + [live_bar]
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            e_v = ema(closes, self.p.ema_period)
            s_v = psar(highs, lows, self.p.sar_start, self.p.sar_step, self.p.sar_max)
            a_v = atr(highs, lows, closes, self.p.atr_period)
            o_v = regime_osc(e_v, s_v, a_v)
            o_live, o_prev = o_v[-1], o_v[-2]
            if o_live is None or o_prev is None or o_live == 0 or o_prev == 0:
                return None
            if (o_live > 0) != (o_prev > 0):
                return LONG if o_live > 0 else SHORT
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
            sl = None
            src = reason
            if reason == "donch":
                # люстра: стоп = цена − k×ATR последнего закрытого бара;
                # далее перестанавливается за peak на каждом закрытии (main._sl_target)
                # TP нет — у дончиана выход только люстра
                a = self.atr_v[-1] if self.atr_v else None
                if a:
                    sl = price - self.p.donch_k_atr * a if side == LONG \
                        else price + self.p.donch_k_atr * a
                else:
                    log.warning("Дончиан: ATR недоступен — люстра НЕ выставлена!")
            else:
                if self.p.tp_enabled:
                    d = self._dist(self.p.tp_unit, self.p.tp_value, price, qty)
                    tp = price + d if side == LONG else price - d
                if self.p.sl_enabled:
                    if self.p.sl_unit == "sar":
                        # SL на SAR (заказ Самурая 16.11.2026): стоп = SAR последнего
                        # закрытого бара; далее следует за SAR на каждом закрытии (main)
                        sl = self.sar_v[-1] if self.sar_v else None
                        if sl is None:
                            log.warning("SL=SAR: SAR недоступен — стоп НЕ выставлен!")
                        elif (side == LONG and sl >= price) or (side == SHORT and sl <= price):
                            log.warning(f"SL=SAR: SAR {sl:.2f} не с той стороны цены {price:.2f} — стоп НЕ выставлен")
                            sl = None
                    else:
                        d = self._dist(self.p.sl_unit, self.p.sl_value, price, qty)
                        sl = price - d if side == LONG else price + d
            self.position = {"side": side, "qty": qty, "entry": price, "entry_t": ts,
                             "tp": tp, "sl": sl, "peak": price, "trail_active": False,
                             "src": src}
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

    def on_tick(self, last: float) -> None:
        """Обновление пиковой цены и unrealized PnL (для трейлинга и отображения).
        ЗАКРЫТИЙ ЗДЕСЬ НЕТ: SL/TP исполняет биржа по заявкам; флип — на закрытии бара.
        Трейлинг-уровень вычисляется в main (orders_loop) и переставляет биржевую стоп-заявку."""
        with self.lock:
            if not self.position or last <= 0:
                return
            pos = self.position
            side = pos["side"]
            pos["peak"] = max(pos["peak"], last) if side == LONG else min(pos["peak"], last)
            self.unrealized = (last - pos["entry"]) * pos["qty"] * side
            if self.p.trail_enabled and self.p.trail_activate_pct > 0:
                move_pct = (pos["peak"] - pos["entry"]) / pos["entry"] * 100 * side
                if move_pct >= self.p.trail_activate_pct:
                    pos["trail_active"] = True
            elif self.p.trail_enabled:
                pos["trail_active"] = True

    def trail_stop_price(self) -> Optional[float]:
        """Актуальный трейлинг-стоп уровень (для перестановки биржевой заявки). None — не активен."""
        with self.lock:
            pos = self.position
            if not pos or not self.p.trail_enabled or not pos.get("trail_active"):
                return None
            d = self._dist(self.p.trail_unit, self.p.trail_value, pos["entry"], pos["qty"])
            return pos["peak"] - d if pos["side"] == LONG else pos["peak"] + d

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
