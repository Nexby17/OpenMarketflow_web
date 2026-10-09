"""F-061: main_sarema.py — сервис одноинстансового робота SAR×EMA.

Env: SAREMA_TICKER, SAREMA_PORT, SAREMA_CONFIG_FILE, SAREMA_STATE_FILE,
SAREMA_ACCOUNT_ID, DP_URL, SAREMA_DATA_DIR (прогрев), FINAM_API_KEY.

Startup-always-stopped (F-059). Идемпотентный /stop (F-060).
Запуск: python main_sarema.py (cwd = robot/sarema или PYTHONPATH с robot/).
"""
import json
import logging
import os
import threading
from datetime import datetime

import os as _os
import sys as _sys
_ROBOT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _ROBOT_DIR not in _sys.path:
    _sys.path.insert(0, _ROBOT_DIR)
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sarema.config_sarema import (TICKER, SYMBOL, ACCOUNT_ID, PORT, CONFIG_FILE,
                                  STATE_FILE, DP_URL, load_config, validate_params)
from sarema.state_sarema import SaremaState
from sarema.feed_sarema import H1Feed
from sarema.orders_sarema import SaremaOrders
from sarema.strategy_sarema import SaremaStrategy, FLAT

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sarema.main")

DATA_DIR = os.environ.get("SAREMA_DATA_DIR", "")
LOT_SIZE = float(os.environ.get("SAREMA_LOT_SIZE", "1"))

cfg = load_config()
state = SaremaState(STATE_FILE or f"sarema_state_{TICKER}.json", ticker=TICKER)
state.load()                              # восстановление; mode всё равно stopped
feed = H1Feed(DATA_DIR or None)
orders = SaremaOrders(ACCOUNT_ID, SYMBOL)
strategy = SaremaStrategy(cfg, state, orders, feed, lot_size=LOT_SIZE, ticker=TICKER)

app = FastAPI(title=f"SAREMA {TICKER}", version="1.0")

# CORS: UI (:5050 и зеркала) читает /status //journal напрямую с порта робота
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5050", "http://127.0.0.1:5050",
                   "http://localhost:5099", "http://127.0.0.1:5099"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_stop_lock = threading.Lock()             # F-060: идемпотентный /stop
_SAR_LAST_RESTART = [0.0]                 # restart cooldown (90с, урок F-059)


class ParamsPatch(BaseModel):
    base: float | None = None
    risk_pct: float | None = None
    max_long_qty: int | float | None = None
    max_short_qty: int | float | None = None
    max_value_rub: float | None = None
    liq_frac: float | None = None
    day_kill_R: float | None = None
    compound: bool | None = None
    # сигнальные SAR/EMA (редактируемые Дмитрием в popup)
    sar_af0: float | None = None
    sar_step: float | None = None
    sar_max: float | None = None
    ema_n: int | float | None = None
    init_stop_atr: float | None = None
    tf_min: int | float | None = None


@app.get("/status")
def status():
    pos = {
        "dir": state.direction,
        "qty": state.lots_qty,
        "entry": state.entry_price,
        "stop": state.stop_price,
        "entry_time": state.entry_time,
        "stop_order_id": strategy.stop_order_id,
    }
    limits_used = {
        "max_long_qty": cfg.get("max_long_qty"),
        "max_short_qty": cfg.get("max_short_qty"),
        "max_value_rub": cfg.get("max_value_rub"),
        "qty_used": state.lots_qty,
        "dir": state.direction,
        "value_used": state.lots_qty * state.entry_price if state.entry_price else 0.0,
    }
    return {
        "robot": "sarema", "ticker": TICKER, "symbol": SYMBOL, "port": PORT,
        "mode": state.mode, "position": pos,
        "pnl": {"total": state.total_pnl, "trades": state.trades_done},
        "risk": {"base": state.base, "risk_pct": cfg.get("risk_pct"),
                 "compound": cfg.get("compound"), "day_kill_R": cfg.get("day_kill_R"),
                 "kill_day": state.kill_day},
        "limits": limits_used,
        "last_bar": feed.last_bar_begin.isoformat() if feed.last_bar_begin else None,
        "stale": feed.check_stale(),
        "gaps": feed.check_gaps(),
        "warmup_ok": feed.bars is not None and len(feed.bars) >= cfg.get("warmup_bars", 300),
    }


@app.post("/start")
def start():
    if state.direction != FLAT and not state.stop_price:
        raise HTTPException(409, "position without stop — run /resync first")
    state.mode = "running"
    state.save()
    log.info("START")
    return {"ok": True, "mode": state.mode}


