"""F-061: Strategy orchestrator SAR-робота (сигналы движка → риск-слой → ордера).

Одноинстансовый: робот знает только свой тикер. Портфель — зона Дмитрия (D1-D3).
"""
import logging
from datetime import datetime, date

from sarema import sarema_engine as eng
from sarema.config_sarema import apply_limits_qty
from sarema.risk_sarema import (risk_sizing, apply_lot_rounding, apply_liq_cap,
                                check_kill_switch)
from sarema.orders_sarema import BUY, SELL

log = logging.getLogger("sarema.strategy")

FLAT, LONG, SHORT = 0, 1, -1


class SaremaStrategy:
    def __init__(self, cfg: dict, state, orders, feed, lot_size: float = 1.0):
        self.cfg = cfg
        self.state = state
        self.orders = orders
        self.feed = feed
        self.lot_size = lot_size
        self.stop_order_id: str | None = None
        self.last_stats: dict = {}

    # ---------- сигнальный проход на закрытом H1 ----------
    def on_closed_bar(self) -> dict:
        """Вызывается после появления нового закрытого H1-бара.
        Возвращает dict события для журнала/API."""
        g = self.feed.bars
        if g is None or len(g) < 3:
            return {"event": "skip", "reason": "no_bars"}
        # пересчёт индикаторов на всей истории (порт движка, дешёво на H1)
        df = g.rename(columns={"begin": "t"})
        g2, up, dn = eng.build(df, "60min", eng.SAR_AF0, eng.SAR_STEP, eng.EMA_N)
        i = len(g2) - 1                      # индекс последнего ЗАКРЫТОГО бара
        atr = g2["atr"].values
        events = []

        # 1) если позиция есть — трейлинг стопа по SAR этого бара + брокер-стоп
        if self.state.direction != 0:
            sar_i = float(g2["sar"].iloc[i])
            if self.state.direction == LONG:
                new_stop = max(self.state.stop_price, sar_i)
            else:
                new_stop = min(self.state.stop_price, sar_i)
            if abs(new_stop - self.state.stop_price) > 1e-9:
                self._replace_broker_stop(new_stop)
                self.state.stop_price = new_stop

        # 2) вход: сигнал на закрытом баре, флэт, входы не заблокированы
        if self.state.direction == FLAT and (dn[i] or up[i]):
            today = date.today()
            if self.state.entries_blocked(today):
                events.append({"event": "entry_blocked_kill_day"})
            else:
                new_kill = check_kill_switch(self.state, today, self.cfg.get("day_kill_R"))
                if new_kill:
                    self.state.kill_day = new_kill
                    self.state.save()
                    events.append({"event": "kill_switch_on", "kill_day": new_kill})
                else:
                    ev = self._try_enter(int(dn[i]) and LONG or SHORT, float(atr[i]),
                                         float(g2["volume"].iloc[i]))
                    events.append(ev)

        # 3) pending_signal не используется: вход по рынку сразу после закрытия бара
        #    (в бэктесте вход по open следующего бара; рынок в первые минуты часа ≈ то же)
        self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1])}
        return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}

    # ---------- вход ----------
    def _try_enter(self, direction: int, atr_signal: float, bar_volume: float) -> dict:
        last = self.feed.bars.iloc[-1]
        price_hint = float(last["close"])
        stop_dist = eng.INIT_STOP_ATR * atr_signal
        if stop_dist <= 0:
            return {"event": "skip", "reason": "bad_atr"}
        rs = risk_sizing(self.state.base, self.cfg.get("risk_pct", 0.005), stop_dist)
        qty, reason = apply_limits_qty(rs, price_hint, self.cfg)
        if qty < 1:
            self.state.save()
            return {"event": "signal_skipped", "reason": reason, "risk_sizing": rs}
        qty = apply_lot_rounding(qty, self.lot_size)
        if qty < 1:
            self.state.save()
            return {"event": "signal_skipped", "reason": "lot_rounding", "risk_sizing": rs}
        qty = apply_liq_cap(qty, bar_volume, self.cfg.get("liq_frac"))
        if qty < 1:
            self.state.save()
            return {"event": "signal_skipped", "reason": "liq_frac", "risk_sizing": rs}
        side = BUY if direction == LONG else SELL
        po = self.orders.place_market(side, qty, tag="sarema-entry")
        if not po:
            return {"event": "entry_error", "error": self.orders.last_error}
        # начальный стоп строится от фактической цены входа после fill (reconciliation
        # уточняет entry; здесь предварительный стоп от price_hint, дальше /resync)
        stop0 = price_hint - stop_dist if direction == LONG else price_hint + stop_dist
        self.state.direction = direction
        self.state.lots_qty = qty
        self.state.entry_price = price_hint
        self.state.stop_price = stop0
        self.state.entry_time = datetime.utcnow().isoformat()
        self._place_broker_stop(stop0, qty, direction)
        self.state.save()
        log.info(f"entry dir={direction} qty={qty} entry≈{price_hint:.2f} stop={stop0:.2f} reason={reason}")
        return {"event": "entry", "dir": direction, "qty": qty,
                "entry": price_hint, "stop": stop0, "limit_reason": reason}

    # ---------- брокер-стоп ----------
    def _place_broker_stop(self, stop: float, qty: int, direction: int) -> None:
        close_side = SELL if direction == LONG else BUY
        po = self.orders.place_stop_market(close_side, qty, stop, tag="sarema-stop")
        self.stop_order_id = po.order_id if po else None
        if not po:
            log.error("BROKER STOP NOT PLACED — позиция без защиты! Алерт.")

    def _replace_broker_stop(self, new_stop: float) -> None:
        if self.stop_order_id:
            self.orders.cancel(self.stop_order_id)
        self._place_broker_stop(new_stop, self.state.lots_qty, self.state.direction)

    # ---------- выход/переворот (по сигналу противоположного кросса) ----------
    def close_position(self, reason: str = "signal") -> dict:
        if self.state.direction == FLAT:
            return {"event": "close_skip_flat"}
        side = SELL if self.state.direction == LONG else BUY
        po = self.orders.place_market(side, self.state.lots_qty, tag=f"sarema-close-{reason}")
        if self.stop_order_id:
            self.orders.cancel(self.stop_order_id)
        if not po:
            return {"event": "close_error", "error": self.orders.last_error}
        log.info(f"close reason={reason} qty={self.state.lots_qty}")
        ev = {"event": "close", "dir": self.state.direction, "qty": self.state.lots_qty, "reason": reason}
        self.state.direction = FLAT
        self.state.lots_qty = 0
        self.stop_order_id = None
        self.state.save()
        return ev
