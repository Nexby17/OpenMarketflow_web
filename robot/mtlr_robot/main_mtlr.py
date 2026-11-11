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

from config_mtlr import (MtlrParams, load_params, save_params,
                         TF_TO_ISS, TF_SECONDS)
from candles_source import CandleSource, aggregate_m1, iss_interval_for, needs_aggregation
from strategy_mtlr import StrategyEngine, LONG, SHORT
from orders_exec import ArbOrderManager, BUY, SELL

log = logging.getLogger("mtlr")

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtlr_state.json")
MSK = timezone(timedelta(hours=3))

# ============ globals ============
params: MtlrParams = load_params()
engine = StrategyEngine(params)
src = CandleSource(secid=params.ticker, history_bars=params.history_bars)
mode = {"m": "stopped"}          # stopped|paused|running
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
        from config_mtlr import TF_SECONDS
        return aggregate_m1(raw, TF_SECONDS[tf] // 60)
    return raw


def _m1_bars_needed(tf: str) -> int:
    bars = params.history_bars
    if needs_aggregation(tf):
        from config_mtlr import TF_SECONDS
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
        _save_state()


def _do_close(reason: str):
    with exec_lock:
        pos = engine.position
        if not pos:
            return
        q = src.last_quote or {}
        bs = SELL if pos["side"] == LONG else BUY
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
    with state_lock:
        st = engine.state_dict()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                engine.load_state(json.load(f))
            logg("state restored")
        except Exception as e:
            logg(f"state load failed: {e}", logging.ERROR)


# ============ loops ============
def quote_loop():
    """Live-цена + риск-чеки TP/SL/Trail."""
    while True:
        try:
            q = src.fetch_quote()
            last = q.get("last") or 0.0
            if last > 0 and mode["m"] == "running":
                ev = engine.on_tick(last)
                if ev:
                    execute_events([ev])
        except Exception as e:
            logg(f"quote_loop: {e}", logging.ERROR)
        time.sleep(5)


def bar_loop():
    """Свечи + индикаторы + сигналы по закрытым барам."""
    first = True
    while True:
        try:
            candles = _load_candles(full=first)
            if first:
                first = False
            if candles:
                engine.recompute(candles)
                if mode["m"] == "running":
                    evs = engine.scan()
                    if evs:
                        execute_events(evs)
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
                        try:
                            v = int(v)
                        except (TypeError, ValueError):
                            v = 0
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
            "bid": q.get("bid") if q else None,
            "offer": q.get("offer") if q else None,
            "indicators": {"osc": osc, "rsi": rsi_v, "ema": ema_v, "sar": sar_v,
                           "regime": regime},
            "position": engine.position,
            "pending": engine.pending,
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
        candles = engine.candles[-tail:]
        n_full = len(engine.candles)
        off = n_full - len(candles)
        return {
            "candles": candles,
            "ema": engine.ema_v[-tail:],
            "sar": engine.sar_v[-tail:],
            "rsi": engine.rsi_v[-tail:],
            "osc": engine.osc_v[-tail:],
            "trades": engine.journal[-60:],
            "position": engine.position,
            "timeframe": params.timeframe,
            "offset": off,
        }


# ============ main ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5094)
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

    threading.Thread(target=quote_loop, daemon=True).start()
    threading.Thread(target=bar_loop, daemon=True).start()

    port = args.port
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logg(f"MTLR robot API on :{port} (stopped, ждёт /start)")
    sig_module.signal(sig_module.SIGTERM, lambda *a: os._exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