def _load_state_file() -> dict:
    """Читает state-файл с диска напрямую. Работает и когда процесс робота НЕ запущен —
    журнал/параметры видны в UI всегда (замечание Дмитрия: «сделал журнал? не вижу»)."""
    path = STATE_FILE or f"sarema_state_{TICKER}.json"
    if not os.path.exists(path):
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.basename(path))
        if os.path.exists(alt):
            path = alt
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@app.get("/bars")
def bars(tf_min: int = 60, limit: int = 300):
    """H1/TF бары с EMA и PSAR (по текущим параметрам конфига) — для графика в popup UI.
    Источник: прогрев-CSV + живые бары в feed (если running). Только основная сессия."""
    from sarema import sarema_engine as _eng
    limit = max(50, min(int(limit), 1000))
    tf = max(5, min(int(tf_min), 240))
    try:
        if feed.bars is not None and len(feed.bars):
            df = feed.bars.copy()
            df = df.rename(columns={'begin': 't'})
        else:
            df = _eng.load_m1(TICKER, DATA_DIR) if DATA_DIR else None
            if df is None:
                return {"bars": [], "indicators": [], "note": "no data"}
        g, up, dn = _eng.build(df, f"{tf}min",
                               cfg.get("sar_af0", 0.02), cfg.get("sar_step", 0.2),
                               int(cfg.get("ema_n", 12)), cfg.get("sar_max", 0.2))
        tail = g.tail(limit)
        bars = [{"t": str(r.begin), "o": float(r.open), "h": float(r.high),
                 "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
                for r in tail.itertuples()]
        inds = [{"t": str(r.begin), "sar": float(r.sar), "ema": float(r.ema)}
                for r in tail.itertuples()]
        return {"ticker": TICKER, "tf_min": tf, "bars": bars, "indicators": inds,
                "up": [bool(x) for x in up[-len(tail):]],
                "dn": [bool(x) for x in dn[-len(tail):]]}
    except Exception as e:
        return {"bars": [], "indicators": [], "error": str(e)}


class PositionAdjust(BaseModel):
    qty: int                      # штуки СО ЗНАКОМ: абсолютная позиция (2, -1, 0=FLAT)
    price: float = 0.0            # средняя цена входа (обязательна при qty!=0)


@app.post("/position/adjust")
def position_adjust(body: PositionAdjust):
    """Ручная корректировка позиции (как у OF-роботов): синхронизация state с брокером
    БЕЗ ордеров. Используется после ручной сделки Дмитрия в терминале.
    qty: 0 = FLAT (обнулить), положительное = LONG qty штук, отрицательное = SHORT.
    price: средняя цена входа (при qty!=0 обязательна > 0).
    Стоп выставляется по 3×ATR от price (или сохранённый, если баров нет)."""
    import time as _t
    qty = int(body.qty)
    price = float(body.price or 0)
    if qty != 0 and price <= 0:
        raise HTTPException(400, {"ok": False, "error": "при qty!=0 требуется price > 0"})
    if abs(qty) > 100000:
        raise HTTPException(400, {"ok": False, "error": "|qty| > 100000 — подозрительно много"})

    # остановить трейдинг-логику на время правки (режим сохраняем)
    was_running = state.mode == "running"
    state.mode = "paused"

    try:
        # отменить активный брокер-стоп перед правкой
        if strategy.stop_order_id:
            orders.cancel(strategy.stop_order_id)
            strategy.stop_order_id = None

        if qty == 0:
            old = f"{state.direction} {state.lots_qty}"
            state.direction = 0
            state.lots_qty = 0
            state.entry_price = 0.0
            state.stop_price = 0.0
            state.entry_time = None
            log.info(f"MANUAL ADJUST: FLAT (было {old}, ордеров НЕ было)")
            action = "FLAT"
        else:
            new_dir = 1 if qty > 0 else -1
            abs_qty = abs(qty)
            state.direction = new_dir
            state.lots_qty = abs_qty
            state.entry_price = price
            state.stop_price = price if price > 0 else 0.0
            state.entry_time = datetime.utcnow().isoformat()
            # стоп по 3×ATR от указанной цены (если есть бары)
            atr_now = None
            try:
                if feed.bars is not None and len(feed.bars) >= 20:
                    from sarema import sarema_engine as _eng
                    _df = feed.bars.rename(columns={'begin': 't'}) if 't' not in feed.bars.columns else feed.bars
                    g = _eng.m5_extras(_eng.tf_bars(_df, f"{int(cfg.get('tf_min', 60))}min"))
                    atr_now = float(g["atr"].iloc[-1])
            except Exception as _e:
                log.warning(f"adjust ATR calc failed: {_e}")
                atr_now = None
            if atr_now and atr_now > 0:
                sd = cfg.get("init_stop_atr", 3.0) * atr_now
                state.stop_price = round(price - sd if new_dir == 1 else price + sd, 2)
            log.info(f"MANUAL ADJUST: {'LONG' if new_dir == 1 else 'SHORT'} {abs_qty} шт @ {price:.2f}, стоп {state.stop_price:.2f} (ордеров НЕ было)")
            action = f"{new_dir} {abs_qty} @ {price}"
            # выставить брокер-стоп на новую позицию
            strategy._place_broker_stop(state.stop_price, abs_qty, new_dir)

        state.save()
        if was_running:
            state.mode = "running"      # вернуться в running (позиция теперь «своя»)
            state.save()
        return {"ok": True, "dir": state.direction, "qty": state.lots_qty,
                "entry": state.entry_price, "stop": state.stop_price, "mode": state.mode}
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"MANUAL ADJUST error: {e}")
        raise HTTPException(500, {"ok": False, "error": str(e)})


