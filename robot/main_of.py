"""Order Flow Robot — main entry point.

Subscribes to Trades + OrderBook + Bars via Finam WS-hub (PORT-A2),
runs OrderFlowStrategy, sends orders via DataProvider REST.

Usage: python3 main_of.py [--paper] [--port 5080]

v2 — fixed: bar callback signature, dynamic bar_start_ts, stale price check,
      entry lock reset on manual stop, config consolidation.
"""
import sys, os

# PORT-A2: bootstrap paths + .env (py4 SDK 4.3.3, robot/, project root with finam_hub.py)
import sys as _sys, os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.dirname(_HERE)
_PY4 = _os.path.join(_HERE, "py4")
for _p in (_PY4, _HERE, _PROJ):
    if _os.path.isdir(_p) and _p not in _sys.path:
        _sys.path.insert(0, _p)

def _load_env():
    for _base in (_HERE, _PROJ, _os.path.join(_PROJ, "src")):
        _p = _os.path.join(_base, ".env")
        if _os.path.exists(_p):
            with open(_p) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _, _v = _line.partition("=")
                        _os.environ.setdefault(_k.strip(), _v.strip())
_load_env()

import argparse
import json
import logging
import signal as sig_module
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from hub_adapter import FinamHubAdapter  # PORT-A2: data via WS-hub
import finam_rest4 as _rest4mod  # PORT-B2: REST 4.3.3

import config_of as config
from strategy_of import OrderFlowStrategy, OFParams, LONG, SHORT, FLAT, LotEntry
from orders_rest4 import OrderManagerRest4, get_broker_position, positions_in_sync, BUY, SELL  # PORT-B2

log = logging.getLogger("robot_of")

MSK = timezone(timedelta(hours=3))

# --- Parse args ---
parser = argparse.ArgumentParser()
parser.add_argument("--paper", action="store_true")
parser.add_argument("--port", type=int, default=5080)
args, _ = parser.parse_known_args()

# --- Logging ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_log_file = LOG_DIR / f"of_{datetime.now().strftime('%Y%m%d')}.log"



class DailyFileHandler(logging.FileHandler):
    """FileHandler с суточной ротацией по имени: of_<YYYYMMDD>.log.
    Стандартный FileHandler фиксирует имя при старте процесса — живущий через полночь
    пишет в файл вчерашнего дня (баг наблюдения 2026-08-28)."""
    def __init__(self, dir_path, prefix, encoding='utf-8'):
        self._dir = dir_path
        self._prefix = prefix
        self._current_date = None
        super().__init__(self._path_for_today(), mode='a', encoding=encoding)

    def _today(self):
        return datetime.now().strftime('%Y%m%d')

    def _path_for_today(self):
        return f"{self._dir}/{self._prefix}_{self._today()}.log"

    def emit(self, record):
        today = self._today()
        if today != self._current_date:
            self._current_date = today
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            self.baseFilename = self._path_for_today()
            self.stream = self._open()
        super().emit(record)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        DailyFileHandler(LOG_DIR, 'of'),
    ],
)

# --- Config ---
SYMBOL = getattr(config, "SYMBOL", "SiU6@RTSX")
TICKER = getattr(config, "TICKER", "SiU6")
ACCOUNT = getattr(config, "ACCOUNT_ID", os.environ.get("FINAM_ACCOUNT", "1225953"))

# === Multi-account support ===
ACCOUNTS = {
    "main": "1225953",
    "edp": "2049688",
}
ACTIVE_ACCOUNT_KEY = "main"
PORT = args.port
PAPER_MODE = args.paper

# --- Timeframe → seconds mapping for bar_start_ts ---
TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H2": 7200, "H4": 14400, "H8": 28800,
    "D": 86400,
}

# --- Strategy params (from config or of_config.json) ---
def load_params() -> OFParams:
    p = OFParams()
    # Load from config_of.py constants first
    for attr in dir(config):
        val = getattr(config, attr)
        if attr.isupper() and hasattr(p, attr.lower()):
            setattr(p, attr.lower(), val)
    # Then override from of_config.json if exists
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "of_config.json")  # FIX: не cwd
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(p, k):
                setattr(p, k, v)
    return p

params = load_params()
strategy = OrderFlowStrategy(params)

# Aggression tracking (ticks at improving prices) — module-level for callback access
_agg_buy_vol = 0
_agg_sell_vol = 0
_last_tick_price = 0.0

# --- Load active account from state file before OrderManager init ---
def _load_active_account():
    """Read activeAccount from of_state.json so it survives restarts."""
    global ACTIVE_ACCOUNT_KEY
    state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "of_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                data = json.load(f)
            saved = data.get("activeAccount")
            if saved and saved in ACCOUNTS:
                ACTIVE_ACCOUNT_KEY = saved
        except Exception:
            pass

_load_active_account()

orders = OrderManagerRest4(
    account=ACCOUNTS[ACTIVE_ACCOUNT_KEY], symbol=SYMBOL,
    paper=PAPER_MODE, rest4=_rest4mod,
    quote_provider=lambda: getattr(strategy, '_current_price', 0) or 0,
)
log.info(f"Trading account: {ACTIVE_ACCOUNT_KEY} ({ACCOUNTS[ACTIVE_ACCOUNT_KEY]})")

