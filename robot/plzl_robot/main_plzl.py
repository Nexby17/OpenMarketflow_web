"""MTLR Robot — spot MTLR@MISX (MOEX TQBR), SAR×EMA cross + pullback strategy.

HTTP API (default port 5094):
  GET  /status        — full state for UI (mode, position, stats, indicators tail)
  GET  /chart         — candles + indicators + trades + position levels
  GET  /params        — current params
  POST /params        — update params (saved to mtlr_config.json, applied live)
  POST /start         — mode=running
  POST /pause         — mode=paused
  POST /stop          — mode=stopped + close position at market
  POST /reset-stats   — journal/realized -> 0 (position untouched)

Execution: market orders via FinamPy gRPC (orders_exec.ArbOrderManager — battle-tested
on stocks LKOH/SBER). Journal prices = REAL fill prices (trade sub / gRPC avg).
Paper mode: fills at bid/ask from ISS marketdata.

Start: python3 main_mtlr.py [--port 5094] [--paper]
"""
import argparse
import json
import logging
import os
import signal as sig_module
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from config_plzl import (MtlrParams, load_params, save_params,
                         TF_TO_ISS, TF_SECONDS)
from candles_source import CandleSource, aggregate_m1, iss_interval_for, needs_aggregation
from strategy_plzl import StrategyEngine, LONG, SHORT
from indicators import ema
from orders_exec import ArbOrderManager, BUY, SELL

log = logging.getLogger("plzl")

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plzl_state.json")
MSK = timezone(timedelta(hours=3))

# ============ globals ============
params: MtlrParams = load_params()
engine = StrategyEngine(params)
src = CandleSource(secid=params.ticker, history_bars=params.history_bars)
mode = {"m": "stopped"}          # stopped|paused|running
_place_fail_streak = 0        # серия фейлов перевыставления TP/SL (антиспам)
_last_place_fail_t = 0.0
_last_place_ok_t = 0.0        # последняя успешная установка TP/SL (дубль-защита)
entry_limit = None              # 🎯 активная entry-лимитка на касании EMA: {id,side,level,qty,placed_t,touch_t}
orders_mgr: ArbOrderManager | None = None
fp = None                        # FinamPy
exec_lock = threading.Lock()     # исполнение — строго последовательно
state_lock = threading.Lock()
events_lock = threading.Lock()   # отдельный лок: logg вызывается внутри state_lock

from orders_exec import grpc_lock  # shared FinamPy lock (not thread-safe)

events: list = []

def logg(msg: str, level=logging.INFO):
    log.log(level, msg)
    with events_lock:
        events.append({"t": datetime.now(MSK).strftime("%H:%M:%S"), "msg": msg, "lvl": str(logging.getLevelName(level))})
        del events[:-200]