@app.get("/journal")
def journal():
    """Журнал позиций (логика таблицы Дмитрия): открытые сверху, закрытие дозаписывается.
    Читается из state-файла на диске — доступен даже при остановленном/невключённом роботе."""
    disk = _load_state_file()
    hist = disk.get("trade_history", state.trade_history)
    open_recs = [r for r in hist if r.get("status") == "open"]
    closed_recs = [r for r in hist if r.get("status") == "closed"]
    return {
        "ticker": TICKER,
        "open": list(reversed(open_recs)),
        "closed": list(reversed(closed_recs)),
        "open_count": len(open_recs),
        "closed_count": len(closed_recs),
        "base": disk.get("base", state.base),
    }


@app.get("/health")
def health():
    """Liveness probe для RobotProcessManager (C#) и UI — быстрый, без обращения к брокеру."""
    return {"ok": True, "symbol": SYMBOL, "ticker": TICKER, "mode": state.mode}


@app.post("/stop")
def stop():
    """Идемпотентный /stop (урок F-060): mutex + re-check под замком."""
    with _stop_lock:
        state.mode = "stopped"
        state.save()
    log.info("STOP (idempotent)")
    return {"ok": True, "mode": state.mode}


@app.post("/pause")
def pause():
    state.mode = "paused"
    state.save()
    return {"ok": True, "mode": state.mode}


@app.post("/restart")
def restart():
    """Перезапуск по регламенту (урок F-059): FLAT-проверка + cooldown 90с + отложенный exit.
    RobotProcessManager (C# proxy) поднимет процесс заново; позиция сохранена в state."""
    import time as _t
    left = 90 - (_t.time() - _SAR_LAST_RESTART[0])
    if left > 0:
        raise HTTPException(429, {"ok": False, "error": "cooldown", "retry_after_sec": int(left) + 1})
    if state.direction != 0:
        raise HTTPException(409, {"ok": False, "error": "position open",
                                  "hint": "close position first or use /restart?force=1"})
    _SAR_LAST_RESTART[0] = _t.time()
    state.mode = "stopped"
    state.save()
    # отложенный выход: даём uvicorn ответить, затем процесс завершается; UI/proxy поднимет заново.
    def _delayed_exit():
        _t.sleep(2.0)
        os._exit(0)
    threading.Thread(target=_delayed_exit, daemon=True).start()
    log.info("RESTART scheduled (FLAT, cooldown passed)")
    return {"ok": True, "restarting": True}


@app.post("/resync")
def resync():
    """Reconciliation с брокером: позиция/цены — от брокера (урок F-048)."""
    broker_qty = orders.get_position()
    diff = broker_qty - state.lots_qty * (state.direction if state.direction else 0)
    return {"ok": True, "broker_qty": broker_qty, "local_qty": state.lots_qty,
            "local_dir": state.direction, "diff": diff,
            "note": "full resync in stage 2 finalization (fills/entry price)"}


@app.get("/params")
def get_params():
    return cfg