# Fill subscription thread tracking (for watchdog)
_fill_sub_thread: threading.Thread | None = None

# --- Data connection (PORT-A2: WS-hub adapter) ---
_hub_adapter = None
_running = True
_mode = "stopped"  # stopped, running, paused
_last_fp_reconnect: float = 0.0  # guard against reconnect loop

# --- Current price (thread-safe via lock) ---
_price_lock = threading.Lock()
_current_price: float = 0.0

# --- State persistence ---
STATE_FILE = os.path.join(os.getcwd(), "of_state.json")

def save_state():
    try:
        state = strategy.get_state()
        state["mode"] = _mode
        state["activeAccount"] = ACTIVE_ACCOUNT_KEY
        state["direction_filter"] = params.direction_filter
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Save state error: {e}")

def load_state_from_disk():
    global _mode
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            strategy.load_state(state)
            _mode = state.get("mode", "stopped")
            params.direction_filter = state.get("direction_filter", "both")
            log.info(f"State loaded: dir={state.get('dir', 0)} lots={state.get('totalLots', 0)} mode={_mode} dirFilter={params.direction_filter}")
        except Exception as e:
            log.error(f"Load state error: {e}")


# ========== Hub subscriptions (PORT-A2) ==========

def connect_finam():
    """PORT-A2: subscribe via WS-hub (replaces 5 gRPC subscription threads)."""
    global _hub_adapter
    token = os.environ.get("FINAM_API_KEY")
    if not token:
        log.error("FINAM_API_KEY not set!")
        return False

    # PORT-B2 preview: REST4 probe (non-fatal)
    try:
        _acc = _rest4mod.get_account_info(ACCOUNTS[ACTIVE_ACCOUNT_KEY])
        log.info(f"REST4 OK: account {_acc.get('account_id')}, equity={_acc.get('equity')}")
    except Exception as e:
        log.warning(f"REST4 account probe failed (non-fatal): {str(e)[:100]}")

    # Data: WS-hub adapter
    _hub_adapter = FinamHubAdapter(SYMBOL)
    _hub_adapter.start(
        on_trades=_on_latest_trades,
        on_order_book=_on_order_book,
        on_quote=_on_quote,
        on_bar=_on_new_bar,
        timeframe=params.timeframe,
    )
    log.info(f"HubAdapter: subscriptions started ({SYMBOL}, TF={params.timeframe})")
    return True


# ========== Callbacks ==========

def _on_latest_trades(event):
    """Callback from SubscribeLatestTrades."""
    try:
        from orderflow_engine import Trade, SIDE_BUY, SIDE_SELL
        for trade in event.trades:
            price = float(trade.price.value) if hasattr(trade.price, "value") else float(trade.price)
            size = int(float(trade.size.value)) if hasattr(trade.size, "value") else int(float(trade.size))
            side_val = int(trade.side)  # 1=BUY, 2=SELL from Finam
            mapped_side = SIDE_BUY if side_val == 1 else SIDE_SELL

            ts = time.time()
            if hasattr(trade.timestamp, "seconds"):
                ts = trade.timestamp.seconds

            strategy.trades.add_trade(Trade(
                price=price,
                size=size,
                side=mapped_side,
                timestamp=ts,
            ))

            # Feed Volume Profile
            if strategy.vp:
                strategy.vp.add_trade(price, size, datetime.now())
                strategy.vp.calculate()

            # Update trade stream health
            strategy._last_trade_ts = time.time()

            # Track aggression (ticks at improving prices)
            global _agg_buy_vol, _agg_sell_vol, _last_tick_price
            if _last_tick_price > 0 and price != _last_tick_price:
                if price > _last_tick_price:
                    _agg_buy_vol += size
                elif price < _last_tick_price:
                    _agg_sell_vol += size
            _last_tick_price = price
    except Exception as e:
        log.error(f"Trades callback error: {e}")


def _on_order_book(event):
    """Callback from SubscribeOrderBook."""
    try:
        rows = []
        for ob in event.order_book:
            for r in ob.rows:
                price = _to_float(r.price)
                buy = _to_float(r.buy_size)
                sell = _to_float(r.sell_size)
                action = int(r.action)
                rows.append((price, int(buy), int(sell), action))

        if rows:
            strategy.ob_tracker.update(rows)
    except Exception as e:
        log.error(f"OB callback error: {e}")


def _on_new_bar(event, finam_timeframe=None):
    """Callback from hub SubscribeBars — bar closed.
    Note: hub adapter passes (event, finam_timeframe) — accept both.
    """
    try:
        # Calculate bar duration from timeframe
        bar_secs = TF_SECONDS.get(params.timeframe, 300)

        for bar in event.bars:
            o = _to_float(bar.open)
            h = _to_float(bar.high)
            l = _to_float(bar.low)
            c = _to_float(bar.close)
            v = _to_float(bar.volume)

            ts = time.time()
            if hasattr(bar.timestamp, "seconds"):
                ts = bar.timestamp.seconds

            # Close bar metrics from accumulated trades — use real bar duration
            metrics = strategy.trades.close_bar(
                bar_open=o,
                bar_close=c,
                bar_start_ts=ts - bar_secs,
                bar_end_ts=ts,
            )

            # Feed aggression data to signal engine
            global _agg_buy_vol, _agg_sell_vol
            strategy.signals.add_bar_aggression(_agg_buy_vol, _agg_sell_vol)
            _agg_buy_vol = 0
            _agg_sell_vol = 0

            strategy.on_bar_close(metrics, h, l, c)
            log.debug(f"Bar close: O={o:.0f} H={h:.0f} L={l:.0f} C={c:.0f} V={v:.0f} delta={metrics.delta} cvd={metrics.cvd:.0f}")
    except Exception as e:
        log.error(f"Bar callback error: {e}")


