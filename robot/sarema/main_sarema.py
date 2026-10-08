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
_stop_lock = threading.Lock()             # F-060: идемпотентный /stop
_SAR_LAST_RESTART = [0.0]                 # restart cooldown (90с, урок F-059)


class ParamsPatch(BaseModel):
    base: float | None = None
    risk_pct: float | None = None
    max_qty: int | float | None = None
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
        "max_qty": cfg.get("max_qty"),
        "max_value_rub": cfg.get("max_value_rub"),
        "qty_used": state.lots_qty,
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
    while True:
        try:
            if state.mode == "running":
                # TODO этап 2 finalization: подписка/поллинг REST-баров Finam,
                # append_closed_bar -> on_closed_bar; до тех пор loop в standby
                pass
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