@app.post("/params")
def post_params(patch: ParamsPatch):
    clean, errors = validate_params(patch.model_dump(exclude_none=True))
    if errors:
        raise HTTPException(400, {"errors": errors})
    cfg.update(clean)
    if CONFIG_FILE:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    state.base = cfg.get("base", state.base)
    state.save()
    log.info(f"params updated: {clean}")
    return {"ok": True, "params": cfg}


def _main_loop():
    """Цикл робота: раз в минуту проверяет новый закрытый H1-бар; при появлении —
    strategy.on_closed_bar(). Рестарт между итерациями через state."""
    import time
    log.info("main loop started")
    # --- Live H1 feed: Finam REST напрямую (JWT 15 мин, кэшируем) ---
    import urllib.request as _uq
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    _feed_state = {"tok": None, "tok_ts": 0.0, "last_closed": None}

    def _finam_token():
        if _feed_state["tok"] and time.time() - _feed_state["tok_ts"] < 800:
            return _feed_state["tok"]
        env_path = os.environ.get("FINAM_API_KEY")
        if not env_path:
            # .env рядом с robot/../src/... (как в orders_sarema)
            for cand in (os.path.join(_ROBOT_DIR, "..", "src", "Server", "bin", "Debug", "net10.0", ".env"),):
                if os.path.exists(cand):
                    with open(cand, encoding="utf-8-sig") as f:
                        for line in f:
                            if line.strip().startswith("FINAM_API_KEY="):
                                env_path = line.split("=", 1)[1].strip()
                                break
        if not env_path:
            raise RuntimeError("FINAM_API_KEY not found")
        r = _uq.Request("https://api.finam.ru/v1/sessions",
                        data=_json.dumps({"secret": env_path}).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        with _uq.urlopen(r, timeout=15) as resp:
            tok = _json.loads(resp.read().decode())["token"]
        _feed_state["tok"] = tok
        _feed_state["tok_ts"] = time.time()
        return tok

    def _fetch_recent_h1():
        """Последние 2 дня H1 LKOH-класса (акции), возвращает список закрытых баров (кроме текущего часа)."""
        tok = _finam_token()
        now = _dt.utcnow()
        url = (f"https://api.finam.ru/v1/instruments/{SYMBOL}/bars?timeframe=TIME_FRAME_H1"
               f"&interval.start_time={(now - _td(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&interval.end_time={now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        r = _uq.Request(url)
        r.add_header("Authorization", tok)
        with _uq.urlopen(r, timeout=15) as resp:
            d = _json.loads(resp.read().decode())
        out = []
        hour_ago = now - _td(minutes=65)   # бар закрыт, если его час закончился минуту назад
        for b in d.get("bars", []):
            ts = b.get("timestamp", "").replace("Z", "").replace("+00:00", "")
            g = lambda k: b.get(k, {}).get("value", b.get(k))
            try:
                bt = _dt.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                continue
            # фильтр основной сессии 07:00-15:40 UTC + только закрытые
            tod = bt.hour * 3600 + bt.minute * 60
            if not (7 * 3600 <= tod <= 15 * 3600 + 40 * 60):
                continue
            if bt >= hour_ago:
                continue
            out.append((bt, float(g("open")), float(g("high")), float(g("low")),
                        float(g("close")), float(g("volume") or 0)))
        return out

    while True:
        try:
            if state.mode == "running":
                try:
                    bars = _fetch_recent_h1()
                    new_count = 0
                    for (bt, o, h, l, c, v) in bars:
                        if feed.append_closed_bar(bt, o, h, l, c, v):
                            new_count += 1
                    if new_count:
                        log.info(f"live feed: +{new_count} closed H1 bars (last {bars[-1][0] if bars else '?'})")
                        ev = strategy.on_closed_bar()
                        if ev.get("events"):
                            log.info(f"strategy events: {ev['events']}")
                except Exception as fe:
                    log.error(f"live feed error: {fe}")
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    # RobotProcessManager передаёт --port (совместимость с OF-паттерном запуска)
    import sys as _sys
    if "--port" in _sys.argv:
        try:
            _p = int(_sys.argv[_sys.argv.index("--port") + 1])
            if _p > 0:
                import sarema.config_sarema as _cfgmod
                _cfgmod.PORT = _p
                PORT = _p
        except (ValueError, IndexError):
            pass
    warm = feed.warmup_from_csv(TICKER) if DATA_DIR else False
    log.info(f"SAREMA {TICKER} port={PORT} warmup={'ok' if warm else 'SKIPPED (no SAREMA_DATA_DIR)'}")
    t = threading.Thread(target=_main_loop, daemon=True)
    t.start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