def _on_quote(event):
    """Callback from SubscribeQuote — update current price."""
    try:
        for q in event.quote:
            bid = _to_float(q.bid)
            ask = _to_float(q.ask)
            last = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)

            if last > 0:
                _set_current_price(last)
                strategy.update_price(last)
    except Exception as e:
        log.error(f"Quote callback error: {e}")


# ========== Main loop ==========

def _to_float(val) -> float:
    """Safely convert Decimal/string/None to float."""
    if val is None or val == "":
        return 0.0
    if hasattr(val, "value"):
        s = val.value
        return float(s) if s else 0.0
    return float(val)


def _reconnect_finampy():
    """Watchdog: reconnect hub subscriptions (PORT-A2 lab pattern)."""
    global _last_fp_reconnect
    now = time.time()
    if now - _last_fp_reconnect < 30:
        return  # don't reconnect more than once per 30s
    _last_fp_reconnect = now
    log.warning("[WATCHDOG] Price stale - reconnecting hub...")
    try:
        if _hub_adapter is not None:
            try:
                _hub_adapter.stop()
            except Exception:
                pass
        time.sleep(1)
        ok = connect_finam()
        if ok:
            log.info("[WATCHDOG] Hub resubscribed OK")
        else:
            log.error("[WATCHDOG] Hub reconnect failed")
    except Exception as e:
        log.error(f"[WATCHDOG] Reconnect error: {e}")


def _set_current_price(price: float):
    """Thread-safe price update."""
    global _current_price
    with _price_lock:
        _current_price = price


def main_loop():
    """Main strategy loop — poll price + process ticks."""
    global _mode, _fill_sub_thread

    log.info("Main loop started")
    last_save = time.time()
    last_price_sync = 0.0

    while _running:
        try:
            if _mode == "paused":
                # PAUSE-SEMANTICS: не открываем НОВЫЕ позиции, но УПРАВЛЯЕМ открытыми
                # (close_all/partial_tp/stop-loss исполняются; entry/average/pyramid блокируются)
                with _price_lock:
                    price = _current_price
                if price > 0:
                    try:
                        actions = strategy.process_tick(price, datetime.now(MSK))
                        for action in actions:
                            act = action.get("action")
                            if act in ("entry", "average", "pyramid"):
                                continue  # пауза: новые позиции запрещены
                            _execute_action(action)
                    except Exception as e:
                        log.warning(f"paused tick error: {e}")
                time.sleep(1)
                continue

            if _mode != "running":
                time.sleep(1)
                continue

            # Get current price
            with _price_lock:
                price = _current_price

            if price <= 0:
                # Try fallback: REST 4.3.3 last quote (PORT-B2)
                try:
                    q = _rest4mod.get_last_quote(SYMBOL)
                    _q = q.get("quote", {}) if isinstance(q, dict) else {}
                    _l = _q.get("last", {})
                    price = float(_l.get("value", 0)) if isinstance(_l, dict) else float(_l or 0)
                    if price > 0:
                        _set_current_price(price)
                        strategy.update_price(price)
                except Exception:
                    pass

            if price <= 0:
                time.sleep(0.5)
                continue

            now = datetime.now(MSK)

            # Process tick
            actions = strategy.process_tick(price, now)

            # Execute actions
            for action in actions:
                _execute_action(action)
                # Update strategy prices with REAL broker fill price
                fill_price = action.get("fill_price", 0)
                if fill_price > 0:
                    strategy.update_fill_price(fill_price, action.get("action", ""))

            # === VAH/VAL RANGE BREAKOUT STOP ===
            # If price exits VA and we have a position → hard close all
            if strategy.in_position and strategy.is_outside_va(price):
                vah = strategy.vp.vah if strategy.vp else 0
                val = strategy.vp.val if strategy.vp else 0
                log.warning(f"VA BREAKOUT STOP: price={price:.0f} outside VA (VAL={val:.0f}..VAH={vah:.0f}) — closing all")
                if not PAPER_MODE:
                    side = SELL if strategy.direction == LONG else BUY
                    try:
                        orders.place_market(side, strategy.total_lots, tag="va_stop")
                        time.sleep(0.3)
                    except Exception as e:
                        log.error(f"VA BREAKOUT STOP: order failed: {e}")
                strategy._close_all(price, "va_breakout_stop")
                strategy._reset_position()
                save_state()

            # Periodic state save (every 30 sec)
            if time.time() - last_save > 30:
                save_state()
                last_save = time.time()

            # === BROKER PRICE SYNC: avg_price + current_price only ===
            # Does NOT touch position state (lots, dir, lot_queue)
            if time.time() - last_price_sync > 30.0:
                try:
                    sync_account = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
                    bd = get_broker_position(_rest4mod, sync_account, SYMBOL)
                    if bd is not None:
                        strategy.sync_from_broker(bd)
                        # Passive desync monitor (dir-aware; None = API упал, НЕ тревога)
                        ok, msg = positions_in_sync(strategy._total_lots, strategy._dir, bd)
                        if not ok:
                            log.warning(f"DESYNC: {msg} avg_robot={strategy._avg_price:.0f} avg_broker={bd.get('avg_price',0):.0f} — manual fix needed")
                    else:
                        log.debug("Price sync: broker position unavailable (API) — desync check skipped")
                except Exception as e:
                    log.debug(f"Price sync: {e}")
                last_price_sync = time.time()

            # === WATCHDOG: reconnect hub if price stale > 60s ===
            if strategy._is_price_stale(max_age_sec=60):
                _reconnect_finampy()

            # === WATCHDOG: reconnect if CVD/LatestTrades stream dead > 120s ===
            # Price can survive via Quotes while trade stream dies silently
            if hasattr(strategy, '_last_trade_ts') and strategy._last_trade_ts > 0:
                trade_age = time.time() - strategy._last_trade_ts
                if trade_age > 120:
                    log.warning(f"[WATCHDOG] LatestTrades stream dead for {trade_age:.0f}s — reconnecting")
                    _reconnect_finampy()

            # === WATCHDOG: re-subscribe fill stream if thread died ===
            # PORT-A2: fill-channel = WS ORDERS (real-mode, post-F decision);
            # paper-mode fills are simulated by OrderManagerRest4 (PORT-B2)
            pass

            # Tick rate: 50ms (20x per second)
            time.sleep(0.05)

        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(1)

    log.info("Main loop stopped")


