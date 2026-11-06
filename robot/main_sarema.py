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
orders = SaremaOrders(ACCOUNT_ID, SYMBOL, state_ref=state)
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
_SAR_LAST_RESTART = [0.0]
_LAST_FLAT_CLEAN = [0.0]                 # F-158: таймер флэт-чистки                 # restart cooldown (90с, урок F-059)


class ParamsPatch(BaseModel):
    base: float | None = None
    risk_pct: float | None = None
    max_long_qty: int | float | None = None
    max_short_qty: int | float | None = None
    max_value_rub: float | None = None
    compound: bool | None = None
    # сигнальные SAR/EMA (редактируемые Дмитрием в popup)
    sar_af0: float | None = None
    sar_step: float | None = None
    sar_max: float | None = None
    ema_n: int | float | None = None
    tf_min: int | float | None = None
    # F-065: VP Mode (range/fade/breakout/off) + подтипы (null = дефолт)
    vah_val_mode: str | None = None
    vah_val_bin: float | None = None
    vah_val_pct: float | None = None
    vah_val_lookback: int | float | None = None
    tp_R: int | float | None = None   # F-084: take-profit по R
    tp_mode: str | None = None   # F-105: fixed | sar
    size_mode: str | None = None   # F-131: risk | capital
    # F-092: стоп-лосс и трейлинг в единицах (%|rub|points; atr/sar = старое поведение)
    stop_loss_value: int | float | None = None
    stop_loss_unit: str | None = None
    trail_value: int | float | None = None
    trail_unit: str | None = None
    be_value: int | float | None = None
    be_unit: str | None = None
    max_signal_age: int | float | None = None   # F-086: окно сигналов


_CS_PORT = int(os.environ.get("CS_SERVER_PORT", "5050"))  # F-072: C# server for live tick
_QUOTE_CACHE = {"mark": None, "ts": 0.0, "src": ""}


def _live_mark() -> tuple[float | None, str]:
    """F-072 fix2: реал-тайм отметка (тик). Приоритет: C# /api/quote (Finam last) →
    DP /quote → close последнего закрытого H1. Кэш 10с (Finam rate-limit)."""
    import time as _t
    import urllib.request as _uq
    import json as _json
    if _QUOTE_CACHE["mark"] is not None and _t.time() - _QUOTE_CACHE["ts"] < 5:
        return _QUOTE_CACHE["mark"], _QUOTE_CACHE["src"]
    # 1) C# сервер (тот же тик, что показывает брокерский ПнЛ)
    try:
        with _uq.urlopen(f"http://127.0.0.1:{_CS_PORT}/api/quote?ticker={TICKER}", timeout=4) as r:
            q = _json.loads(r.read())
        last = float(q.get("last") or 0)
        if last > 0:
            _QUOTE_CACHE.update(mark=last, ts=_t.time(), src="Finam tick")
            return last, _QUOTE_CACHE["src"]
    except Exception as _e1:
        try:
            log.warning(f"live mark step1 failed: {type(_e1).__name__}: {_e1}")
        except Exception:
            pass
    # 2) DataProvider (стрим кэша)
    try:
        with _uq.urlopen(f"{DP_URL}/quote/{SYMBOL}", timeout=4) as r:
            q = _json.loads(r.read())
        last = float(q.get("last") or 0)
        if last > 0:
            _QUOTE_CACHE.update(mark=last, ts=_t.time(), src="DP")
            return last, _QUOTE_CACHE["src"]
    except Exception:
        pass
    # 3) fallback: close последнего закрытого H1
    try:
        if feed.bars is not None and len(feed.bars):
            last = float(feed.bars["close"].iloc[-1])
            _QUOTE_CACHE.update(mark=last, ts=_t.time(), src="H1 close")
            return last, _QUOTE_CACHE["src"]
    except Exception:
        pass
    return None, ""


