"""F-061: Strategy orchestrator SAR-робота (сигналы движка → риск-слой → ордера).

Одноинстансовый: робот знает только свой тикер. Портфель — зона Дмитрия (D1-D3).
"""
import logging
from datetime import datetime, date

from sarema import sarema_engine as eng
from sarema.config_sarema import apply_limits_qty
from sarema.risk_sarema import (risk_sizing, apply_lot_rounding, apply_liq_cap,
                                )
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
        self.last_sar: float | None = None   # F-105: SAR последнего закрытого бара
        self._last_tp_px: float | None = None   # F-105h: цена текущей TP-лимитки
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
        # F-112: диагностический лог последнего закрытого бара (сверка сигналов)
        try:
            log.info(f"BAR {g2['begin'].iloc[i]} close={float(g2['close'].iloc[i]):.2f} "
                     f"sar={float(g2['sar'].iloc[i]):.2f} ema={float(g2['ema'].iloc[i]):.2f} "
                     f"up={bool(up[i])} dn={bool(dn[i])} pos={self.state.direction}")
        except Exception:
            pass

        # 1) если позиция есть — трейлинг стопа по SAR этого бара + брокер-стоп
        sar_i = float(g2["sar"].iloc[i])
        self.last_sar = sar_i
        if self.state.direction != 0:
            close_i = float(g2["close"].iloc[i])
            # F-096: трейлинг-стоп ОТ ЦЕНЫ (запрос Дмитрия): если trail_value задан
            # (% / рубли / пункты) — стоп тянется за close закрытого бара на trail-дистанцию,
            # SAR в трейлинге не участвует. Пусто = классика (трейлинг за SAR).
            trail_dist = self._unit_dist(self.cfg.get("trail_value"), self.cfg.get("trail_unit"),
                                         close_i, float(atr[i]))
            price_trail = trail_dist is not None and trail_dist > 0
            # F-098 (модель Дмитрия): если задан трейлинг — стоп = первоначальный_стоп + рост цены
            # от входа; двигаем шагом не меньше trail_dist; назад не откатывается. SAR не участвует.
            if price_trail and self.state.initial_stop_price > 0 and self.state.entry_price > 0:
                # F-110: SHORT — стоп едет вниз: want = initial − (entry − close)
                _sign = 1 if self.state.direction == LONG else -1
                _rise = max(0.0, (close_i - self.state.entry_price) * _sign)
                _want = self.state.initial_stop_price + _sign * _rise
                if self.state.direction == LONG:
                    new_stop = max(self.state.stop_price, _want)
                    if new_stop - self.state.stop_price < trail_dist - 1e-9:
                        new_stop = self.state.stop_price
                else:
                    new_stop = min(self.state.stop_price, _want)
                    if self.state.stop_price - new_stop < trail_dist - 1e-9:
                        new_stop = self.state.stop_price
            elif price_trail:
                if self.state.direction == LONG:
                    new_stop = max(self.state.stop_price, sar_i)
                else:
                    new_stop = min(self.state.stop_price, sar_i)
            else:
                if self.state.direction == LONG:
                    new_stop = max(self.state.stop_price, sar_i)
                else:
                    new_stop = min(self.state.stop_price, sar_i)
            if abs(new_stop - self.state.stop_price) > 1e-9:
                self._replace_broker_stop(new_stop)
                self.state.stop_price = new_stop

            # F-105h: плавающий TP (sar) — лимитка снимается и перевыставляется за новым SAR.
            # Также восстанавливаем лимитку, если её нет (исполнилась/снялась брокером).
            if (self.cfg.get("tp_mode") or "fixed").lower() == "sar" and self.cfg.get("tp_R") and self.state.direction != 0:
                try:
                    _tp_new = self.tp_price(self.state.direction, self.state.entry_price, sar_i, float(atr[i]))
                    if _tp_new and _tp_new > 0:
                        _need_replace = self.state.tp_order_id is None
                        if not _need_replace and self._last_tp_px and abs(self._last_tp_px - _tp_new) > 1e-9:
                            _need_replace = True
                        if _need_replace:
                            if self.state.tp_order_id:
                                try:
                                    self.orders.cancel(self.state.tp_order_id)
                                except Exception:
                                    pass
                            _close_side = SELL if self.state.direction == LONG else BUY
                            _tpo = self.orders.place_limit(_close_side, self.state.lots_qty, round(_tp_new, 2), tag="sarema-tp")
                            self.state.tp_order_id = _tpo.order_id if _tpo else None
                            self._last_tp_px = _tp_new
                            log.info(f"TP limit moved (sar) @ {_tp_new:.2f} order={self.state.tp_order_id}")
                except Exception as _tpe:
                    log.error(f"TP move error: {_tpe}")

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
                self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1]), "atr": float(atr[i])}
                return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}
            if self.state.direction == SHORT and dn[i]:
                events.append(self.close_position(reason="reverse-signal"))
                self.state.last_entry_signal_bar = str(g2["begin"].iloc[i])
                ev = self._try_enter(LONG, float(atr[i]), float(g2["volume"].iloc[i]),
                                     signal_bar=str(g2["begin"].iloc[i]), signal_age=0)
                events.append(ev)
                self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1]), "atr": float(atr[i])}
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
            # F-161: после закрытия по стопу старый сигнал погашен — вход только по сигналу,
            # возникшему НА баре стопа или позже
            _lsb = str(getattr(self.state, 'last_stop_bar', '') or '')
            if _lsb and str(g2['begin'].iloc[j]) < _lsb:
                # F-164: гасим только СТРОГО старее стоп-бара; кросс на баре стопа = свежий
                # разворотный сигнал (возник одновременно со стопом) — разрешаем
                entry_sig = None
                events.append({'event': 'entry_skipped_post_stop', 'signal_bar': str(g2['begin'].iloc[j]), 'stop_bar': _lsb})
                return {'events': events, 'last_bar': str(g2['begin'].iloc[-1])}
            sig_age = i - j
            # F-065: VA-гейт (VP Mode) — ДО лимитов/сайзинга; сигнал не расходуется
            vp_mode = self._vp_mode()
            zone = ""
            if vp_mode != "off":
                price_ref = float(g2["close"].iloc[i])
                allowed, reason, zone = gate_check(vp_mode, price_ref,
                                                   self.vp.vah, self.vp.val)
                if not allowed:
                    events.append({"event": "entry_blocked_vp", "mode": vp_mode,
                                   "reason": reason, "price": round(price_ref, 4),
                                   "vah": round(self.vp.vah, 4), "val": round(self.vp.val, 4)})
                    return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}
            ev = self._try_enter(direction, float(atr[j]),
                                 float(g2["volume"].iloc[j]),
                                 signal_bar=str(g2["begin"].iloc[j]),
                                 signal_age=sig_age)
            events.append(ev)

        # 3) pending_signal не используется: вход по рынку сразу после закрытия бара
        #    (в бэктесте вход по open следующего бара; рынок в первые минуты часа ≈ то же)
        self.last_stats = {"bars": len(g2), "last_bar": str(g2["begin"].iloc[-1]), "atr": float(atr[i])}
        return {"events": events, "last_bar": str(g2["begin"].iloc[-1])}

    # ---------- вход ----------
    # ---------- F-092: стоп/трейлинг в единицах (%|rub|points; atr/sar = классика) ----------
    def _unit_dist(self, value, unit, ref_price: float, atr_signal: float) -> float | None:
        """Дистанция стопа в цене. value=None или unit atr/sar → None (классическое поведение).
        pct: value% от ref_price; rub: value рублей цены (не ₽-PnL!); points: value пунктов цены."""
        if value is None or ref_price <= 0:
            return None
        u = (unit or '').lower()
        v = float(value)
        if u == 'pct':
            return ref_price * v / 100.0
        if u == 'rub':
            return v
        if u == 'points':
            return v
        return None                      # atr / sar / неизвестное — классика

    # ---------- F-105: TP по R (fixed от входа | sar — плавает за SAR) ----------
    def tp_price(self, direction: int, entry: float, sar: float, atr: float) -> float | None:
        """Цена TP. fixed: entry ± tp_R × риск_руб/qty в цене (через риск в цене входа).
        sar: sar ± tp_R × 3×ATR (плавает вместе с SAR). None = TP выключен."""
        tp_r = self.cfg.get("tp_R")
        if not tp_r:
            return None
        mode = (self.cfg.get("tp_mode") or "fixed").lower()
        stop_dist, _ = self._initial_stop_dist(direction, entry, atr)
        if mode == "sar":
            base = sar
        else:
            base = entry
        dist = float(tp_r) * stop_dist
        return base + dist if direction == LONG else base - dist

    def _initial_stop_dist(self, direction: int, price_hint: float, atr_signal: float,
                           qty: int | None = None) -> tuple[float, str]:
        """Дистанция начального стопа: стоп-лосс Дмитрия (если задан) или 3×ATR.
        F-094: unit 'rub_pnl' — стоп в рублях УБЫТКА: дистанция = стоп_руб / qty."""
        u = (self.cfg.get("stop_loss_unit") or '').lower()
        v = self.cfg.get("stop_loss_value")
        if u == 'rub_pnl' and v is not None and qty and qty >= 1:
            d = float(v) / qty
            if d > 0:
                return d, "stop_loss:rub_pnl"
        d = self._unit_dist(v, u, price_hint, atr_signal)
        if d is not None and d > 0:
            return d, f"stop_loss:{u}"
        return self.cfg.get("init_stop_atr", 3.0) * atr_signal, "atr"

    def _try_enter(self, direction: int, atr_signal: float, bar_volume: float,
                   signal_bar: str = "", signal_age: int = 0) -> dict:
        last = self.feed.bars.iloc[-1]
        price_hint = float(last["close"])
        # F-094: если стоп в рублях убытка — сайзинг всё равно делаем по ATR-дистанции,
        # финальный стоп пересчитывается от фактического qty ниже.
        stop_dist, stop_basis = self._initial_stop_dist(direction, price_hint, atr_signal)
        if stop_dist <= 0:
            return {"event": "skip", "reason": "bad_atr"}
        # F-131: size_mode=capital — объём = ВЕСЬ Капитал / цена (команда Дмитрия)
        if (self.cfg.get("size_mode") or "risk").lower() == "capital":
            rs = int((self.state.base / price_hint) if price_hint > 0 else 0)
            reason = "capital-sizing"
        else:
            rs = risk_sizing(self.state.base, self.cfg.get("risk_pct", 0.005), stop_dist)
            reason = "risk_sizing"
        qty, reason = apply_limits_qty(rs, price_hint, self.cfg, direction)
        if qty < 1:
            self.state.save()
            return {"event": "signal_skipped", "reason": reason, "risk_sizing": rs}
        # F-124: Лимит участия удалён (Дмитрий) — сайзинг идёт напрямую к лот-округлению
        qty = apply_lot_rounding(qty, self.lot_size)
        if qty < 1:
            self.state.save()
            return {"event": "signal_skipped", "reason": "lot_rounding", "risk_sizing": rs}
        # F-094: стоп в рублях убытка — дистанция = стоп_руб / qty (после финального сайзинга)
        if (self.cfg.get("stop_loss_unit") or '').lower() == 'rub_pnl' and self.cfg.get("stop_loss_value") is not None:
            stop_dist, stop_basis = self._initial_stop_dist(direction, price_hint, atr_signal, qty=qty)
        side = BUY if direction == LONG else SELL
        # F-157: перед входом снять ВСЕ висячие sarema-* заявки (старые стопы/TP прошлых позиций —
        # их исполнение на новой позиции удваивает объём, кейс 05.11 18:04).
        try:
            _cleaned = self.orders.cancel_all_robot_orders()
            if _cleaned:
                log.info(f'F-157: перед входом снято {_cleaned} висячих заявок')
        except Exception as _ce:
            log.error(f'F-157 pre-entry cancel_all err: {_ce}')
        po = self.orders.place_market(side, qty, tag="sarema-entry")
        if not po:
            return {"event": "entry_error", "error": self.orders.last_error}
        # F-074 + F-083: реальная цена исполнения из ленты брокера.
        # Если fill не найден за 3 сек — ордер НЕ исполнен (отклонён/нет денег/лимит) —
        # ОТКАТ state и journal: фантомной позиции быть не должно!
        import time as _t
        fill_price = None
        fill_qty = 0
        for _ in range(5):
            _t.sleep(0.6)
            fi = self.orders.last_fill_by_comment("sarema-entry", symbol=self.orders._symbol)
            if fi and fi.quantity > 0:
                fill_price = fi.price
                fill_qty = fi.quantity
                break
        if not fill_price or fill_qty < 1:
            log.error(f"F-083: ENTRY NOT FILLED — откат state (order_id={po.order_id}, qty={qty})")
            return {"event": "entry_not_filled", "order_id": po.order_id, "qty_requested": qty,
                    "error": self.orders.last_error or 'no fill in 3s'}
        # начальный стоп строится от фактической цены входа после fill (reconciliation
        # уточняет entry; здесь предварительный стоп от price_hint, дальше /resync)
        stop0 = fill_price - stop_dist if direction == LONG else fill_price + stop_dist   # F-121: от РЕАЛЬНОЙ цены
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
            "entry": round(fill_price, 2),   # F-121: РЕАЛЬНАЯ цена исполнения от брокера
            "qty": qty,
            "volume": round(qty * fill_price, 2),
            "risk_pct": round(qty * stop_dist / max(1e-9, self.state.base) * 100, 2),
            "risk_pts": round(risk_pts, 2),
            "stop": round(stop0, 2),
            "vp": (f"{vp_mode}:{getattr(self, '_last_zone', '')}" if vp_mode != "off" else "signal"),
        })
        self.state.direction = direction
        self.state.lots_qty = qty
        self.state.entry_price = fill_price   # F-121: РЕАЛЬНАЯ цена исполнения
        self.state.stop_price = stop0
        self.state.initial_stop_price = stop0   # F-098: база трейлинга (стоп не двигается ближе этого зазора)
        from datetime import timedelta as _td_f164
        self.state.entry_time = (datetime.utcnow() + _td_f164(hours=3)).isoformat()  # F-164/F-166: МСК
        self._place_broker_stop(stop0, qty, direction)
        # F-105: TP-лимитка (fixed: от входа; sar: от SAR последнего бара)
        try:
            _sar_last = float(self.feed.sar_last) if hasattr(self.feed, "sar_last") else None
        except Exception:
            _sar_last = None
        try:
            _tp_px = self.tp_price(direction, price_hint, _sar_last or price_hint, atr_signal)
            if _tp_px:
                _close_side = SELL if direction == LONG else BUY   # F-139 revert: закрытие LONG=SELL, SHORT=BUY   # F-139 fix: закрывающая сторона (LONG→SELL, SHORT→BUY)
                _tpo = self.orders.place_limit(_close_side, qty, round(_tp_px, 2), tag="sarema-tp")
                self.state.tp_order_id = _tpo.order_id if _tpo else None
                self._last_tp_px = _tp_px
                log.info(f"TP limit @ {_tp_px:.2f} ({self.cfg.get('tp_mode')} mode) order={self.state.tp_order_id}")
        except Exception as _tpe:
            log.error(f"TP limit error: {_tpe}")
        if signal_bar:
            self.state.last_entry_signal_bar = signal_bar   # F-071: сигнал потрачен
        self.state.save()
        log.info(f"entry dir={direction} qty={qty} entry≈{price_hint:.2f} stop={stop0:.2f} "
                 f"signal_age={signal_age} signal_bar={signal_bar} reason={reason}")
        return {"event": "entry", "dir": direction, "qty": qty,
                "entry": price_hint, "stop": stop0, "limit_reason": reason,
                "signal_age": signal_age, "signal_bar": signal_bar}

    # ---------- брокер-стоп ----------
    def cleanup_duplicate_stops(self) -> int:
        """F-117: удалить дублирующиеся стоп-заявки у брокера (накопились после рестартов,
        когда stop_order_id терялся). Оставляем ту, чей stop_price ближе к state.stop_price.
        Возвращает число отменённых."""
        try:
            actives = self.orders.list_active_stop_orders()
            if len(actives) <= 1:
                return 0
            # F-145: приоритет — заявка из state.stop_order_id (её поставил трейлинг последним),
            # иначе ближайшая к state.stop_price.
            sid = str(self.state.stop_order_id or '')
            keep = None
            if sid:
                for a in actives:
                    if str(a.get('order_id')) == sid:
                        keep = a
                        break
            if keep is None:
                target = float(self.state.stop_price or 0)
                if target > 0:
                    actives.sort(key=lambda x: abs(float(x['stop_price']) - target))
                keep = actives[0]
            cancelled = 0
            keep_id = str(keep.get('order_id'))
            for o in actives:
                if str(o.get('order_id')) == keep_id:
                    continue
                try:
                    self.orders.cancel(o['order_id'])
                    cancelled += 1
                except Exception:
                    pass
            self.stop_order_id = keep['order_id']
            self.state.stop_order_id = keep['order_id']
            self.state.save()
            log.info(f'F-117: дубли стопов удалены ({cancelled}), оставлена заявка {keep["order_id"]} @ {keep["stop_price"]}')
            return cancelled
        except Exception as e:
            log.error(f'cleanup_duplicate_stops err: {e}')
            return 0

    def _place_broker_stop(self, stop: float, qty: int, direction: int) -> None:
        """F-113: брокерский STOP-ордер (place_stop_loss) — реальная заявка у брокера,
        исполняется мгновенно при пробитии, не зависит от цикла робота."""
        po = self.orders.place_stop_loss(direction, qty, stop, tag="sarema-stop")
        self.stop_order_id = po.order_id if po else None
        self.state.stop_order_id = self.stop_order_id
        if not po:
            log.error("BROKER STOP NOT PLACED — позиция без защиты! Алерт.")

    def _replace_broker_stop(self, new_stop: float) -> None:
        if self.stop_order_id:
            self.orders.cancel(self.stop_order_id)
        self._place_broker_stop(new_stop, self.state.lots_qty, self.state.direction)
        # F-145: после постановки новой стоп-заявки снять ВСЕ оставшиеся активные стопы робота
        # (старая могла не сняться: сеть/статус/листинг-задержка). Дубли = каскад (урок 03.11).
        try:
            for o in self.orders.list_active_stop_orders():
                oid = str(o.get('order_id', ''))
                if oid and oid != str(self.stop_order_id):
                    try:
                        self.orders.cancel(oid)
                        log.info(f'F-145: снят висячий дубликат стоп-заявки {oid}')
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------- выход/переворот (по сигналу противоположного кросса) ----------
    def journal_close_manual(self, reason: str, exit_px: float) -> dict | None:
        """F-132: закрыть последнюю open запись журнала (когда позицию закрыл БРОКЕР,
        а не робот: исполнение стоп/TP заявки). Ручной sync журнал-цикла."""
        rec = next((r for r in reversed(self.state.trade_history) if r.get("status") == "open"), None)
        if not rec:
            return None
        from datetime import datetime as _dt
        _now = _dt.now()
        _entry = float(rec.get("entry", 0))
        _qty = int(float(rec.get("qty", 0)))
        _dir = 1 if rec.get("dir") == "L" else -1
        # F-162: комиссия не моделируем (0.90/лот было неверно для акций) — PnL чистый по цене
        _commission = 0.0
        _net = (exit_px - _entry) * _dir * _qty
        close_rec = {
            "date_close": _now.strftime("%Y-%m-%d"),
            "time_close": _now.strftime("%H:%M:%S"),
            "exit": round(float(exit_px), 2),
            "commission": _commission,
            "pnl": round(_net, 2),
            "R": 0.0,
            "reason": reason,
        }
        closed = self.state.journal_close(close_rec)
        if closed:
            self.state.save()
            log.info(f'F-132: журнал закрыт вручную (broker close): exit={exit_px} PnL={round(_net,2)} reason={reason}')
        return closed

    def close_position(self, reason: str = "signal") -> dict:
        if self.state.direction == FLAT:
            return {"event": "close_skip_flat"}
        side = SELL if self.state.direction == LONG else BUY
        po = self.orders.place_market(side, self.state.lots_qty, tag=f"sarema-close-{reason}")
        if self.stop_order_id:
            self.orders.cancel(self.stop_order_id)
        self.state.stop_order_id = None
        if self.state.tp_order_id:                     # F-105: снять TP-лимитку
            try:
                self.orders.cancel(self.state.tp_order_id)
            except Exception:
                pass
            self.state.tp_order_id = None
        # F-127: ГАРАНТИЯ — снять ВСЕ остальные активные заявки робота у брокера
        # (дубли стопов после рестартов, старые лимитки, всё что могло остаться)
        try:
            _n = self.orders.cancel_all_robot_orders()
            if _n:
                log.warning(f'F-127: снято {_n} оставшихся заявок робота')
        except Exception as _ce2:
            log.error(f'F-127 cancel-all err: {_ce2}')
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