def _record_broker_trade(action: dict, fill_price: float):
    """Record trade using ONLY real broker fill prices. PnL = (exit - entry) * dir * lots - comm."""
    act = action.get("action")
    comm_per_lot = strategy.p.commission * 2  # round-trip

    if act == "partial_tp":
        entry_price = action.get('entryPrice', 0)
        entry_side = action.get('entrySide', strategy._dir)
        lots = action.get('qty', 1)
        pnl = (fill_price - entry_price) * entry_side * lots - comm_per_lot * lots
        direction = 'LONG' if entry_side == 1 else 'SHORT'
    elif act == "close_all":
        entry_price = action.get('avgPrice', strategy._avg_price)
        entry_side = strategy._dir
        lots = action.get('qty', strategy.total_lots)
        pnl = (fill_price - entry_price) * entry_side * lots - comm_per_lot * lots
        direction = 'LONG' if entry_side == 1 else 'SHORT'
    else:
        return

    pnl = round(pnl, 2)
    strategy._trade_history.append({
        'entryPrice': round(entry_price, 2),
        'exitPrice': round(fill_price, 2),
        'direction': direction,
        'lots': lots,
        'pnl': pnl,
        'entryTime': strategy._entry_time.isoformat() if strategy._entry_time else None,
        'exitTime': datetime.now(MSK).isoformat(),
        'reason': action.get('reason', 'partial_tp'),
        'signal': action.get('signal', strategy._signal_type),
    })
    strategy._realized_pnl += pnl
    today = datetime.now(MSK).strftime('%Y-%m-%d')
    if strategy._daily_pnl_date == today:
        strategy._daily_pnl += pnl
    else:
        strategy._daily_pnl = pnl
        strategy._daily_pnl_date = today
    log.info(f"TRADE {direction} {lots}L entry={entry_price:.0f} exit={fill_price:.0f} pnl={pnl:+.1f}₽ comm={comm_per_lot * lots:.1f}₽")


def _get_broker_position_fast():
    """Broker |lots| via REST 4.3.3; None = позиция НЕИЗВЕСТНА (API failed) (PORT-B2)."""
    try:
        acc = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
        bd = get_broker_position(_rest4mod, acc, SYMBOL)
        if bd is not None:
            return bd.get('lots', 0)
    except Exception:
        pass
    return None


