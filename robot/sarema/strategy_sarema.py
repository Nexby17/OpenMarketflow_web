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
from sarema.vp_sarema import VolumeProfileSarema, gate_check

log = logging.getLogger("sarema.strategy")

FLAT, LONG, SHORT = 0, 1, -1


class SaremaStrategy:
    def __init__(self, cfg: dict, state, orders, feed, lot_size: float = 1.0, ticker: str = ""):
        self.cfg = cfg
        self.state = state
        self.orders = orders
        self.feed = feed
        self.lot_size = lot_size
        self.ticker = ticker
        self.stop_order_id: str | None = None
        self.last_stats: dict = {}
        # F-065: Volume Profile (VAH/VAL/POC) — фильтр входа как у OF-роботов
        self.vp = VolumeProfileSarema(
            bin_size=cfg.get("vah_val_bin") or 0.0,
            va_pct=cfg.get("vah_val_pct") or 70.0,
            lookback=cfg.get("vah_val_lookback") or 200,
        )

    # ---------- F-065: VP helpers ----------
    def _vp_mode(self) -> str:
        return (self.cfg.get("vah_val_mode") or "off").lower()

    def _vp_rebuild_if_needed(self) -> None:
        """Пересоздать VP при смене подтипов через /params (mode меняет только гейт)."""
        bin_p = self.cfg.get("vah_val_bin") or 0.0
        pct_p = self.cfg.get("vah_val_pct") or 70.0
        lb_p = self.cfg.get("vah_val_lookback") or 200
        if (self.vp.bin_size != bin_p or self.vp.va_pct != pct_p
                or self.vp.lookback != lb_p):
            self.vp = VolumeProfileSarema(bin_size=bin_p, va_pct=pct_p, lookback=lb_p)

    # ---------- сигнальный проход на закрытом H1 ----------
    def on_closed_bar(self) -> dict:
        """Вызывается после появления нового закрытого H1-бара.
        Возвращает dict события для журнала/API."""
        g = self.feed.bars
        if g is None or len(g) < 3:
            return {"event": "skip", "reason": "no_bars"}
        # F-065: VP всегда строится из окна закрытых H1 (считается до ветки входа —
        # чтобы /status показывал VAH/VAL/POC даже при открытой позиции)
        self._vp_rebuild_if_needed()
        try:
            self.vp.feed_bars(g)
            self.vp.calculate()
        except Exception:
            pass
        # пересчёт индикаторов на всей истории (порт движка, дешёво на H1)
        # параметры берутся из конфига экземпляра (редактируемые Дмитрием в popup)
        df = g.rename(columns={"begin": "t"})
        g2, up, dn = eng.build(df, f"{int(self.cfg.get('tf_min', 60))}min",
                               self.cfg.get("sar_af0", 0.02), self.cfg.get("sar_step", 0.2),
                               int(self.cfg.get("ema_n", 12)), self.cfg.get("sar_max", 0.2))
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

            # F-073 (Дмитрий): закрытие по ПРОТИВОПОЛОЖНОМУ кроссу + разворот.
            # Обратный кросс на закрытом баре против позиции → close, затем вход в новую сторону
            # (использует тот же max_signal_age-фильтр свежести через entry_sig ниже НЕ нужно —
            # разворот делаем сразу: кросс сам по себе и есть сигнал).
            if self.state.direction == LONG and up[i]:
                events.append(self.close_position(reason="reverse-signal"))
                self.state.last_entry_signal_bar = str(g2["begin"].iloc[i])  # этот же бар = сигнал шорта
                ev = self._try_enter(SHORT, float(atr[i]), float(g2["volume"].iloc[i]),
                                     signal_bar=str(g2["begin"].iloc[i]), signal_age=0)
                events.append(ev)
                self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1])}
                return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}
            if self.state.direction == SHORT and dn[i]:
                events.append(self.close_position(reason="reverse-signal"))
                self.state.last_entry_signal_bar = str(g2["begin"].iloc[i])
                ev = self._try_enter(LONG, float(atr[i]), float(g2["volume"].iloc[i]),
                                     signal_bar=str(g2["begin"].iloc[i]), signal_age=0)
                events.append(ev)
                self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1])}
                return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}

        # 2) вход: сигнал на закрытом баре, флэт, входы не заблокированы.
        # F-071 (Дмитрий 14.10: «при включении должен входить, если сигнал 1-3 бара назад»):
        # сканируем окно последних max_signal_age баров; берём САМЫЙ СВЕЖИЙ кросс.
        # Сигнал недействителен, если: уже отработан (last_entry_signal_bar) или
        # SAR уже вернулся на другую сторону EMA (кросс отменился).
        entry_sig = None                      # (j, dir) — самый свежий сигнал в окне
        age = max(1, int(self.cfg.get("max_signal_age", 3))) + 1  # F-081: age N = N+1 бара (включая текущий)
        for j in range(i, max(i - age, -1), -1):
            if dn[j] or up[j]:
                entry_sig = (j, LONG if dn[j] else SHORT)
                break
        if entry_sig:
            j, direction = entry_sig
            sig_bar = str(g2["begin"].iloc[j])
            if self.state.last_entry_signal_bar == sig_bar:
                entry_sig = None              # этот сигнал уже отработан
            elif direction == LONG and not (float(g2["sar"].iloc[i]) < float(g2["ema"].iloc[i])):
                entry_sig = None              # SAR уже вернулся выше EMA — лонг-кросс отменился
            elif direction == SHORT and not (float(g2["sar"].iloc[i]) > float(g2["ema"].iloc[i])):
                entry_sig = None              # SAR уже вернулся ниже EMA — шорт-кросс отменился
        if self.state.direction == FLAT and entry_sig:
            j, direction = entry_sig
            sig_age = i - j
            # F-065: VA-гейт (VP Mode) — ДО лимитов/сайзинга; сигнал не расходуется
            vp_mode = self._vp_mode()
            if vp_mode != "off":
                price_ref = float(g2["close"].iloc[i])
                allowed, reason, zone = gate_check(vp_mode, price_ref,
                                                   self.vp.vah, self.vp.val)
                if not allowed:
                    events.append({"event": "entry_blocked_vp", "mode": vp_mode,
                                   "reason": reason, "price": round(price_ref, 4),
                                   "vah": round(self.vp.vah, 4), "val": round(self.vp.val, 4)})
                    return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}
            else:
                zone = ""
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
                    ev = self._try_enter(direction, float(atr[j]),
                                         float(g2["volume"].iloc[j]),
                                         signal_bar=str(g2["begin"].iloc[j]),
                                         signal_age=sig_age)
                    events.append(ev)

        # 3) pending_signal не используется: вход по рынку сразу после закрытия бара
        #    (в бэктесте вход по open следующего бара; рынок в первые минуты часа ≈ то же)
        self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1])}
        return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}

    # ---------- вход ----------
    def _try_enter(self, direction: int, atr_signal: float, bar_volume: float,
                   signal_bar: str = "", signal_age: int = 0) -> dict:
        last = self.feed.bars.iloc[-1]
        price_hint = float(last["close"])
        stop_dist = self.cfg.get("init_stop_atr", 3.0) * atr_signal
        if stop_dist <= 0:
            return {"event": "skip", "reason": "bad_atr"}
        rs = risk_sizing(self.state.base, self.cfg.get("risk_pct", 0.005), stop_dist)
        qty, reason = apply_limits_qty(rs, price_hint, self.cfg, direction)
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
        # F-074: реальная цена исполнения из ленты брокера (короткая пауза на проведение сделки)
        import time as _t
        fill_price = None
        for _ in range(5):
            _t.sleep(0.6)
            fi = self.orders.last_fill_by_comment("sarema-entry", symbol=self.orders._symbol)
            if fi and fi.quantity >= qty:
                fill_price = fi.price
                break
        if fill_price:
            price_hint = fill_price   # journal/stop считаются от факта
        # начальный стоп строится от фактической цены входа после fill (reconciliation
        # уточняет entry; здесь предварительный стоп от price_hint, дальше /resync)
        stop0 = price_hint - stop_dist if direction == LONG else price_hint + stop_dist
        # запись в журнал позиций (логика таблицы Дмитрия): открытие — сразу при входе
        from datetime import date as _date
        _now = datetime.utcnow()
        risk_pts = stop_dist
        vp_mode = self._vp_mode()
        self.state.journal_open({
            "tic": self.ticker,
            "tf": "H1",
            "date_open": _now.strftime("%Y-%m-%d"),
            "time_open": _now.strftime("%H:%M:%S"),
            "dir": "L" if direction == LONG else "S",
            "entry": price_hint,
            "qty": qty,
            "volume": round(qty * price_hint, 2),
            "risk_pct": round(qty * stop_dist / max(1e-9, self.state.base) * 100, 2),
            "risk_pts": round(risk_pts, 2),
            "stop": round(stop0, 2),
            "vp": (f"{vp_mode}:{zone}" if vp_mode != "off" and zone else (vp_mode if vp_mode != "off" else "signal")),
        })
        self.state.direction = direction
        self.state.lots_qty = qty
        self.state.entry_price = price_hint
        self.state.stop_price = stop0
        self.state.entry_time = datetime.utcnow().isoformat()
        self._place_broker_stop(stop0, qty, direction)
        if signal_bar:
            self.state.last_entry_signal_bar = signal_bar   # F-071: сигнал потрачен
        self.state.save()
        log.info(f"entry dir={direction} qty={qty} entry≈{price_hint:.2f} stop={stop0:.2f} "
                 f"signal_age={signal_age} signal_bar={signal_bar} reason={reason}")
        return {"event": "entry", "dir": direction, "qty": qty,
                "entry": price_hint, "stop": stop0, "limit_reason": reason,
                "signal_age": signal_age, "signal_bar": signal_bar}

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
        # закрытие в журнал позиций: дозапись справа в открытую строку (логика Дмитрия)
        from datetime import datetime as _dt
        _now = _dt.utcnow()
        _entry = self.state.entry_price
        _qty = self.state.lots_qty
        _dir = self.state.direction
        # F-074: реальная цена выхода из ленты брокера (по reason-тэгу), fallback — close бара
        _exit_px = None
        try:
            fi = self.orders.last_fill_by_comment(f"sarema-close-{reason}", symbol=self.orders._symbol)
            if fi and fi.price > 0:
                _exit_px = fi.price
        except Exception:
            pass
        if _exit_px is None:
            _exit_px = self.feed.bars["close"].iloc[-1] if (self.feed.bars is not None and len(self.feed.bars)) else _entry
        _commission = round(_qty * 0.90, 2)
        _net = (_exit_px - _entry) * _dir * _qty - _commission
        _risk_total = max(1e-9, self.state.base * self.cfg.get("risk_pct", 0.005))
        closed = self.state.journal_close({
            "date_close": _now.strftime("%Y-%m-%d"),
            "time_close": _now.strftime("%H:%M:%S"),
            "exit": round(float(_exit_px), 2),
            "commission": _commission,
            "pnl": round(_net, 2),
            "R": round(_net / _risk_total, 3),
            "reason": reason,
        })
        if closed:
            self.state.record_trade_R(_now.strftime("%Y-%m-%d"), closed["close"]["R"])
        ev = {"event": "close", "dir": _dir, "qty": _qty, "reason": reason, "journal": closed is not None}
        self.state.direction = FLAT
        self.state.lots_qty = 0
        self.stop_order_id = None
        self.state.save()
        return ev