def _pnl_block() -> dict:
    """F-072: реализованный + нереализованный PnL. Отметка реал-тайм (тик брокера),
    fallback H1-close. source — откуда отметка."""
    unreal = None
    src = ""
    try:
        if state.direction != 0:
            mark, src = _live_mark()
            if mark is not None:
                unreal = round((mark - state.entry_price) * state.direction * state.lots_qty, 2)
    except Exception:
        pass
    # F-162: total из журнала НА ДИСКЕ (правильный PnL по цене, без фантомной комиссии)
    _disk = _load_state_file()
    _hist = _disk.get("trade_history", [])
    _jp = sum((r.get("close") or {}).get("pnl") or 0.0 for r in _hist if r.get("status") == "closed")
    _jc = sum(1 for r in _hist if r.get("status") == "closed")
    total = round(_jp + (unreal or 0.0), 2)
    return {"total": round(_jp, 2), "trades": _jc,
            "unrealized": unreal, "total_with_unrealized": total, "mark_source": src}


@app.get("/status")
def status():
    pos = {
        "dir": state.direction,
        "qty": state.lots_qty,
        # F-147: при ФЛЭТ вход/стоп пустые (null) — UI показывает «—»,
        # а не устаревшие значения последней сделки.
        "entry": state.entry_price if state.direction else None,
        "stop": state.stop_price if state.direction else None,
        "entry_time": state.entry_time if state.direction else None,
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
    # F-162: ПнЛ журнала читаем С ДИСКА (как /journal) — память может расходиться с диском.
    _disk = _load_state_file()
    _hist = _disk.get("trade_history", state.trade_history or [])
    _journal_pnl = sum((r.get("close") or {}).get("pnl") or 0.0
                       for r in _hist if r.get("status") == "closed")
    _journal_trades = sum(1 for r in _hist if r.get("status") == "closed")
    return {
        "robot": "sarema", "ticker": TICKER, "symbol": SYMBOL, "port": PORT,
        "mode": state.mode, "position": pos,
        "pnl": _pnl_block(),
        "journal_pnl": round(_journal_pnl, 2),
        "risk": {"base": state.base, "risk_pct": cfg.get("risk_pct"),
                 "compound": cfg.get("compound")},
        "limits": limits_used,
        "last_bar": feed.last_bar_begin.isoformat() if feed.last_bar_begin else None,
        "stale": feed.check_stale(),
        "gaps": feed.check_gaps(),
        "warmup_ok": feed.bars is not None and len(feed.bars) >= cfg.get("warmup_bars", 300),
        # F-065: VP Mode + живой VP (VAH/VAL/POC) для панели UI
        "vp": {"mode": cfg.get("vah_val_mode", "off"), **strategy.vp.state()},
        # F-105: живой TP для панели
        "tp": {"mode": cfg.get("tp_mode", "fixed"), "R": cfg.get("tp_R"),
               "price": strategy.tp_price(state.direction, state.entry_price,
                                          strategy.last_sar or (state.entry_price or 0),
                                          (strategy.last_stats or {}).get("atr") or 0.0) if state.direction != 0 and state.entry_price else None,
               "order_id": state.tp_order_id},
    }


@app.post("/start")
def start():
    if state.direction != FLAT and not state.stop_price:
        raise HTTPException(409, "position without stop — run /resync first")
    state.mode = "running"
    state.auto_start_disabled = False   # F-076: явный ▶ возвращает авто-старт
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
            state.entry_time = (datetime.utcnow() + _td(hours=3)).isoformat()  # F-151: МСК-naive (время везде московское)
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


@app.delete("/journal")
def journal_clear():
    """F-149: очистить торговый журнал (trade_history в state и на диске).
    Только при ФЛЭТ — при открытой позиции журнал ведёт запись входа."""
    if state.direction != 0 or state.lots_qty:
        raise HTTPException(400, "очистка журнала только при ФЛЭТ (закрой позицию)")
    state.trade_history = []
    if CONFIG_FILE:
        pass  # журнал живёт в state-файле, не в config
    # перезаписать state-файл на диске с пустой историей
    state.save()
    log.info("F-149: торговый журнал очищен (trade_history=[])")
    return {"ok": True, "open": 0, "closed": 0}


@app.get("/health")
def health():
    """Liveness probe для RobotProcessManager (C#) и UI — быстрый, без обращения к брокеру."""
    return {"ok": True, "symbol": SYMBOL, "ticker": TICKER, "mode": state.mode}


@app.post("/stop")
def stop():
    """Идемпотентный /stop (урок F-060): mutex + re-check под замком."""
    with _stop_lock:
        state.mode = "stopped"
        state.auto_start_disabled = True   # F-076: после ручного ⏹ робот сам не включится
        state.save()
    log.info("STOP (idempotent, auto-start disabled)")
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




@app.get("/feed_dump")
def feed_dump():
    """F-143: диагностический дамп последних 30 баров фида робота."""
    try:
        from sarema import sarema_engine as _eng_local
        _df = feed.bars.copy()
        _df['t'] = _df['begin']
        g2, up, dn = _eng_local.build(_df, f"{int(cfg.get('tf_min', 60))}min",
                                cfg.get("sar_af0", 0.02), cfg.get("sar_step", 0.2),
                                int(cfg.get("ema_n", 34)), cfg.get("sar_max", 0.2))
        n = len(g2)
        out = []
        for i in range(max(0, n - 30), n):
            out.append({
                "begin": str(g2["begin"].iloc[i])[:16],
                "close": round(float(g2["close"].iloc[i]), 2),
                "sar": round(float(g2["sar"].iloc[i]), 2),
                "ema": round(float(g2["ema"].iloc[i]), 2),
                "up": bool(up[i]),
                "dn": bool(dn[i]),
            })
        return {"robot": TICKER, "bars_total": n, "last_30": out}
    except Exception as e:
        return {"error": str(e)}
@app.post("/params")
def post_params(patch: ParamsPatch):
    clean, errors = validate_params(patch.model_dump(exclude_unset=True))  # F-070: null доходит (очистка kill/лимитов)
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
    # F-107: JWT Finam живёт ~сутки; планируем превентивный само-перезапуск процесса раз в 20 часов,
    # чтобы токен обновлялся гарантированно и роботы не вставали в entry_error 'Token invalid'.
    _start_ts = time.time()
    _RESTART_AFTER = 20 * 3600
    log.info("main loop started (F-107: auto-restart every 20h)")
    # F-117: убрать накопленные дубли стоп-заявок (после рестартов stop_order_id терялся)
    try:
        _cancelled = strategy.cleanup_duplicate_stops()
        if _cancelled:
            log.info(f'F-117: cleanup removed {_cancelled} duplicate stop orders')
    except Exception as _ce2:
        log.error(f'F-117 cleanup err: {_ce2}')
    # F-127: если робот во флэте — снять ВСЕ его заявки у брокера (мусор прошлых запусков)
    try:
        if state.direction == 0:
            _n = orders.cancel_all_robot_orders()
            if _n:
                log.warning(f'F-127: снято {_n} заявок при флэте (мусор прошлых запусков)')
    except Exception as _ce3:
        log.error(f'F-127 flat-cleanup err: {_ce3}')
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
        now = _dt.utcnow()   # F-151: UTC нужен ТОЛЬКО для интервала /bars (формат API Finam 'Z'); бары конвертируются в МСК ниже
        url = (f"https://api.finam.ru/v1/instruments/{SYMBOL}/bars?timeframe=TIME_FRAME_H1"
               f"&interval.start_time={(now - _td(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&interval.end_time={now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        r = _uq.Request(url)
        r.add_header("Authorization", tok)
        with _uq.urlopen(r, timeout=15) as resp:
            d = _json.loads(resp.read().decode())
        out = []
        hour_ago = now + _td(hours=3) - _td(minutes=65)   # F-150: порог в МСК-naive (как bt: UTC+3), иначе свежие бары skip на 3+ часа
        for b in d.get("bars", []):
            ts = b.get("timestamp", "").replace("Z", "").replace("+00:00", "")
            g = lambda k: b.get(k, {}).get("value", b.get(k))
            try:
                bt = _dt.strptime(ts, "%Y-%m-%dT%H:%M:%S")
                # F-112: Finam отдаёт UTC; история робота (M1-кэш warmup) в МСК-naive.
                # Приводим live-бары к МСК-naive (+3ч), иначе смешение поясов ломает хронологию фида и SAR.
                bt = bt + _td(hours=3)
            except Exception:
                continue
# F-071 fix2: session-фильтр удалён — вечерняя/утренняя сессии MOEX реальны;
# их выброс ломал SAR-траекторию и рассинхронизировал сигналы с графиком.
            if bt >= hour_ago:
                continue
            out.append((bt, float(g("open")), float(g("high")), float(g("low")),
                        float(g("close")), float(g("volume") or 0)))
        return out

    while True:
        try:
            # --- F-084: TAKE-PROFIT по R — закрытие при ПнЛ ≥ tp_R × риск ---
            if state.mode == "running" and state.direction != 0 and cfg.get("tp_R"):
                try:
                    mark, src = _live_mark()
                    if mark is not None:
                        risk_rub = state.base * cfg.get("risk_pct", 0.015)
                        pnl_now = (mark - state.entry_price) * state.direction * state.lots_qty
                        tp_target = risk_rub * float(cfg["tp_R"])
                        if pnl_now >= tp_target:
                            log.warning(f"TAKE-PROFIT: pnl={pnl_now:.2f} >= target={tp_target:.2f} (tp_R={cfg['tp_R']}) — закрываю маркетом")
                            ev = strategy.close_position(reason="take-profit")
                            log.warning(f"take-profit close result: {ev}")
                except Exception as te:
                    log.error(f"TP watchdog error: {te}")

            # --- F-073: WATCHDOG СТОПА — работает всегда, пока есть позиция (running/paused) ---
            if state.mode in ("running", "paused"):
                try:
                    mark, src = _live_mark()
                    if mark is not None:
                        # F-097: трейлинг-стоп в РЕАЛ-ТАЙМЕ — за живым тиком (mark), не только на закрытии H1.
                        # Работает только если trail_value задан (% / рубли / пункты); пусто = классика (за SAR на закрытии бара).
                        _tv = cfg.get("trail_value")
                        if _tv is not None and strategy and state.stop_price > 0:
                            try:
                                _dist = strategy._unit_dist(_tv, cfg.get("trail_unit"), float(mark), 0.0)
                                if _dist is not None and _dist > 0:
                                    # F-098 (модель Дмитрия): стоп = первоначальный_стоп + рост цены от входа.
                                    # Пример: вход 1000, стоп 5руб (0,5%)=995; цена +0,1% (1001) → стоп 996 (4руб).
                                    # trail_value задаёт МИНИМАЛЬНЫЙ шаг сдвига (в % / рублях / пунктах),
                                    # чтобы не двигать стоп на каждом тике. Назад стоп не откатывается.
                                    # F-145: ЛЕСТНИЦА от ПЕРВОНАЧАЛЬНОГО стопа (модель Дмитрия):
                                    # пример SHORT: initial 45.07, шаг 0.2% (0.09). Цена ушла ниже на 0.2%
                                    # → стоп 45.07−0.09=44.98; ещё на шаг → 44.89; и т.д. Ступени по шагу,
                                    # никогда не откатываются, выше initial (шорт) не поднимается.
                                    _init = float(state.initial_stop_price or 0) or float(state.stop_price or 0)
                                    if _init <= 0:
                                        _init = float(mark)
                                    _unit = (cfg.get("trail_unit") or "pct")
                                    if _unit == "pct":
                                        _step = _init * float(_tv) / 100.0
                                    elif _unit == "rub":
                                        _step = float(_tv)
                                    else:  # points: 1 пункт = 0.01 ₽ (акции MOEX)
                                        _step = float(_tv) * 0.01
                                    if _step <= 0:
                                        _step = _dist
                                                                        # F-153: БЕЗУБЫТОК — при профите >= порога стоп прыжком на entry.
                                    _bv = cfg.get("be_value")
                                    if _bv is not None and state.initial_stop_price and state.entry_price:
                                        _pnl_units = ((float(mark) - state.entry_price) / state.entry_price * 100.0) if (cfg.get("be_unit") or "pct") == "pct" else (((float(mark) - state.entry_price) * state.lots_qty) if (cfg.get("be_unit") == "rub") else ((float(mark) - state.entry_price) / 0.01))
                                        _need_be = (_pnl_units >= float(_bv)) if state.direction > 0 else (_pnl_units <= -float(_bv))
                                        if _need_be:
                                            _be_px = round(float(state.entry_price), 2)
                                            _ns_be = max(state.stop_price, _be_px) if state.direction > 0 else min(state.stop_price, _be_px)
                                            if abs(_ns_be - state.stop_price) > 1e-9:
                                                strategy._replace_broker_stop(_ns_be)
                                                state.stop_price = _ns_be
                                                state.save()
                                                log.info(f"F-153 BE: стоп -> безубыток {_ns_be:.2f} (mark={mark:.2f})")

# F-160z: ступени считаются ОТ INITIAL по движению ЦЕНЫ от INITIAL (правило Дмитрия 03.11: initial 45.07, цена ушла ниже на 0.2% -> стоп 44.98; синхронно за ценой, без отката).
                                    _eps = 0.005
                                    if state.direction > 0:
                                        _n = max(0, int((float(mark) - _init + _eps) / _step))
                                        _want = _init + _n * _step
                                        _ns = max(state.stop_price, _want)
                                    else:
                                        _n = max(0, int((_init - float(mark) + _eps) / _step))
                                        _want = _init - _n * _step
                                        _ns = min(state.stop_price, _want)
                                    if abs(_ns - state.stop_price) > 1e-9:
                                        strategy._replace_broker_stop(_ns)
                                        state.stop_price = _ns
                                        state.save()
                                        log.info(f"LIVE-TRAIL: stop -> {_ns:.4f} (mark={mark:.4f}, src={src})")
                            except Exception as _te:
                                log.error(f"live trail error: {_te}")
                        # F-113: брокер снимает заявки в конце торгового дня — проверяем и перевыставляем
                        try:
                            if orders and strategy:
                                # F-129: стоп-заявка ИСПОЛНЕНА брокером → позиция закрыта → FLAT sync
                                if state.stop_order_id and orders.is_order_filled(state.stop_order_id):
                                    # F-133: стоп реален если mark прошёл стоп-уровень (breached).
                                    # Если mark до стопа — фантомный статус чужой/старой заявки, игнор.
                                    _stop_hit = (mark <= state.stop_price) if state.direction > 0 else (mark >= state.stop_price)
                                    if not _stop_hit:
                                        log.error('F-133: стоп FILLED но mark не дошёл до стопа — фантом, игнорирую')
                                        state.stop_order_id = None
                                        state.save()
                                        continue
                                    _pnl = (state.stop_price - state.entry_price) * (-1 if state.direction < 0 else 1) * state.lots_qty
                                    try:
                                        state.last_stop_bar = str((strategy.last_stats or {}).get('last_bar') or '')
                                    except Exception:
                                        state.last_stop_bar = ''
                                    log.warning(f'F-129: стоп ИСПОЛНЕН брокером (PnL≈{_pnl:.0f}₽) — синхронизирую FLAT')
                                    try:
                                        _ca = orders.cancel_all_robot_orders()
                                        if _ca:
                                            log.info(f'F-157: снято {_ca} висячих sarema-* заявок после FLAT-sync')
                                    except Exception as _cae:
                                        log.error(f'F-157 cancel_all err: {_cae}')
                                    try:
                                        strategy.journal_close_manual(reason='stop-filled-broker',
                                                                      exit_px=state.stop_price)
                                    except Exception as _je:
                                        log.error(f'F-129 journal close err: {_je}')
                                    state.direction = 0
                                    state.lots_qty = 0
                                    state.entry_price = 0.0
                                    state.stop_price = 0.0
                                    state.initial_stop_price = 0.0
                                    state.stop_order_id = None
                                    state.tp_order_id = None
                                    state.save()
                                    continue
                                # F-113: None (не ставился/после рестарта) или снят брокером → ставим/перевыставляем
                                need_stop = (not state.stop_order_id) or orders.is_dead_order(state.stop_order_id)
                                if need_stop:
                                    log.warning('F-113: стоп-заявка отсутствует/снята — ставлю брокерский стоп')
                                    strategy._place_broker_stop(state.stop_price, state.lots_qty, state.direction)
                                    state.save()
                            if orders and (state.tp_order_id or cfg.get('tp_R')):  # F-156: TP нужен и при None (перевыставить)
                                # F-129: TP-лимитка ИСПОЛНЕНА = тейк сработал → позиция закрыта → FLAT sync
                                if state.tp_order_id and orders.is_order_filled(state.tp_order_id):  # F-156: FILLED только при живом id
                                    # F-133: подтверждение по цене — тейк реален только если mark
                                    # прошёл TP-уровень (иначе фантомный статус чужой заявки)
                                    _tp_ok = ((mark >= state.tp_price) if state.direction > 0 else (mark <= state.tp_price and state.tp_price > 0)) if state.tp_price else True
                                    if not _tp_ok:
                                        log.error('F-133: TP FILLED но mark не дошёл до TP — фантомный статус, игнорирую')
                                        state.tp_order_id = None
                                        state.save()
                                        continue
                                    log.warning('F-129: TP-лимитка ИСПОЛНЕНА брокером (тейк) — синхронизирую FLAT')
                                    # F-132: закрыть запись в торговом журнале по реальной цене TP-лимитки
                                    try:
                                        _jfl = orders.last_fill_by_comment('sarema-tp', symbol=orders._symbol)
                                        _jexit = float(_jfl.price) if (_jfl and _jfl.price > 0) else (mark or state.entry_price)
                                    except Exception:
                                        _jexit = mark or state.entry_price
                                    try:
                                        strategy.journal_close_manual(reason='take-profit-broker',
                                                                      exit_px=_jexit)
                                    except Exception as _je:
                                        log.error(f'F-129 journal close err: {_je}')
                                    state.direction = 0
                                    state.lots_qty = 0
                                    state.entry_price = 0.0
                                    state.stop_price = 0.0
                                    state.initial_stop_price = 0.0
                                    state.stop_order_id = None
                                    state.tp_order_id = None
                                    state.save()
                                    try:  # F-158: после закрытия снять ВСЕ оставшиеся заявки
                                        _ca = orders.cancel_all_robot_orders()
                                        if _ca:
                                            log.info(f'F-158: снято {_ca} заявок после TP-close')
                                    except Exception as _cae:
                                        log.error(f'F-158 cancel_all err: {_cae}')
                                    continue
                                need_tp = (not state.tp_order_id) or orders.is_dead_order(state.tp_order_id)  # F-156: None тоже перевыставляем
                                if need_tp and cfg.get('tp_R'):
                                    _tp_px = strategy.tp_price(state.direction, state.entry_price,
                                                               strategy.last_sar or state.entry_price,
                                                               (strategy.last_stats or {}).get('atr') or 0.0)
                                    if _tp_px:
                                        _close_side = 'sell' if state.direction > 0 else 'buy'
                                        # F-156: side=_close_side (строка buy/sell), а не direction (int) —
                                        # иначе place_limit трактовал -1 как SELL и F-140 блокировал TP шорта.
                                        _tpo = orders.place_limit(_close_side, state.lots_qty, round(_tp_px, 2), tag='sarema-tp')
                                        state.tp_order_id = _tpo.order_id if _tpo else None
                                        state.save()
                                        log.info(f'F-113: TP-лимитка перевыставлена @ {_tp_px:.2f}')
                        except Exception as _re_:
                            log.error(f'F-113 re-place error: {_re_}')

                        breached = bool(state.stop_price) and ((mark <= state.stop_price) if state.direction > 0 else (mark >= state.stop_price))
                        if breached:
                            # F-161: запомнить бар стопа — старые противоположные сигналы не должны
                            # открывать позицию сразу после стопа
                            try:
                                state.last_stop_bar = str((strategy.last_stats or {}).get('last_bar') or '')
                            except Exception:
                                state.last_stop_bar = ''
                            log.warning(f"STOP-HIT: mark={mark} vs stop={state.stop_price} (src={src}) — закрываю маркетом")
                            ev = strategy.close_position(reason="stop-hit")
                            log.warning(f"stop-hit close result: {ev}")
                            # продолжаем цикл (позиция закрыта, stop_order_id обнулён в close_position)
                except Exception as se:
                    log.error(f"stop watchdog error: {se}")

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
            # F-075 fix: ретрай входа при FLAT и незакрытом сигнале (entry_error/qty) —
            # не ждём новый бар: сигнал в окне age остаётся активным до исполнения.
            if state.mode == "running" and state.direction == 0:
                try:
                    ev = strategy.on_closed_bar()
                    if ev.get("events"):
                        log.info(f"retry events: {ev['events']}")
                except Exception as re_:
                    log.error(f"retry error: {re_}")
                except Exception as fe:
                    log.error(f"live feed error: {fe}")
        except Exception as e:
            log.exception(f"loop error: {e}")
        # F-107: превентивный само-перезапуск раз в 20 часов (свежий JWT из .env, свежий код)
        # F-158: периодическая чистка висячих заявок во ФЛЭТЕ (раз в ~5 мин)
        if state.mode == "running" and state.direction == 0:
            try:
                if time.time() - _last_flat_clean[0] > 300:
                    _last_flat_clean[0] = time.time()
                    _cn = orders.cancel_all_robot_orders()
                    if _cn:
                        log.info(f'F-158: ФЛЭТ-чистка сняла {_cn} висячих заявок')
            except Exception as _fce:
                log.error(f'F-158 flat clean err: {_fce}')

        if time.time() - _start_ts >= _RESTART_AFTER:
            log.warning("F-107: 20h reached — self-restart for fresh JWT")
            state.save()
            _py = _sys.executable
            _os.execl(_py, _py, "-X", "utf8", _os.path.abspath(__file__), "--port", str(PORT))

        time.sleep(20)


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
    # F-076: авто-старт 24/7 — если ⏹ не ставил флаг, робот сам возвращается в running
    if not getattr(state, "auto_start_disabled", False):
        state.mode = "running"
        state.save()
        log.info(f"AUTO-START: mode=running (dir={state.direction})")
    else:
        log.info("auto-start disabled (был ручной ⏹) — жду ▶")
    # F-114: warmup из /api/candles (Finam H1 — тот же источник, что график Дмитрия).
    # Старый M1-кэш давал SAR-траекторию, не совпадающую с графиком → «пропуски сигналов».
    warm = False
    try:
        import urllib.request as _uw
        import json as _uj
        from datetime import datetime as _dtu, timedelta as _tdd
        _url = f"http://127.0.0.1:5050/api/candles?ticker={TICKER}&tf=60&days=30"
        with _uw.urlopen(_url, timeout=20) as _r:
            _candles = _uj.loads(_r.read())
        _n = 0
        for _c in _candles:
            _bt = _dtu.utcfromtimestamp(int(_c["t"])) + _tdd(hours=3)   # UTC → МСК-naive (как F-112)
            if feed.append_closed_bar(_bt, float(_c["o"]), float(_c["h"]), float(_c["l"]),
                                      float(_c["c"]), float(_c["v"] or 0)):
                _n += 1
        warm = feed.bars is not None and len(feed.bars) >= 100
        log.info(f"F-114: warmup from /api/candles: +{_n} H1 bars")
    except Exception as _we:
        log.error(f"F-114 warmup candles failed: {_we} — fallback to csv")
        warm = feed.warmup_from_csv(TICKER) if DATA_DIR else False
    log.info(f"SAREMA {TICKER} port={PORT} warmup={'ok' if warm else 'SKIPPED'}")
    t = threading.Thread(target=_main_loop, daemon=True)
    t.start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