def _execute_action(action: dict):
    """Execute a strategy action via OrderManager (or log only in paper mode)."""
    act = action.get("action")
    side_str = action.get("side", "buy")
    qty = action.get("qty", 1)
    tag = f"of_{act}"

    if PAPER_MODE:
        log.info(f"📄 PAPER {act}: {side_str} {qty} @ {action.get('price', 0):.0f}")
        # PORT-FIX (paper-исполнение): применяем действие к стратегии, иначе позиция из
        # state никогда не закрывается → вечный CLOSE_ALL-цикл (баг найден в soak 20.08)
        action["fill_price"] = action.get("price", 0) or strategy._current_price
        if act == "close_all":
            action["avgPrice"] = strategy._avg_price
            _record_broker_trade(action, action["fill_price"])
            strategy._reset_position()
        elif act == "partial_tp":
            _record_broker_trade(action, action["fill_price"])
        else:  # entry / average / pyramid
            strategy.update_fill_price(action["fill_price"], act)
        return

    if act == "close_all":
        # Skip if already flat (partial_tp closed last lot in same tick)
        if strategy._total_lots <= 0:
            log.info(f"CLOSE_ALL skipped — already flat (partial_tp closed in same tick)")
            return

        # First cancel all orders
        active = orders.get_active_orders(symbol=SYMBOL)
        if active:
            for o in active:
                oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                orders.cancel(oid)
            time.sleep(0.5)

        # Save avgPrice BEFORE strategy modifies it
        avg_for_record = strategy._avg_price

        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag=f"of_close_{action.get('reason', '')}")
        if result:
            fill_price = orders.wait_fill(result.order_id, timeout=3.0) or 0.0
            if fill_price > 0:
                action["fill_price"] = fill_price
                action['avgPrice'] = avg_for_record
                log.info(f"Executed CLOSE_ALL: {side_str} {qty} @ {fill_price:.0f} reason={action.get('reason')}")
                _record_broker_trade(action, fill_price)
                strategy._reset_position()
            else:
                log.warning(f"CLOSE_ALL: no broker fill for {side_str} {qty} — recording with strategy price {action.get('price', 0):.0f}")
                action['avgPrice'] = avg_for_record
                _record_broker_trade(action, action.get('price', strategy._current_price))
                strategy._reset_position()
        else:
            # DP failed — verify with broker before deciding
            lots_before = strategy._total_lots
            time.sleep(3)
            broker_lots = _get_broker_position_fast()
            if broker_lots is None:
                log.error(f"CLOSE_ALL: order placement failed for {side_str} {qty} AND broker unreadable — keeping position, will retry next tick")
            elif broker_lots < lots_before:
                log.warning(f"CLOSE_ALL: DP failed but broker lots changed ({lots_before}→{broker_lots}) — order executed, resetting")
                action['avgPrice'] = avg_for_record
                _record_broker_trade(action, strategy._current_price)
                strategy._reset_position()
            else:
                log.error(f"CLOSE_ALL: order placement failed for {side_str} {qty} — keeping position, will retry next tick")

    elif act in ("entry", "average", "pyramid"):
        side_int = BUY if side_str == "buy" else SELL
        lots_before = strategy._total_lots - qty  # lots BEFORE this action added them
        result = orders.place_market(side_int, qty, tag=tag)
        if result:
            fill_price = orders.wait_fill(result.order_id, timeout=3.0) or 0.0
            if fill_price > 0:
                action["fill_price"] = fill_price
                log.info(f"Executed {act.upper()}: {side_str} {qty} @ {fill_price:.0f}")
            else:
                log.info(f"Executed {act.upper()}: {side_str} {qty} @ {action.get('price', 0):.0f}")
        else:
            # DP failed — verify with broker before rollback
            time.sleep(3)
            broker_lots = _get_broker_position_fast()
            if broker_lots is None:
                log.error(f"{act.upper()}: order placement failed for {side_str} {qty} AND broker unreadable — keeping lots, DESYNC monitor will verify")
            elif broker_lots != lots_before:
                log.warning(f"{act.upper()}: DP failed but broker lots changed ({lots_before}→{broker_lots}) — order executed, keeping lots")
            else:
                log.error(f"{act.upper()}: order placement FAILED for {side_str} {qty} — rolling back {qty} lot(s)")
                strategy.rollback_pending_entry(qty)

    elif act == "partial_tp":
        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag="of_partial_tp")
        if result:
            fill_price = orders.wait_fill(result.order_id, timeout=3.0) or 0.0
            if fill_price > 0:
                action["fill_price"] = fill_price
                log.info(f"Executed PARTIAL_TP: {side_str} {qty} @ {fill_price:.0f}")
                _record_broker_trade(action, fill_price)
            else:
                log.warning(f"PARTIAL_TP: no broker fill for {side_str} {qty} — recording with strategy price {action.get('price', 0):.0f}")
                _record_broker_trade(action, action.get('price', strategy._current_price))
        else:
            # DP failed — verify with broker before re-adding lot
            expected_lots = strategy._total_lots  # already decremented by strategy
            time.sleep(3)
            broker_lots = _get_broker_position_fast()
            if broker_lots is None:
                log.error(f"PARTIAL_TP: order placement failed for {side_str} {qty} AND broker unreadable — lot stays removed, DESYNC monitor will verify")
            elif broker_lots < expected_lots + qty:
                log.warning(f"PARTIAL_TP: DP failed but broker lots changed — order executed, lot stays removed")
            else:
                log.error(f"PARTIAL_TP: order placement failed for {side_str} {qty} — re-adding lot to queue")
                strategy._lot_queue.append(strategy.LotEntry(price=action.get('entryPrice', strategy._current_price), side=action.get('entrySide', strategy._dir), lots=qty))
                strategy._total_lots += qty
                if strategy._total_lots > 0 and strategy._dir == FLAT:
                    strategy._dir = action.get('entrySide', 0)