# ============ candles ============
def _load_candles(full: bool = False):
    """Загрузить свечи под текущий ТФ (с агрегацией M1→M5/M15/M30).
    src.candles хранит СЫРЬЁ (M1 для агрегируемых ТФ, натив — для остальных);
    агрегация выполняется на выходе."""
    tf = params.timeframe
    iss = iss_interval_for(tf)
    raw = src.fetch_history(iss, _m1_bars_needed(tf)) if full else src.refresh_tail(iss)
    if needs_aggregation(tf):
        from config_plzl import TF_SECONDS
        return aggregate_m1(raw, TF_SECONDS[tf] // 60)
    return raw


def _m1_bars_needed(tf: str) -> int:
    bars = params.history_bars
    if needs_aggregation(tf):
        from config_plzl import TF_SECONDS
        mult = TF_SECONDS[tf] // 60
        return min(bars * mult, 28800)  # ≤20 торговых дней M1
    return bars


# ============ session ============
def _in_session(now=None) -> bool:
    now = now or datetime.now(MSK)
    hm = now.strftime("%H:%M")
    s, e = params.session_start, params.session_end
    if s <= hm <= e:
        return True
    if params.use_evening:
        return params.evening_start <= hm <= params.evening_end
    return False


# ============ execution ============
def _init_orders():
    global orders_mgr, fp
    orders_mgr = ArbOrderManager(dp_url=os.environ.get("DP_URL", "http://localhost:5060"),
                                 account=params.account)
    orders_mgr._stock_account = params.account
    token = os.environ.get("FINAM_API_KEY", "")
    if token:
        try:
            from FinamPy import FinamPy as _FP
            fp = _FP(token)
            orders_mgr.set_finam_py(fp)
            orders_mgr.start_trade_subscription()
            _start_quote_stream()
            logg(f"FinamPy gRPC connected, trade sub on, account={params.account}")
        except Exception as e:
            logg(f"FinamPy connect FAILED: {e} — real orders unavailable", logging.ERROR)
            fp = None
    else:
        logg("FINAM_API_KEY not set — real orders unavailable", logging.ERROR)


def _fill_price(order_id: str, side: str, qty: int, est: float) -> float:
    """Реальная цена исполнения; paper — bid/ask симуляция."""
    if params.paper:
        q = src.last_quote or {}
        if side == BUY:
            return q.get("offer") or est
        return q.get("bid") or est
    if not orders_mgr:
        return 0.0
    time.sleep(0.2)  # fill callback async — дать сработать
    return orders_mgr._get_real_fill_price(order_id, est)


def _market(side: str, qty: int, est_price: float):
    """Возвращает (order_id, fill_price)."""
    if params.paper:
        return f"paper-{int(time.time()*1000)}", 0.0
    if not orders_mgr:
        return None, 0.0
    r = orders_mgr._place_market(params.symbol, side, qty, tag=f"mtlr-{side}")
    if not r:
        return None, 0.0
    return r.get("order_id", ""), 0.0


def execute_events(evs: list):
    """Исполнить события от engine.scan() / on_tick."""
    for ev in evs:
        try:
            if ev["action"] == "close":
                _do_close(ev.get("reason", "manual"))
            elif ev["action"] == "open":
                _do_open(ev["side"], ev["qty"], ev.get("reason", ""))
        except Exception as e:
            logg(f"execute error: {e}", logging.ERROR)


def _do_open(side: int, qty: int, reason: str):
    with exec_lock:
        if engine.position or mode["m"] != "running":
            return
        if not _in_session():
            logg("entry skipped: out of session")
            return
        q = src.last_quote or {}
        est = q.get("offer") if side == LONG else q.get("bid")
        est = est or (engine.candles[-1]["c"] if engine.candles else 0.0)
        # 20.11 HARD CAP: объём входа СТРОГО ≤ max_capital (страховка от любых
        # будущих багов конвертации — инцидент SNGS: 1100 акций ушли как 110 000)
        if params.max_capital > 0 and est > 0:
            lot = getattr(params, "lot_size", 1) or 1
            cap_qty = (int(params.max_capital / est) // lot) * lot
            if qty > cap_qty:
                if cap_qty <= 0:
                    logg(f"🚫 CAP: вход отменён — цена {est:.2f} выше max_capital {params.max_capital:.0f}₽", logging.WARNING)
                    return
                logg(f"⚠️ CAP: вход урезан {qty} → {cap_qty} шт (max_capital {params.max_capital:.0f}₽ @ {est:.2f})", logging.WARNING)
                qty = cap_qty
        bs = BUY if side == LONG else SELL
        oid, _ = _market(bs, qty, est)
        if oid is None and not params.paper:
            logg(f"OPEN FAILED ({reason}): order rejected", logging.ERROR)
            return
        price = _fill_price(oid, bs, qty, est)
        if price <= 0:
            logg(f"OPEN FAILED ({reason}): no fill price", logging.ERROR)
            return
        price = round(price, 2)
        engine.on_open_fill(side, qty, price, time.time(), reason)
        pos = engine.position
        logg(f"OPEN {'LONG' if side==LONG else 'SHORT'} {qty} @ {price:.2f} [{reason}] "
             f"TP={pos['tp']} SL={pos['sl']}")
        # БИРЖЕВЫЕ заявки: TP (жёсткий) + SL (стоп)
        _place_protective_orders(pos)
        _save_state()


def _close_side(side: int) -> str:
    return SELL if side == LONG else BUY


def _tick_round(px: float) -> float:
    """Цена, валидная для сетки инструмента (PLZL тик = 0.1₽).
    Инцидент 17.11 13:16: сырой SAR 1076.2659… → INVALID_ARGUMENT ConditionValuePrice,
    стоп не стоял у брокера. Округляем все цены заявок до тика."""
    step = params.price_step or 0.01
    return round(round(px / step) * step, 4)


def _place_protective_orders(pos: dict):
    """Выставить биржевые TP/SL по включённым галкам. Сохраняет order ids в позицию."""
    if params.paper or not orders_mgr:
        return
    close_side = _close_side(pos["side"])
    # TP — жёсткая лимитка (не переносится никогда)
    if params.tp_enabled and pos.get("tp"):
        oid = orders_mgr.place_tp_limit(params.symbol, close_side, pos["qty"], pos["tp"], tag="tp")
        pos["tp_order_id"] = oid
        if not oid:
            logg("⚠️ TP-заявка НЕ выставлена — orders_loop перевыставит", logging.WARNING)
        else:
            logg(f"📗 TP заявка на бирже: {pos['tp']:.2f} (id={oid})")
    # SL — стоп-заявка (переставляется трейлингом)
    if params.sl_enabled and pos.get("sl"):
        pos["sl"] = _tick_round(pos["sl"])  # сырой SAR не проходит ценовую сетку биржи
        cond = "last_up" if pos["side"] == SHORT else "last_down"
        oid = orders_mgr.place_sl_stop(params.symbol, close_side, pos["qty"], pos["sl"], stop_condition=cond, tag="sl")
        pos["sl_order_id"] = oid
        if not oid:
            logg("⚠️ SL-заявка НЕ выставлена — orders_loop перевыставит", logging.WARNING)
        else:
            logg(f"📕 SL заявка на бирже: {pos['sl']:.2f} (id={oid})")


def _cancel_protective_orders(pos: dict) -> dict:
    """Снять биржевые TP/SL заявки позиции.
    Возвращает {"tp": state, "sl": state}, где state:
      "ok"    — снятие подтверждено (или заявки не было / уже не жива без исполнения)
      "filled"— заявка ИСПОЛНЕНА (avg_price>0)
      None    — не удалось подтвердить (ошибка канала) — заявку считать ЖИВОЙ.
    18.11 (перенос из mtlr): прежде глотал ошибки cancel и «снимал» id — гонка
    с исполнением SL порождала маркет-продажу поверх уже закрытой позиции."""
    res = {"tp": "ok", "sl": "ok"}
    if params.paper or not orders_mgr:
        return res
    for key, name in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
        oid = pos.get(key)
        if not oid:
            continue
        k = key.split("_")[0]
        try:
            sent, not_found = orders_mgr._grpc_cancel(oid)
            if sent:
                logg(f"заявка {name} снята (id={oid})")
                pos[key] = None
                continue
            # отмена не подтверждена — проверить фактический статус заявки
            st = orders_mgr._grpc_get_order_status(oid, orders_mgr._stock_account)
            if st and st.get("avg_price", 0) > 0:
                res[k] = "filled"
                logg(f"заявка {name} ИСПОЛНЕНА (id={oid}, avg={st['avg_price']:.2f}) — не снята", logging.WARNING)
            elif st and st.get("state") in ("filled", "cancelled"):
                logg(f"заявка {name} уже не жива: {st.get('state')} (id={oid})")
                pos[key] = None
            elif st:
                res[k] = None  # жива — отмена не прошла
                logg(f"снятие {name} {oid} НЕ прошло (status={st.get('state')}) — заявка жива", logging.WARNING)
            else:
                res[k] = None  # статус неизвестен (канал) — считать живой
                logg(f"снятие {name} {oid} не подтверждено (канал) — заявка считается живой", logging.WARNING)
        except Exception as e:
            res[k] = None
            logg(f"снятие {name} {oid}: {e}", logging.WARNING)
    return res


def _order_filled_price(order_id: str, fallback: float) -> float:
    """Реальная средняя цена исполнения заявки."""
    if not orders_mgr:
        return fallback
    st = orders_mgr._grpc_get_order_status(order_id, orders_mgr._stock_account)
    if st and st.get("avg_price", 0) > 0:
        return st["avg_price"]
    real = orders_mgr._wait_fill_price(order_id, timeout=1.0)
    return real if real and real > 0 else fallback


def _do_close(reason: str):
    with exec_lock:
        pos = engine.position
        if not pos:
            return
        # 0) ГОHКА: TP/SL могли исполниться на бирже мгновение назад — оформить по заявке, не маркетом
        if not params.paper and orders_mgr:
            sm = _orders_status_map()
            tp_id, sl_id = pos.get("tp_order_id"), pos.get("sl_order_id")
            if sm is None:
                # канал мёртв: нельзя ни проверить заявки, ни снять, ни маркетить —
                # отложим close (retry на след. тике), биржевые TP/SL продолжают защищать
                logg(f"CLOSE [{reason}] ОТЛОЖЕН: статус заявок недоступен (канал) — retry", logging.WARNING)
                return
            if tp_id and sm.get(tp_id) in _FILLED:
                price = round(_order_filled_price(tp_id, pos["tp"]), 2)
                _cancel_protective_orders(pos)
                trade = engine.on_close_fill(price, time.time(), "tp")
                logg(f"📗 CLOSE [{reason}] отменён — TP уже исполнен @ {price:.2f}"
                     + (f" PnL={trade['pnl']:+.2f}₽" if trade else ""))
                _save_state()
                return
            if sl_id and sm.get(sl_id) in _FILLED:
                price = round(_order_filled_price(sl_id, pos["sl"]), 2)
                _cancel_protective_orders(pos)
                trade = engine.on_close_fill(price, time.time(), "sl")
                logg(f"📕 CLOSE [{reason}] отменён — SL уже исполнен @ {price:.2f}"
                     + (f" PnL={trade['pnl']:+.2f}₽" if trade else ""))
                _save_state()
                return
        # 1) снять ВСЕ заявки по позиции (TP/SL на бирже + любые остатки)
        cancels = _cancel_protective_orders(pos)
        if not params.paper and orders_mgr:
            try:
                n = orders_mgr.cancel_open_orders([params.symbol])
                if n:
                    logg(f"снято остаточных заявок по {params.ticker}: {n}")
            except Exception as e:
                logg(f"cancel_open_orders: {e}", logging.WARNING)
            # 1.5) ГОHКА SL/TP (18.11, перенос из mtlr): отмена не подтверждена — маркет вслепую НЕ ставить.
            # Если заявка уже исполнена — оформить закрытие по ней; если жива/неизвестна —
            # отложить close (retry на след. тике): биржевые TP/SL продолжают защищать позицию.
            for k, nm, oid, fb in (("tp", "TP", tp_id, pos.get("tp")),
                                   ("sl", "SL", sl_id, pos.get("sl"))):
                if cancels.get(k) == "filled":
                    price = round(_order_filled_price(oid, fb or 0.0), 2)
                    if price <= 0:
                        price = round(fb or 0.0, 2)
                    _cancel_protective_orders(pos)
                    trade = engine.on_close_fill(price, time.time(), k)
                    logg(f"📕 CLOSE [{reason}]: {nm} уже исполнен @ {price:.2f} — маркет отменён"
                         + (f" PnL={trade['pnl']:+.2f}₽" if trade else ""))
                    _save_state()
                    return
            if cancels.get("sl") is None or cancels.get("tp") is None:
                logg(f"CLOSE [{reason}] ОТЛОЖЕН: отмена TP/SL не подтверждена — маркет не ставлю, "
                     f"retry на след. тике (биржевые заявки защищают позицию)", logging.WARNING)
                return
        # 2) закрыть позицию по рынку
        q = src.last_quote or {}
        bs = _close_side(pos["side"])
        est = q.get("bid") if pos["side"] == LONG else q.get("offer")
        est = est or (engine.candles[-1]["c"] if engine.candles else 0.0)
        oid, _ = _market(bs, pos["qty"], est)
        if oid is None and not params.paper:
            logg(f"CLOSE FAILED ({reason}): order rejected — will retry next tick", logging.ERROR)
            return
        price = _fill_price(oid, bs, pos["qty"], est)
        if price <= 0:
            logg(f"CLOSE FAILED ({reason}): no fill price — retry next tick", logging.ERROR)
            return
        price = round(price, 2)
        trade = engine.on_close_fill(price, time.time(), reason)
        if trade:
            logg(f"CLOSE @ {price:.2f} [{reason}] PnL={trade['pnl']:+.2f}₽ "
                 f"({trade['exit_time']-trade['entry_time']:.0f}s)")
        _save_state()


# ============ state persist ============
def _save_state():
    global entry_limit
    with state_lock:
        st = engine.state_dict()
        st["entry_limit"] = dict(entry_limit) if entry_limit else None  # 16.11: лимитка переживает рестарт
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _load_state():
    global entry_limit
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            engine.load_state(st)
            el = st.get("entry_limit")
            if el and not engine.position:
                entry_limit = el  # восстановлена; сверка при старте её узнаёт,
                # а entry_loop долбит статус: заявка исчезла (EOD) — переставит сам
            logg("state restored"
                 + (f" (+entry-limit {el.get('side')} {el.get('qty')} @ {el.get('level')})" if el else ""))
        except Exception as e:
            logg(f"state load failed: {e}", logging.ERROR)


# ============ loops ============
# ============ real-time quotes (Finam gRPC stream, мс) ============
_QUOTE_SYMBOLS_OK = {"PLZL"}  # страхуемся от чужих тиков
_LIVE_BAR = {}  # живой бар текущего часа (тик-источник, ISS его не отдаёт)
_LIVE_BAR_SEEDED = 0  # bt часа, для которого уже сеяли из M1 (реальные o/h/l/v)
_PENDING_BARS = {}  # t -> закрытый live-бар прошлого часа (ISS H1 лагает — держим до его свечи)
_GAP_FILL_TS = 0.0  # анти-спам досевки дыр (раз в 60с)
_FLIP_CLOSE_TS = 0.0  # время последнего флип-закрытия: 120с без pb-лимиток после него
_LIVE_V_REFRESH_TS = 0.0  # обновление объёма живого бара из M1 (раз в 60с)


def _refresh_live_volume():
    """Раз в минуту: объём текущего живого бара — реальный биржевый из M1
    (тики не несут объём). Заодно добираем h/l из M1 (безопасный max/min)."""
    global _LIVE_V_REFRESH_TS
    if not _LIVE_BAR or time.time() - _LIVE_V_REFRESH_TS < 60:
        return
    _LIVE_V_REFRESH_TS = time.time()
    m1 = _m1_hour_bar(_LIVE_BAR["t"])
    if m1 and _LIVE_BAR:
        _LIVE_BAR["v"] = m1["v"]  # объём биржи — авторитетный
        _LIVE_BAR["h"] = max(_LIVE_BAR["h"], m1["h"])
        _LIVE_BAR["l"] = min(_LIVE_BAR["l"], m1["l"])


def _fill_gap_bars():
    """Дыры между последней ISS-свечой и текущим часом (рестарт/лаг ISS):
    закрытые часы досеиваем из M1 ISS — на графике не должно быть пропусков."""
    global _GAP_FILL_TS
    if not engine.candles or time.time() - _GAP_FILL_TS < 60:
        return
    _GAP_FILL_TS = time.time()
    tf = TF_SECONDS.get(params.timeframe, 300)
    cur_bt = int(time.time() // tf) * tf
    last_t = engine.candles[-1]["t"]
    hole = next((t for t in range(last_t + tf, cur_bt, tf) if t not in _PENDING_BARS), None)
    if hole is None:
        return
    try:
        frm = datetime.fromtimestamp(last_t, MSK).strftime("%Y-%m-%d")
        rows = _m1_day_rows(frm)
        for row in rows:
            t = int(datetime.strptime(row[6], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp())
            bt = t // tf * tf
            if bt <= last_t or bt >= cur_bt or bt in _PENDING_BARS:
                continue
            b = _PENDING_BARS.setdefault(bt, {"t": bt, "o": float(row[0]), "h": float(row[2]),
                                               "l": float(row[3]), "c": float(row[1]), "v": 0})
            b["h"] = max(b["h"], float(row[2]))
            b["l"] = min(b["l"], float(row[3]))
            b["c"] = float(row[1])
            b["v"] += int(row[5])
        logg(f"🕳 Досеяны пропущенные часы из M1: {sorted(t for t in _PENDING_BARS if t > last_t)}")
    except Exception as e:
        logg(f"fill gap bars: {str(e)[:80]}", logging.WARNING)


def _m1_day_rows(frm: str):
    """Все M1-свечи дня с ISS (с пагинацией — ISS отдаёт по 500 строк!)."""
    import requests as _rq
    out = []
    start = 0
    while start < 60000:
        r = _rq.get(
            f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
            f"securities/{params.ticker}/candles.json",
            params={"iss.meta": "off", "interval": 1, "from": frm, "start": start},
            timeout=8)
        data = r.json().get("candles", {}).get("data", []) or []
        if not data:
            break
        out.extend(data)
        if len(data) < 500:
            break
        start += len(data)
        time.sleep(0.12)
    return out


def _m1_hour_bar(bt: int):
    """Бар часа bt из M1 ISS: настоящие o/h/l/v. None при ошибке/нет данных."""
    try:
        tf = TF_SECONDS.get(params.timeframe, 300)
        frm = datetime.fromtimestamp(bt, MSK).strftime("%Y-%m-%d")
        rows = _m1_day_rows(frm)
        h, l, v = None, None, 0
        o = c = None
        for row in rows:
            t = int(datetime.strptime(row[6], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp())
            if t < bt or t >= bt + tf:
                continue
            if o is None:
                o = float(row[0])
            h = float(row[2]) if h is None else max(h, float(row[2]))
            l = float(row[3]) if l is None else min(l, float(row[3]))
            c = float(row[1])
            v += int(row[5])
        if o is None:
            return None
        return {"o": o, "h": h, "l": l, "c": c, "v": v}
    except Exception as e:
        logg(f"m1 hour bar: {str(e)[:80]}", logging.WARNING)
        return None


def _seed_live_bar(bt: int):
    """Засеять живой бар из M1-свечей ISS текущего часа: настоящий open, полный
    high/low (тени!), объём. Тики поверх дают мс-свежесть close/h/l."""
    global _LIVE_BAR, _LIVE_BAR_SEEDED
    if _LIVE_BAR_SEEDED == bt:
        return
    try:
        frm = datetime.fromtimestamp(bt, MSK).strftime("%Y-%m-%d")
        rows = _m1_day_rows(frm)
        m1 = []
        for row in rows:
            t = int(datetime.strptime(row[6], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp())
            if t >= bt:
                m1.append({"t": t, "o": float(row[0]), "c": float(row[1]),
                           "h": float(row[2]), "l": float(row[3]), "v": int(row[5])})
        _LIVE_BAR_SEEDED = bt
        if not m1:
            return
        h = max(x["h"] for x in m1)
        l = min(x["l"] for x in m1)
        v = sum(x["v"] for x in m1)
        if _LIVE_BAR and _LIVE_BAR.get("t") == bt:
            # тики уже жили в этом часе — мержим: экстремумы шире, open настоящий
            _LIVE_BAR["o"] = m1[0]["o"]
            _LIVE_BAR["h"] = max(_LIVE_BAR["h"], h)
            _LIVE_BAR["l"] = min(_LIVE_BAR["l"], l)
            _LIVE_BAR["v"] = max(_LIVE_BAR["v"], v)
        else:
            _LIVE_BAR = {"t": bt, "o": m1[0]["o"], "h": h, "l": l,
                         "c": m1[-1]["c"], "v": v}
    except Exception as e:
        logg(f"seed live bar: {str(e)[:80]}", logging.WARNING)


def _dec(v):
    """Decimal-поле gRPC -> float (0.0 если пусто)."""
    try:
        return float(getattr(v, "value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _apply_live_tick():
    """Живой бар текущего часа (график реал-тайм). ISS отдаёт H1 только ЗАКРЫТЫЕ —
    текущий час держим сами из тиков; на закрытии ISS подменит точной свечой.
    Сигналы НЕ трогаем: они по закрытым ISS-барам (точные o/h/l/c)."""
    q = src.last_quote or {}
    last = q.get("last") or 0.0
    if last <= 0:
        return
    tf = TF_SECONDS.get(params.timeframe, 300)
    now = q.get("ts") or time.time()
    bt = int(now // tf) * tf
    global _LIVE_BAR
    if not _LIVE_BAR or _LIVE_BAR["t"] != bt:
        if _LIVE_BAR and _LIVE_BAR["t"] < bt:
            # час закрылся: финализируем бар — M1 даёт настоящие o/h/v и
            # ОФИЦИАЛЬНЫЙ close (последняя минута часа = close биржи);
            # тиковый close оставался только пока M1 не пришёл. Бары = бэктест-модель,
            # пограничные сигналы больше не решаются тиковым шумом в копейки
            fin = dict(_LIVE_BAR)
            m1 = _m1_hour_bar(fin["t"])
            if m1:
                fin["o"] = m1["o"]
                fin["h"] = max(fin["h"], m1["h"])
                fin["l"] = min(fin["l"], m1["l"])
                fin["v"] = max(fin["v"], m1["v"])
                fin["c"] = m1["c"]
            _PENDING_BARS[fin["t"]] = fin
        _seed_live_bar(bt)  # настоящий open/тени/объём часа из M1 ISS
        if not _LIVE_BAR or _LIVE_BAR["t"] != bt:
            _LIVE_BAR = {"t": bt, "o": last, "h": last, "l": last, "c": last, "v": 0}
        else:
            # тик поверх засеянного бара
            _LIVE_BAR["h"] = max(_LIVE_BAR["h"], last)
            _LIVE_BAR["l"] = min(_LIVE_BAR["l"], last)
            _LIVE_BAR["c"] = last
            _LIVE_BAR["v"] += 0
    else:
        _LIVE_BAR["h"] = max(_LIVE_BAR["h"], last)
        _LIVE_BAR["l"] = min(_LIVE_BAR["l"], last)
        _LIVE_BAR["c"] = last  # объём не трогаем — его обновляет _refresh_live_volume из M1


def _check_live_flip():
    """Внутричасовой кросс SAR×EMA (заказ Самурая 16.11.2026): закрыть позицию
    по рынку в МОМЕНТ кросса, не дожидаясь закрытия бара. Индикаторы — на
    закрытых барах + живой бар; смена знака осциллятора против позиции → close.
    Гонку с биржевым SL разруливает _do_close (шаг 0): если стоп уже исполнен —
    оформится по заявке. Дончиан-позиции НЕ закрываются флипом —
    у них выход только люстра (как в бэктесте)."""
    try:
        if mode["m"] != "running" or not engine.position:
            return
        if engine.position.get("src") == "donch":
            return
        if not _LIVE_BAR or not _LIVE_BAR.get("t"):
            return
        # вырожденный бар (один тик, h==l) — пропускаем, ждём развития бара
        if _LIVE_BAR["h"] == _LIVE_BAR["l"]:
            return
        new_side = engine.live_flip_side(_LIVE_BAR)
        if new_side is None:
            return
        if new_side != engine.position["side"]:
            q = src.last_quote or {}
            logg(f"⚡ LIVE-ФЛИП osc→{'LONG' if new_side == LONG else 'SHORT'} "
                 f"(last={q.get('last')}) — закрытие в момент кросса")
            global _FLIP_CLOSE_TS
            _FLIP_CLOSE_TS = time.time()  # кулдаун pb-лимиток после флипа (анти-пила)
            _do_close("flip")
    except Exception as e:
        logg(f"live_flip: {e}", logging.ERROR)


def _on_quote_event(event):
    """Тик из стрима Finam SubscribeQuote — миллисекундная свежесть."""
    try:
        for qe in event.quote:  # repeated: батч тиков
            base = (qe.symbol or "").split("@")[0]
            if _QUOTE_SYMBOLS_OK and base not in _QUOTE_SYMBOLS_OK:
                continue
            last = _dec(qe.last)
            if last <= 0:
                continue
            src.last_quote = {
                "last": last, "bid": _dec(qe.bid), "offer": _dec(qe.ask),
                "updated": "", "ts": time.time(), "source": "stream",
            }
            engine.on_tick(last)
            _apply_live_tick()
            _check_live_flip()
    except Exception as e:
        logg(f"quote event: {e}", logging.ERROR)


def _start_quote_stream():
    if not fp:
        return
    fp.on_quote.subscribe(_on_quote_event)
    threading.Thread(target=fp.subscribe_quote_thread, args=([params.symbol],),
                     daemon=True, name="finam-quote-stream").start()
    logg(f"📡 Finam quote stream: {params.symbol} (реал-тайм)")


def _poll_last_quote():
    """Фоллбэк: LastQuote через gRPC (брокерские данные), когда стрим молчит."""
    try:
        from FinamPy.grpc.marketdata_service_pb2 import QuoteRequest
        with grpc_lock:
            fp.auth()
            resp, _ = fp.marketdata_stub.LastQuote.with_call(
                request=QuoteRequest(symbol=params.symbol),
                timeout=5, metadata=(fp.metadata,))
        qe = resp.quote
        last = _dec(qe.last)
        if last <= 0:
            return
        src.last_quote = {
            "last": last, "bid": _dec(qe.bid), "offer": _dec(qe.ask),
            "updated": "", "ts": time.time(), "source": "poll",
        }
        engine.on_tick(last)
        _apply_live_tick()
        _check_live_flip()
    except Exception as e:
        logg(f"last_quote poll: {str(e)[:80]}", logging.WARNING)


# ============ FinamPy watchdog (инцидент 21.11: канал умер в 04:18, ожил в 08:16) ============
_last_fp_reconnect = 0.0


def _reconnect_finampy(reason: str = ""):
    """Пересоздать FinamPy-канал + подписки (trade sub, quote stream).
    Кулдаун 120с — не чаще раза в 2 минуты."""
    global fp, _last_fp_reconnect
    now = time.time()
    if now - _last_fp_reconnect < 120:
        return
    _last_fp_reconnect = now
    logg(f"🔁 WATCHDOG: реконнект FinamPy ({reason})", logging.WARNING)
    try:
        try:
            orders_mgr.stop_trade_subscription()
        except Exception:
            pass
        if fp is not None:
            try:
                fp.close_channel()
            except Exception:
                pass
        fp = None
        time.sleep(1)
        from FinamPy import FinamPy as _FP
        fp = _FP(os.environ.get("FINAM_API_KEY", ""))
        orders_mgr.set_finam_py(fp)
        orders_mgr.start_trade_subscription()
        _start_quote_stream()
        logg("✅ WATCHDOG: FinamPy пересоздан, подписки восстановлены")
    except Exception as e:
        logg(f"❌ WATCHDOG: реконнект не удался: {e}", logging.ERROR)
        fp = None


_STALE_OPEN_SEC = 180  # маркет-вход по бару, закрытому дольше, — просрочен


def _drop_stale_opens(evs: list) -> list:
    """Гасить OPEN-события по бару, закрытому >_STALE_OPEN_SEC назад
    (catch-up после провала данных: 21.11 кросс 978 → маркет-вход 963.4).
    CLOSE-события (flip/SL) исполняются всегда — позицию бросать нельзя."""
    from config_plzl import TF_SECONDS
    tf = TF_SECONDS.get(params.timeframe, 1800)
    if not engine.last_bar_t:
        return evs  # инициализация (scan ещё не отмечал бары) — не блокируем
    age = time.time() - (engine.last_bar_t + tf)
    if age <= _STALE_OPEN_SEC:
        return evs
    kept = []
    for e in evs:
        if e.get("action") == "open":
            logg(f"⏭ STALE-OPEN: вход {e.get('reason')} погашен — бар закрыт "
                 f"{age/60:.0f} мин назад, сигнал просрочен", logging.WARNING)
        else:
            kept.append(e)
    return kept


def quote_loop():
    """Watchdog цены: стрим — основной источник; тишина >10с → LastQuote-поллинг.
    ISS больше НЕ источник цены (лагает против брокера).
    Backoff: при ошибках полла (rate-limit) — пауза растёт до 30с."""
    err_streak = 0
    while True:
        try:
            q = src.last_quote or {}
            fresh = q.get("ts") and (time.time() - q["ts"] <= 10)
            if not fresh:
                ts_before = (src.last_quote or {}).get("ts")
                if fp:
                    _poll_last_quote()
                    ok = (src.last_quote or {}).get("ts", 0) != (ts_before or 0)
                else:
                    q2 = src.fetch_quote()
                    ok = bool(q2.get("last"))
                    if ok:
                        engine.on_tick(q2["last"])
                err_streak = 0 if ok else err_streak + 1
            else:
                err_streak = 0
        except Exception as e:
            err_streak += 1
            logg(f"quote_loop: {e}", logging.ERROR)
        # WATCHDOG (инцидент 21.11 04:18–08:16): канал FinamPy умер молча —
        # котировки мертвы >120с в торговые часы → пересоздать канал+подписки
        if fp:
            _hm = datetime.now(MSK).strftime("%H:%M")
            _mkt_open = ("09:45" <= _hm <= "18:55"
                         or (params.use_evening and params.evening_start <= _hm <= params.evening_end))
            if _mkt_open:
                _stale_q = time.time() - ((src.last_quote or {}).get("ts") or 0)
                if _stale_q > 120:
                    _reconnect_finampy(f"котировки мертвы {_stale_q:.0f}с")
        time.sleep(min(2 * (1 + err_streak * 2), 30))


# статусы исполнена/активна (по имени из gRPC OrderStatus)
_FILLED = {"ORDER_STATUS_FILLED", "ORDER_STATUS_EXECUTED", "ORDER_STATUS_SL_EXECUTED", "ORDER_STATUS_TP_EXECUTED"}
_ACTIVE = {"ORDER_STATUS_NEW", "ORDER_STATUS_PARTIALLY_FILLED", "ORDER_STATUS_PENDING_NEW", "ORDER_STATUS_WATCHING",
           "ORDER_STATUS_WAIT", "ORDER_STATUS_LINK_WAIT", "ORDER_STATUS_FORWARDING", "ORDER_STATUS_SL_FORWARDING",
           "ORDER_STATUS_TP_FORWARDING", "ORDER_STATUS_SL_GUARD_TIME", "ORDER_STATUS_TP_GUARD_TIME",
           "ORDER_STATUS_TP_CORRECTION", "ORDER_STATUS_TP_CORR_GUARD_TIME", "ORDER_STATUS_PENDING_CANCEL"}


def _orders_status_map() -> dict | None:
    """{order_id: status_name} по всем заявкам MTLR за сегодня.
    None = не удалось получить (ошибка канала) — монитор ждёт.
    {} = получено успешно, заявок нет (брокер снял EOD) — перевыставлять."""
    if not orders_mgr:
        return None
    try:
        orders = orders_mgr.get_symbol_orders(params.symbol)
        if orders is None:
            return None
        return {o["order_id"]: o["status"] for o in orders}
    except Exception:
        return None


def _placement_blocked() -> bool:
    """Анти-спам перевыставления в 5с-цикле:
    - после 3+ фейлов подряд — пауза 60с (14.11: 950 попыток подряд на мёртвом канале)
    - после успешной установки — 60с не трогаем (GetOrders лагает, дубль-защита, 13.11)"""
    now = time.time()
    if _place_fail_streak >= 3 and now - _last_place_fail_t < 60:
        return True
    if _last_place_ok_t and now - _last_place_ok_t < 60:
        return True
    return False


def _note_place(ok: bool):
    global _place_fail_streak, _last_place_fail_t, _last_place_ok_t
    if ok:
        _place_fail_streak = 0
        _last_place_ok_t = time.time()
    else:
        _place_fail_streak += 1
        _last_place_fail_t = time.time()


# ============ 🎯 ВХОД ПО КАСАНИЮ EMA (лимитка у брокера, заказ 14.11) ============

def _cancel_entry_limit(reason: str):
    """Снять entry-лимитку. Частичный филл оформляем в позицию, остаток снимаем."""
    global entry_limit
    el = entry_limit
    if not el:
        return
    entry_limit = None
    filled_qty = 0
    if orders_mgr and el.get("id"):
        try:
            orders = orders_mgr.get_symbol_orders(params.symbol)
            if orders is not None:
                o = next((o for o in orders if str(o["order_id"]) == str(el["id"])), None)
                if o:
                    filled_qty = min(int(o.get("executed_qty") or 0), el["qty"])
        except Exception:
            pass
        try:
            if filled_qty < el["qty"]:
                orders_mgr._grpc_cancel(el["id"])
        except Exception as e:
            logg(f"entry-limit cancel: {e}", logging.WARNING)
    logg(f"🎯 ENTRY-LIMIT снята ({reason})" + (f" id={el.get('id')}" if el.get("id") else ""))
    _save_state()
    if filled_qty > 0:
        vwap = orders_mgr._fill_prices.get(el["id"]) if orders_mgr else None
        _entry_limit_filled(filled_qty, vwap or el["level"], el)


def _entry_limit_filled(filled_qty: int, price: float, el: dict = None):
    """Лимитка исполнена биржей на касании EMA — оформить позицию + TP/SL."""
    global entry_limit
    el = el or entry_limit
    if el is None:
        return
    if entry_limit is not None and el.get("id") == entry_limit.get("id"):
        entry_limit = None
    side = el["side"]
    qty = int(filled_qty or el["qty"])
    price = round(price or el["level"], 2)
    with exec_lock:
        if engine.position:
            return
        engine.on_open_fill(side, qty, price, time.time(), "pb-limit")
        pos = engine.position
        logg(f"OPEN {'LONG' if side == LONG else 'SHORT'} {qty} @ {price:.2f} [pb-limit — касание EMA] "
             f"TP={pos['tp']} SL={pos['sl']}")
        _place_protective_orders(pos)
        _save_state()


def _manage_entry_limit():
    """Постановка/перестановка/снятие entry-лимитки (вызывается из bar_loop ~15с).
    Уровень = EMA закрытого бара; живёт, пока жив сигнал (EMA наклон + импульс)."""
    global entry_limit
    if params.paper or not orders_mgr or not params.entry_by_limit:
        return
    if mode["m"] != "running" or engine.position:
        if entry_limit:
            _cancel_entry_limit("позиция открыта" if engine.position else f"mode={mode['m']}")
        return
    if not _in_session():
        if entry_limit:
            _cancel_entry_limit("вне сессии")
        return
    # кулдаун после флип-закрытия (инцидент MTLR 17.11: серия филл→флип→филл)
    if time.time() - _FLIP_CLOSE_TS < 120:
        if entry_limit:
            _cancel_entry_limit("кулдаун после флипа")
        return
    sig = engine.entry_limit_signal()
    if not sig:
        if entry_limit:
            _cancel_entry_limit("сигнал умер")
        return
    side, level, qty = sig["side"], sig["level"], sig["qty"]
    # Анти-маркет (инцидент MTLR 17.11 11:07): лимитка только «в ожидание» —
    # SELL строго НАД ценой, BUY строго ПОД; котировка свежая ≤30с
    q = src.last_quote or {}
    last = q.get("last") or q.get("bid") or q.get("offer") or 0.0
    if last <= 0 or time.time() - (q.get("ts") or 0) > 30:
        if entry_limit:
            _cancel_entry_limit("котировка протухла/нет — не ставим")
        return
    if side == LONG and last <= level:
        if entry_limit:
            _cancel_entry_limit(f"цена {last:.2f} уже у/под EMA {level:.2f} — сигнал мёртв")
        return
    if side == SHORT and last >= level:
        if entry_limit:
            _cancel_entry_limit(f"цена {last:.2f} уже у/над EMA {level:.2f} — сигнал мёртв")
        return
    level = _tick_round(level)  # сетка PLZL 0.1₽: 1088.55 невалиден
    # прокол-фильтр: рынок глубже уровня на depth×ATR — обвал, не откат
    a = engine.atr_v[-1] if engine.atr_v else None
    zone = params.pb_depth_atr * (a or 0)
    if side == LONG and q.get("bid") and q["bid"] < level - zone:
        if entry_limit:
            _cancel_entry_limit(f"прокол вниз: bid {q['bid']:.2f} < {level - zone:.2f}")
        return
    if side == SHORT and q.get("offer") and q["offer"] > level + zone:
        if entry_limit:
            _cancel_entry_limit(f"прокол вверх: offer {q['offer']:.2f} > {level + zone:.2f}")
        return
    if entry_limit:
        if entry_limit["side"] == side and abs(entry_limit["level"] - level) < params.price_step:
            return  # уровень не изменился
        _cancel_entry_limit("перестановка уровня EMA" if entry_limit["side"] == side else "смена стороны")
    bs = BUY if side == LONG else SELL
    r = orders_mgr._grpc_place_order(params.symbol, bs, qty, level, "limit")
    oid = r.get("order_id") if r else None
    if oid:
        entry_limit = {"id": oid, "side": side, "level": level, "qty": qty,
                       "placed_t": time.time(), "touch_t": 0.0}
        logg(f"🎯 ENTRY-LIMIT выставлена: {'BUY' if side == LONG else 'SELL'} {qty} @ {level:.2f} (касание EMA)")
        _save_state()
    else:
        logg("🎯 ENTRY-LIMIT FAILED: биржа отклонила заявку", logging.ERROR)


def entry_loop():
    """Тик-контроль (1с): биржа исполняет лимитку на касании; если касание прошло,
    а заявки нет/умерла — резервный маркет; частичные филлы оформляем."""
    global entry_limit
    last_poll = 0.0
    while True:
        try:
            el = entry_limit
            if el and not params.paper and orders_mgr and mode["m"] == "running" and not engine.position:
                q = src.last_quote or {}
                now = time.time()
                side, level, qty = el["side"], el["level"], el["qty"]
                ask, bid = q.get("offer") or 0.0, q.get("bid") or 0.0
                # касание тиком: buy-limit исполняется при ask<=level, sell-limit при bid>=level
                touch = (side == LONG and ask > 0 and ask <= level) or \
                        (side == SHORT and bid > 0 and bid >= level)
                if touch and not el.get("touch_t"):
                    el["touch_t"] = now
                    logg(f"👆 касание EMA {level:.2f} (ask={ask:.2f} bid={bid:.2f}) — ждём исполнения биржей")
                poll_due = (now - last_poll) >= (2.0 if touch else 10.0)
                if poll_due:
                    last_poll = now
                    orders = orders_mgr.get_symbol_orders(params.symbol)
                    if orders is not None:
                        o = next((o for o in orders if str(o["order_id"]) == str(el["id"])), None)
                        if o:
                            ex = int(o.get("executed_qty") or 0)
                            active = o["status"] in _ACTIVE
                            if o["status"] in _FILLED or ex >= qty:
                                vwap = orders_mgr._fill_prices.get(el["id"])
                                _entry_limit_filled(qty, vwap or level)
                            elif ex > 0 and not active:
                                vwap = orders_mgr._fill_prices.get(el["id"])
                                _entry_limit_filled(min(ex, qty), vwap or level)
                            elif not active and not ex and touch:
                                logg("⚡ касание есть, лимитка неактивна — резервный маркет", logging.WARNING)
                                entry_limit = None
                                _do_open(side, qty, "pb-touch")
                            elif not active:
                                # сверка с брокером (заказ Самурая 16.11): заявка снята на
                                # бирже без касания — память чистим, bar_loop переставит
                                logg(f"⚠️ лимитка снята на бирже без касания "
                                     f"(status={o['status']}) — будет переставлена", logging.WARNING)
                                entry_limit = None
                        else:
                            if touch:
                                logg("⚡ касание есть, лимитка исчезла — резервный маркет", logging.WARNING)
                                entry_limit = None
                                _do_open(side, qty, "pb-touch")
                            else:
                                entry_limit = None  # снята без касания — bar_loop переставит
            elif el and (params.paper or not orders_mgr):
                entry_limit = None
        except Exception as e:
            logg(f"entry_loop: {e}", logging.ERROR)
        time.sleep(1.0)


def _sl_target(pos):
    """Целевой SL (заказ Самурая 16.11.2026): sl_unit=sar → стоп следует за SAR
    последнего закрытого бара; активный трейлинг — берём БОЛЕЕ ЖЁСТКИЙ из двух
    (для лонга выше, для шорта ниже). None — перестановка не нужна."""
    cand = None
    if params.sl_enabled:
        if params.sl_unit == "sar":
            sar = engine.sar_v[-1] if engine.sar_v else None
            if sar is not None:
                cand = _tick_round(sar)
        trail = engine.trail_stop_price()
        if trail:
            t = _tick_round(trail)
            cand = t if cand is None else (max(cand, t) if pos["side"] == 1 else min(cand, t))
    return cand


def _monitor_orders():
    """Сердце биржевой защиты (каждые 5с при открытой позиции):
    1) TP/SL исполнены на бирже → оформить закрытие, снять вторую заявку
    2) заявки сняты брокером (срок жизни 1 день) → перевыставить
    3) трейлинг подтянулся → переставить SL (TP не трогаем — он жёсткий)"""
    if params.paper or not orders_mgr:
        return
    pos = engine.position
    if not pos:
        return
    sm = _orders_status_map()
    if sm is None:  # ошибка канала — не предпринимаем резких действий, ждём следующего цикла
        return
    if engine.position is not pos:
        # 19.11: позиция закрылась/заменилась, пока шли статусы — перевыставлять нечего.
        # (Инцидент 19.11 на MTLR: голый перевыставленный стоп → шорт 908 при 456 у робота.)
        return

    def st(oid):
        return sm.get(oid, "UNKNOWN")

    # --- 1) исполнение TP / SL на бирже ---
    tp_id = pos.get("tp_order_id")
    sl_id = pos.get("sl_order_id")
    if tp_id and st(tp_id) in _FILLED:
        price = round(_order_filled_price(tp_id, pos["tp"]), 2)
        _cancel_protective_orders(pos)
        trade = engine.on_close_fill(price, time.time(), "tp")
        logg(f"📗 TP ИСПОЛНЕН на бирже @ {price:.2f}" + (f" PnL={trade['pnl']:+.2f}₽" if trade else ""))
        _save_state()
        return
    if sl_id and st(sl_id) in _FILLED:
        price = round(_order_filled_price(sl_id, pos["sl"]), 2)
        _cancel_protective_orders(pos)
        trade = engine.on_close_fill(price, time.time(), "sl")
        logg(f"📕 SL ИСПОЛНЕН на бирже @ {price:.2f}" + (f" PnL={trade['pnl']:+.2f}₽" if trade else ""))
        _save_state()
        return

    # --- 2) перевыставление снятых заявок (брокер снимает EOD) ---
    if not _placement_blocked():
        if params.tp_enabled and pos.get("tp") and (not tp_id or st(tp_id) not in _ACTIVE):
            oid = orders_mgr.place_tp_limit(params.symbol, _close_side(pos["side"]), pos["qty"], pos["tp"], tag="tp")
            pos["tp_order_id"] = oid
            _note_place(bool(oid))
            if oid:
                logg(f"♻️ TP перевыставлен @ {pos['tp']:.2f} (брокер снимал) id={oid}")
        if params.sl_enabled and pos.get("sl") and (not sl_id or st(sl_id) not in _ACTIVE):
            pos["sl"] = _tick_round(pos["sl"])  # старый state мог хранить сырой float
            cond = "last_up" if pos["side"] == SHORT else "last_down"
            oid = orders_mgr.place_sl_stop(params.symbol, _close_side(pos["side"]), pos["qty"], pos["sl"], stop_condition=cond, tag="sl")
            pos["sl_order_id"] = oid
            _note_place(bool(oid))
            if oid:
                logg(f"♻️ SL перевыставлен @ {pos['sl']:.2f} (брокер снимал) id={oid}")
    if (params.tp_enabled and pos.get("tp") and pos.get("tp_order_id") != tp_id) or \
       (params.sl_enabled and pos.get("sl") and pos.get("sl_order_id") != sl_id):
        _save_state()  # id заявок менялись — сохранить, иначе рестарт породит дубли

    # --- 3) перестановка SL: SAR-follow (sl_unit=sar) и/или трейлинг — более жёсткий ---
    cand = _sl_target(pos)
    if cand and pos.get("sl") is None:
        # 18.11 (перенос из mtlr): SL ещё не выставлен (SAR был не с той стороны при входе)
        # — поставить, как только cand окажется с правильной стороны цены
        q = src.last_quote or {}
        last = q.get("last") or q.get("bid") or q.get("offer")
        side_ok = last and ((pos["side"] == 1 and cand < last) or (pos["side"] == -1 and cand > last))
        if side_ok:
            new_sl = _tick_round(cand)
            cond = "last_up" if pos["side"] == -1 else "last_down"
            oid = orders_mgr.place_sl_stop(params.symbol, _close_side(pos["side"]), pos["qty"], new_sl, stop_condition=cond, tag="sl")
            if oid:
                pos["sl"] = new_sl
                pos["sl_order_id"] = oid
                logg(f"📕 SL выставлен (не было при входе) → {new_sl:.2f} (id={oid})")
                _save_state()
    if cand and pos.get("sl"):
        cand = round(cand, 2)
        step = 0.10  # мин. шаг перестановки (анти-спам: ту же цену не перевыставляем)
        better = (pos["side"] == 1 and cand >= pos["sl"] + step) or (pos["side"] == -1 and cand <= pos["sl"] - step)
        if better:
            new_sl = cand
            if sl_id:
                try:
                    orders_mgr._grpc_cancel(sl_id)
                except Exception:
                    pass
            cond = "last_up" if pos["side"] == -1 else "last_down"
            oid = orders_mgr.place_sl_stop(params.symbol, _close_side(pos["side"]), pos["qty"], new_sl, stop_condition=cond, tag="trail")
            if oid:
                pos["sl"] = new_sl
                pos["sl_order_id"] = oid
                logg(f"🔁 Трейлинг: SL переставлен → {new_sl:.2f} (id={oid})")
            else:
                pos["sl_order_id"] = None  # перевыставится на следующем цикле
            _save_state()


def orders_loop():
    """Мониторинг биржевых заявок — постоянный, пока жива позиция.
    19.11: строго под exec_lock — иначе гонка с _do_close: монитор видел
    «снятые» TP/SL в момент флип-закрытия и перевыставлял их закрытой позиции."""
    while True:
        try:
            if engine.position:
                with exec_lock:
                    if engine.position:  # закрылась, пока ждали лок — не лезем
                        _monitor_orders()
        except Exception as e:
            logg(f"orders_loop: {e}", logging.ERROR)
        time.sleep(5)


def _d1_direction(candles: list, now: float = None):
    """Тренд последнего ЗАКРЫТОГО дня (заказ Самурая 16.11.2026, честная семантика):
    дневные закрытия из часовых баров по UTC-дням, последний close vs EMA(d1_ema_period).
    Текущий (формирующийся) день ИСКЛЮЧЁН — подглядывания нет. None = мало данных."""
    if not candles:
        return None
    day = 86400
    cur_day = int((now or time.time()) // day)
    dc = {}
    for c in candles:  # candles отсортированы по t — последний close дня победит
        d = c["t"] // day
        if d < cur_day:
            dc[d] = c["c"]
    days = sorted(dc)
    if len(days) < params.d1_ema_period + 1:
        return None
    closes = [dc[d] for d in days]
    e_v = ema(closes, params.d1_ema_period)
    e = e_v[-1]
    if e is None:
        return None
    return LONG if closes[-1] > e else SHORT


def _cleanup_orphan_orders():
    """Сверка биржевых заявок при старте (заказ Самурая 16.11: рестарты не должны
    оставлять осиротевших заявок). Всё живое по символу, кроме TP/SL восстановленной
    позиции, снимается."""
    if params.paper or not orders_mgr:
        return
    try:
        orders = orders_mgr.get_symbol_orders(params.symbol)
        if orders is None:
            logg("сверка заявок при старте: канал не ответил — пропускаю", logging.WARNING)
            return
        keep = set()
        pos = engine.position
        if pos:
            for k in ("tp_order_id", "sl_order_id"):
                if pos.get(k):
                    keep.add(str(pos[k]))
        if entry_limit and entry_limit.get("id"):
            keep.add(str(entry_limit["id"]))  # своя entry-лимитка — НЕ снимать (16.11)
        live = [o for o in orders
                if o["status"] in ("ORDER_STATUS_NEW", "ORDER_STATUS_WORKING", "ORDER_STATUS_ACTIVE")]
        for o in live:
            oid = str(o["order_id"])
            if oid in keep:
                continue
            try:
                orders_mgr._grpc_cancel(oid)
                logg(f"🧹 сверка при старте: снята осиротевшая заявка {o['side']} "
                     f"{o['qty']} @ {o['limit_price'] or o['stop_price']} (id={oid})")
            except Exception as e:
                logg(f"сверка: снятие {oid}: {e}", logging.WARNING)
    except Exception as e:
        logg(f"сверка заявок: {e}", logging.ERROR)


def bar_loop():
    """Свечи + индикаторы + сигналы по закрытым барам."""
    first = True
    while True:
        try:
            candles = _load_candles(full=first)
            if first:
                first = False
            if candles:
                global _LIVE_BAR
                iss_last = candles[-1]["t"]
                # 1) ISS отдал официальную свечу — pending больше не нужен
                for t in list(_PENDING_BARS):
                    if t <= iss_last:
                        del _PENDING_BARS[t]
                # 2) мердж: закрытые live-бары (M1+тик) входят в движок →
                #    сигнал срабатывает на закрытии часа (≤15с), не ждём лаг ISS H1
                extra = [_PENDING_BARS[t] for t in sorted(_PENDING_BARS) if t > iss_last]
                engine.recompute(candles + extra)
                if _LIVE_BAR and engine.candles and engine.candles[-1]["t"] >= _LIVE_BAR["t"]:
                    _LIVE_BAR = {}  # час закрыт и уже в барах — далее новая свеча
                _fill_gap_bars()
                _refresh_live_volume()
                engine.d1_dir = _d1_direction(engine.candles)
                if mode["m"] == "running":
                    evs = engine.scan()
                    if evs and params.entry_by_limit and not params.paper:
                        # 🎯 вход по касанию исполняет биржа (entry-лимитка на EMA);
                        # маркет-вход pb по закрытию бара — гасим;
                        # кросс-входы (reason=cross) идут маркетом как есть
                        evs = [e for e in evs if not (e.get("action") == "open" and e.get("reason") == "pb")]
                    if evs:
                        evs = _drop_stale_opens(evs)
                    if evs:
                        execute_events(evs)
                _manage_entry_limit()
        except Exception as e:
            logg(f"bar_loop: {e}", logging.ERROR)
        time.sleep(15)


# ============ HTTP ============
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status":
            self._json(self._status())
        elif path == "/params":
            self._json({"params": vars(params)})
        elif path == "/chart":
            self._json(self._chart())
        elif path == "/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global params
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                return self._json({"error": "bad json"}, 400)

        if path == "/params":
            import re as _re
            _hhmm = _re.compile(r"^\d{1,2}:\d{2}$")
            with state_lock:
                changed_tf = body.get("timeframe", params.timeframe) != params.timeframe
                for k, v in body.items():
                    if not hasattr(params, k):
                        continue
                    # защита: поля сессии — только валидный HH:MM (не даём обнулить)
                    if k in ("session_start", "session_end", "evening_start", "evening_end"):
                        if isinstance(v, str) and _hhmm.match(v.strip()):
                            setattr(params, k, v.strip())
                        else:
                            logg(f"params: invalid {k}={v!r} — kept {getattr(params, k)}", logging.WARNING)
                        continue
                    if k == "timeframe" and v not in ("M1", "M5", "M15", "M30", "H1", "D"):
                        continue
                    if k in ("long_qty", "short_qty"):
                        # tri-state: None (пусто) = авто, 0 = запрет, N = фикс
                        if v is None or (isinstance(v, str) and not v.strip()):
                            v = None
                        else:
                            try:
                                v = int(v)
                            except (TypeError, ValueError):
                                v = None
                    if k == "max_capital":
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            v = 0.0
                    setattr(params, k, v)
                engine.p = params
                src.secid = params.ticker
                save_params(params)
            if changed_tf:
                # перезагрузить историю под новый ТФ
                try:
                    candles = _load_candles(full=True)
                    engine.recompute(candles)
                except Exception as e:
                    logg(f"TF reload: {e}", logging.ERROR)
            return self._json({"ok": True, "params": vars(params)})

        if path == "/start":
            mode["m"] = "running"
            logg("▶️ START (running)")
            # ☑ Позиция при старте: без ожидания сигнала, по текущему режиму (без фильтров)
            if params.entry_on_start:
                with engine.lock:
                    pos = engine.position
                    n = len(engine.osc_v)
                    osc = engine.osc_v[n - 1] if n else None
                if pos:
                    logg("позиция при старте: уже открыта — пропускаю")
                elif osc is None:
                    logg("позиция при старте: режим не определён (нет данных) — жду сигнал", logging.WARNING)
                elif osc == 0:
                    logg("позиция при старте: режим FLAT (osc=0) — жду сигнал", logging.WARNING)
                else:
                    side = LONG if osc > 0 else SHORT
                    price = (src.last_quote or {}).get("last") or (engine.candles[-1]["c"] if engine.candles else 0.0)
                    qty = engine._qty_for(side, price)
                    if qty > 0:
                        logg(f"позиция при старте: {'LONG' if side==LONG else 'SHORT'} по режиму (osc={osc:.2f})")
                        threading.Thread(target=_do_open, args=(side, qty, "start-regime"), daemon=True).start()
                    else:
                        logg("позиция при старте: qty=0 (направление выкл или нет капитала)", logging.WARNING)
            return self._json({"ok": True, "mode": "running"})
        if path == "/pause":
            mode["m"] = "paused"
            logg("⏸ PAUSE")
            return self._json({"ok": True, "mode": "paused"})
        if path == "/stop":
            mode["m"] = "stopped"
            logg("⏹ STOP — closing position")
            _do_close("manual")
            return self._json({"ok": True, "mode": "stopped"})
        if path == "/reset-stats":
            with engine.lock:
                engine.journal = []
                engine.realized_pnl = 0.0
                engine.commission_paid = 0.0
                engine.peak_pnl = 0.0
                engine.max_dd = 0.0
            _save_state()
            logg("🗑 stats reset")
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # -------- payloads --------
    def _status(self):
        i = len(engine.osc_v) - 1 if engine.osc_v else -1
        osc = engine.osc_v[i] if i >= 0 else None
        rsi_v = engine.rsi_v[i] if i >= 0 else None
        ema_v = engine.ema_v[i] if i >= 0 else None
        sar_v = engine.sar_v[i] if i >= 0 else None
        regime = "LONG" if (osc or 0) > 0 else "SHORT" if (osc or 0) < 0 else "—"
        q = src.last_quote
        return {
            "mode": mode["m"],
            "paper": params.paper,
            "symbol": params.symbol,
            "account": params.account,
            "timeframe": params.timeframe,
            "in_session": _in_session(),
            "price": q.get("last") if q else None,
            "quote_ts": q.get("ts") if q else None,
            "quote_lag_ms": int((time.time() - q["ts"]) * 1000) if q and q.get("ts") else None,
            "quote_src": q.get("source") if q else None,
            "bid": q.get("bid") if q else None,
            "offer": q.get("offer") if q else None,
            "indicators": {"osc": osc, "rsi": rsi_v, "ema": ema_v, "sar": sar_v,
                           "regime": regime, "d1_dir": engine.d1_dir},
            "position": engine.position,
            "pending": engine.pending,
            "entry_limit": dict(entry_limit) if entry_limit else None,
            "unrealized": round(engine.unrealized, 2),
            "stats": engine.stats(),
            "journal": engine.journal[-100:],
            "last_signal": engine.last_signal,
            "events": events[-30:],
            "candles_ok": len(engine.candles),
            "ts": time.time(),
        }

    def _chart(self):
        tail = 400
        candles = list(engine.candles[-tail:])
        ema_t = list(engine.ema_v[-tail:])
        sar_t = list(engine.sar_v[-tail:])
        rsi_t = list(engine.rsi_v[-tail:])
        osc_t = list(engine.osc_v[-tail:])
        extra = 0
        last_t = candles[-1]["t"] if candles else 0
        for b in sorted(_PENDING_BARS.values(), key=lambda x: x["t"]):
            if b["t"] > last_t:  # закрытые live-бары, которых ISS ещё не отдал
                candles.append(dict(b))
                ema_t.append(ema_t[-1] if ema_t else None)
                sar_t.append(sar_t[-1] if sar_t else None)
                rsi_t.append(rsi_t[-1] if rsi_t else None)
                osc_t.append(osc_t[-1] if osc_t else None)
                last_t = b["t"]
                extra += 1
        if _LIVE_BAR and _LIVE_BAR["t"] > last_t:
            candles.append(dict(_LIVE_BAR))  # текущий час — тиковый, реал-тайм
            ema_t.append(ema_t[-1] if ema_t else None)
            sar_t.append(sar_t[-1] if sar_t else None)
            rsi_t.append(rsi_t[-1] if rsi_t else None)
            osc_t.append(osc_t[-1] if osc_t else None)
            extra = 1
        n_full = len(engine.candles)
        off = n_full - (len(candles) - extra)
        # канал Дончиана: верх/низ по N предыдущим барам (без текущего — как в сигнале)
        n = params.donch_n
        dch_up = [None] * len(candles)
        dch_dn = [None] * len(candles)
        for i in range(n, len(candles)):
            dch_up[i] = max(candles[j]["h"] for j in range(i - n, i))
            dch_dn[i] = min(candles[j]["l"] for j in range(i - n, i))
        return {
            "candles": candles,
            "ema": ema_t,
            "sar": sar_t,
            "rsi": rsi_t,
            "osc": osc_t,
            "dch_up": dch_up,
            "dch_dn": dch_dn,
            "trades": engine.journal[-60:],
            "position": engine.position,
            "timeframe": params.timeframe,
            "offset": off,
        }


# ============ main ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5095)
    ap.add_argument("--paper", action="store_true", help="paper trading (no real orders)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    global params
    if args.paper:
        params.paper = True
    if params.paper:
        logg("📝 PAPER MODE — ордера симулируются по bid/ask")
    else:
        logg("💰 REAL MODE — счёт " + params.account)

    _load_state()
    _init_orders()
    _cleanup_orphan_orders()

    threading.Thread(target=quote_loop, daemon=True).start()
    threading.Thread(target=bar_loop, daemon=True).start()
    threading.Thread(target=orders_loop, daemon=True).start()
    threading.Thread(target=entry_loop, daemon=True, name="entry-loop").start()

    port = args.port
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logg(f"PLZL robot API on :{port} (stopped, ждёт /start)")
    sig_module.signal(sig_module.SIGTERM, lambda *a: os._exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