# ========== API ==========

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress default logging

    def _json(self, code: int, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        global _mode
        with _price_lock:
            price = _current_price
        # Parse full path with query params
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path  # Extract path without query params
        params_url = parse_qs(parsed.query)

        if path == "/status":
            status = strategy.get_status()
            status["mode"] = _mode
            status["symbol"] = SYMBOL
            status["currentPrice"] = price
            status["params"] = {k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}
            status["paper"] = PAPER_MODE
            status["accounts"] = ACCOUNTS
            status["activeAccount"] = ACTIVE_ACCOUNT_KEY
            status["activeAccountId"] = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
            status["directionFilter"] = params.direction_filter
            
            # Filter tradeHistory by date period
            start_date = params_url.get('startDate', [None])[0]
            end_date = params_url.get('endDate', [None])[0]
            all_trades = params_url.get('all', ['false'])[0].lower() == 'true'
            
            # Log the request
            log.info(f"API GET /status with query: {parsed.query} | filters: startDate={start_date}, endDate={end_date}, all={all_trades}")
            
            original_trades = status.get("tradeHistory", [])
            
            if all_trades:
                # Return all trades without filtering
                status["tradeHistory"] = original_trades
                status["tradesFiltered"] = False
                status["filters"] = {}
                log.info(f"Returning all {len(original_trades)} trades (all=true)")
            elif start_date or end_date:
                # Filter by date
                filtered_trades = []
                for trade in original_trades:
                    trade_date = trade["entryTime"][:10]  # Extract YYYY-MM-DD
                    if start_date and trade_date < start_date:
                        continue
                    if end_date and trade_date > end_date:
                        continue
                    filtered_trades.append(trade)
                
                status["tradeHistory"] = filtered_trades
                status["tradesFiltered"] = True
                status["filters"] = {"startDate": start_date, "endDate": end_date}
                log.info(f"Filtered trades: {len(filtered_trades)} from {len(original_trades)} (startDate={start_date}, endDate={end_date})")
            else:
                # No filters - return all trades
                status["tradeHistory"] = original_trades
                status["tradesFiltered"] = False
                status["filters"] = {}
                log.info(f"No filters applied, returning all {len(original_trades)} trades")
            
            self._json(200, status)

        elif path == "/account":
            self._json(200, {
                "accounts": ACCOUNTS,
                "active": ACTIVE_ACCOUNT_KEY,
                "accountId": ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
            })

        elif path == "/health":
            self._json(200, {"ok": True, "symbol": SYMBOL, "mode": _mode})

        elif path == "/start":
            _mode = "running"
            strategy._force_unlock()  # Reset entry lock on manual start
            save_state()
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/sync":
            # GET /sync?lots=N — sync internal lot count with broker
            try:
                qs = self.path.split("?")[1] if "?" in self.path else ""
                sync_params = {}
                for pair in qs.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        sync_params[k] = v
                broker_lots = int(sync_params.get("lots", "0"))
            except (ValueError, IndexError):
                self._json(400, {"error": "lots param required (integer)"})
                return
            old_lots = strategy.total_lots
            strategy.sync_lot_count(broker_lots)
            save_state()
            self._json(200, {
                "ok": True,
                "old_lots": old_lots,
                "new_lots": strategy.total_lots,
                "avg_price": strategy.avg_price,
                "avg_levels": strategy.get_status()["averageLevels"],
            })

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global _mode, orders, ACTIVE_ACCOUNT_KEY
        path = self.path.split("?")[0]

        if path == "/start":
            _mode = "running"
            strategy._force_unlock()  # Reset entry lock on manual start
            save_state()
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/stop":
            # Stop = cancel orders + close position
            _mode = "stopped"
            if not PAPER_MODE:
                active = orders.get_active_orders(symbol=SYMBOL)
                if active:
                    for o in active:
                        oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                        orders.cancel(oid)
            # Close position if any
            if strategy.in_position:
                with _price_lock:
                    price = _current_price
                if price > 0:
                    if not PAPER_MODE:
                        side = SELL if strategy.direction == LONG else BUY
                        orders.place_market(side, strategy.total_lots, tag="of_stop")
                    strategy._close_all(price, "manual_stop")
                    strategy._reset_position()
            strategy._force_unlock()  # Reset entry lock after manual stop
            save_state()
            self._json(200, {"ok": True, "mode": _mode, "paper": PAPER_MODE})

        elif path == "/pause":
            # Pause = cancel orders, keep position
            _mode = "paused"
            if not PAPER_MODE:
                active = orders.get_active_orders(symbol=SYMBOL)
                if active:
                    for o in active:
                        oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                        orders.cancel(oid)
            save_state()
            self._json(200, {"ok": True, "mode": _mode, "paper": PAPER_MODE})

        elif path == "/position/adjust":
            # MANUAL POSITION ADJUST: синхронизация state с брокером БЕЗ ордеров.
            # POST {lots: int со знаком (абсолютная позиция), price: float}
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                try:
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    lots = int(data.get("lots", 0))
                    price = float(data.get("price", 0) or 0)

                    if lots != 0 and price <= 0:
                        self._json(400, {"error": "при lots!=0 требуется price > 0"})
                        return
                    if abs(lots) > 100:
                        self._json(400, {"error": "|lots| > 100 — подозрительно много"})
                        return
                    if strategy._is_entry_locked():
                        self._json(409, {"error": "entry lock активен (недавний вход) — повторите через ~60с"})
                        return

                    new_dir = (1 if lots > 0 else -1) if lots != 0 else 0
                    abs_lots = abs(lots)

                    if lots == 0:
                        strategy._reset_position()
                        log.info("MANUAL ADJUST: FLAT (позиция обнулена, ордеров НЕ было)")
                    else:
                        strategy._dir = new_dir
                        strategy._total_lots = abs_lots
                        strategy._entry_price = price
                        strategy._avg_price = price
                        strategy._last_average_price = 0.0
                        strategy._last_pyramid_price = 0.0
                        strategy._peak_lots = abs_lots
                        strategy._entry_time = datetime.now(MSK)
                        strategy._lot_queue.clear()
                        strategy._lot_queue.append(LotEntry(
                            price=price, side=new_dir, lots=abs_lots, added_ts=time.monotonic()))
                        log.info(f"MANUAL ADJUST: dir={'LONG' if new_dir == 1 else 'SHORT'} lots={abs_lots} @ {price:.0f} (ордеров НЕ было)")
                    save_state()
                    self._json(200, {"ok": True, "dir": strategy._dir, "lots": strategy._total_lots,
                                     "avgPrice": strategy._avg_price, "mode": _mode})
                except (ValueError, TypeError) as e:
                    self._json(400, {"error": f"Некорректные данные: {e}"})
            else:
                self._json(400, {"error": "Missing request body"})

        elif path == "/trades":
            # Filter tradeHistory by date period (POST with JSON body)
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                try:
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    start_date = data.get('startDate')
                    end_date = data.get('endDate')
                    
                    status = strategy.get_status()
                    original_trades = status.get("tradeHistory", [])
                    
                    if start_date or end_date:
                        filtered_trades = []
                        for trade in original_trades:
                            # Filter by exitTime (when trade was actually closed)
                            exit_date = trade.get("exitTime", "")[:10]
                            entry_date = trade["entryTime"][:10]
                            # Include trade if either entry or exit falls within range
                            if start_date and exit_date < start_date and entry_date < start_date:
                                continue
                            if end_date and exit_date > end_date and entry_date > end_date:
                                continue
                            filtered_trades.append(trade)
                        
                        self._json(200, {
                            "trades": filtered_trades,
                            "count": len(filtered_trades),
                            "totalCount": len(original_trades),
                            "filtered": True,
                            "filters": {"startDate": start_date, "endDate": end_date}
                        })
                    else:
                        self._json(200, {
                            "trades": original_trades,
                            "count": len(original_trades),
                            "filtered": False
                        })
                except Exception as e:
                    self._json(400, {"error": str(e)})
            else:
                self._json(400, {"error": "Missing request body"})

        elif path == "/account":
            # Switch active trading account (accepts key or account ID)
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                account_val = data.get("account", "main")
                # Resolve: try as key first, then as ID
                account_key = account_val if account_val in ACCOUNTS else None
                if not account_key:
                    for k, v in ACCOUNTS.items():
                        if v == account_val:
                            account_key = k
                            break
                new_account = ACCOUNTS.get(account_key)
                if new_account:
                    ACTIVE_ACCOUNT_KEY = account_key
                    orders = OrderManagerRest4(
                        account=new_account, symbol=SYMBOL,
                        paper=PAPER_MODE, rest4=_rest4mod,
                        quote_provider=lambda: getattr(strategy, '_current_price', 0) or 0,
                    )
                    log.info(f"Account switched to {account_key} ({new_account})")
                    save_state()
                    self._json(200, {"ok": True, "account": account_key, "id": new_account})
                else:
                    self._json(400, {"error": f"Unknown account: {account_val}", "available": list(ACCOUNTS.keys())})
            else:
                self._json(400, {"error": "Missing request body"})

        elif path == "/load-state":
            load_state_from_disk()
            log.info("State reloaded from disk")
            self._json(200, {"ok": True, "mode": _mode, "dir": strategy.dir(), "lots": strategy.total_lots()})

        elif path == "/params":
            # Update parameters (FIX: стабильный cfg-путь + защита от исключений)
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                try:
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    for k, v in data.items():
                        if hasattr(params, k):
                            setattr(params, k, v)
                            log.info(f"Param updated: {k} = {v}")
                    # Reconstruct VWEMA if toggle changed
                    if "use_vwema" in data or any(k.startswith("vwema_") for k in data):
                        strategy._init_vwema()
                    # Reconstruct VP if VAH/VAL params changed
                    if "use_vah_val" in data or any(k.startswith("vah_val_") for k in data):
                        strategy._init_vp()
                    # Sync agg engine params (Si-specific)
                    if "agg_window" in data:
                        strategy.signals.set_agg_window(data["agg_window"])
                    if "agg_ratio_threshold" in data:
                        strategy.signals._agg_ratio_threshold = data["agg_ratio_threshold"]
                    if "use_agg_ratio" in data:
                        strategy.signals._use_agg_ratio = data["use_agg_ratio"]
                    # Save to config (FIX: путь от папки робота, не cwd)
                    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "of_config.json")
                    with open(cfg_path, "w") as f:
                        json.dump({k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}, f, indent=2)
                    self._json(200, {"ok": True})
                except Exception as e:
                    log.error(f"/params error: {e}")
                    self._json(400, {"error": str(e)[:200]})
            else:
                self._json(400, {"error": "Missing request body"})

        elif path == "/reset-stats":
            strategy._realized_pnl = 0.0
            strategy._trade_history = []
            strategy._round_trips = 0
            strategy._daily_pnl = 0.0
            strategy._daily_pnl_date = None
            save_state()
            log.info("Statistics reset: realizedPnl=0, trades=0, roundTrips=0")
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


# ========== VWEMA Warmup ==========

def _warmup_vwema():
    """Fetch historical bars via REST 4.3.3 and warm up VWEMA filter (PORT-B2, lab pattern)."""
    if not strategy.vwema:
        return

    try:
        # REST 4.3.3 bars
        tf_map = {"M1": "TIME_FRAME_M1", "M5": "TIME_FRAME_M5",
                  "M15": "TIME_FRAME_M15", "M30": "TIME_FRAME_M30"}
        ws_tf = tf_map.get(params.timeframe, "TIME_FRAME_M1")

        bar_seconds = TF_SECONDS.get(params.timeframe, 60)
        bars_needed = params.vwema_slow + 20
        lookback_seconds = bars_needed * bar_seconds

        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=lookback_seconds)

        resp = _rest4mod.get_bars(SYMBOL, ws_tf,
                                  start.isoformat().replace("+00:00", "Z"),
                                  now.isoformat().replace("+00:00", "Z"))

        bars = resp.get("bars", []) if isinstance(resp, dict) else []
        if bars:
            fed = 0
            for bar in bars:
                def _dv(x):
                    return float(x.get("value", 0)) if isinstance(x, dict) else float(x or 0)
                h, l, c, v = _dv(bar.get("high")), _dv(bar.get("low")), _dv(bar.get("close")), _dv(bar.get("volume"))
                if c > 0:
                    strategy.vwema.update(c, h, l, volume=v)
                    fed += 1
            state = strategy.vwema.state
            log.info(f"VWEMA warmup: {fed} bars fed | ready={strategy.vwema.ready} | dir={state['direction']} | ema_f={state['ema_f']} ema_s={state['ema_s']} atr={state['atr']}")
        else:
            log.warning("VWEMA warmup: no historical bars received")
    except Exception as e:
        log.error(f"VWEMA warmup error: {e}", exc_info=True)


# ========== Shutdown ==========

def on_shutdown(signum, frame):
    global _running
    log.info(f"Signal {signum} received — shutting down...")
    _running = False
    global _mode
    _mode = "stopped"
    save_state()
    if _hub_adapter:
        try:
            _hub_adapter.stop()
        except Exception:
            pass
    sys.exit(0)


sig_module.signal(sig_module.SIGTERM, on_shutdown)
sig_module.signal(sig_module.SIGINT, on_shutdown)


# ========== Start ==========

if __name__ == "__main__":
    log.info(f"=== Order Flow Robot v2 ===")
    log.info(f"Symbol: {SYMBOL} | Account: {ACCOUNT} | Port: {PORT}")
    log.info(f"Params: lots={params.lots} stepAvg={params.step_average} stepPyr={params.step_pyramid}")
    log.info(f"  maxAvg={params.max_average_levels} maxPyr={params.max_pyramid_levels} spread={params.spread}")
    log.info(f"  partialTP={params.partial_tp} SL={params.stop_loss_mode}/{params.stop_loss_value}")
    log.info(f"  timeframe={params.timeframe}")

    # Load state
    load_state_from_disk()

    # Connect data hub
    if not connect_finam():
        log.error("Failed to connect hub - exiting")
        sys.exit(1)

    # VWEMA warmup: load historical bars so filter is ready immediately
    _warmup_vwema()

    # Warmup period (let subscriptions accumulate data)
    log.info("Warmup: waiting 10 sec for data streams...")
    time.sleep(10)
    log.info(f"Warmup done. OB has data: {strategy.ob_tracker.has_data}")

    # === STARTUP RECONCILIATION: check broker position once (PORT-B2: REST) ===
    try:
        _sync_acc = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
        _bpos = get_broker_position(_rest4mod, _sync_acc, SYMBOL)
        if _bpos is None:
            log.warning("STARTUP: broker position check failed (API) — skipping reconciliation, DESYNC monitor will verify")
        else:
            _bl = _bpos.get('lots', 0)
            _bdir = _bpos.get('dir', 0)
            if _bl != 0:
                _side = "long" if _bdir > 0 else "short"
                log.warning(f"STARTUP: broker has {_bl} {_side} lot(s) (avg={_bpos.get('avg_price', 0):.0f}) but robot is FLAT. NOT entering — investigate manually.")
                _mode = "stopped"
                save_state()
    except Exception as _e:
        log.warning(f"STARTUP: broker position check failed: {_e}")

    # Start main loop    # Start main loop
    t_main = threading.Thread(target=main_loop, daemon=True, name="main-loop")
    t_main.start()

    # Start HTTP server with SO_REUSEADDR to prevent "Address already in use" on restart
    import socket
    HTTPServer.address_family = socket.AF_INET
    HTTPServer.socket_type = socket.SOCK_STREAM
    import socketserver

    class ReusableHTTPServer(socketserver.ThreadingMixIn, HTTPServer):  # PORT-FIX: threaded API
        allow_reuse_address = True
    server = ReusableHTTPServer(("0.0.0.0", PORT), APIHandler)
    log.info(f"API listening on :{PORT}")
    log.info(f"Endpoints: GET /status | GET /health | POST /start /stop /pause /params")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        on_shutdown(None, None)
