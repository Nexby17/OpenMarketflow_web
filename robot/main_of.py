"""Order Flow Robot — main entry point.

Subscribes to Trades + OrderBook + Bars via FinamPy gRPC,
runs OrderFlowStrategy, sends orders via DataProvider REST.

Usage: python3 main_of.py [--paper] [--port 5080]

v2 — fixed: bar callback signature, dynamic bar_start_ts, stale price check,
      entry lock reset on manual stop, config consolidation.
"""
import sys, os
import argparse
import json
import logging
import signal as sig_module
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from FinamPy import FinamPy

import config_of as config
from strategy_of import OrderFlowStrategy, OFParams, LONG, SHORT, FLAT
from orders_dp import OrderManager, BUY, SELL

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, mode='a', encoding='utf-8'),
    ],
)

# --- Config ---
SYMBOL = getattr(config, "SYMBOL", "SiU6@RTSX")
TICKER = getattr(config, "TICKER", "SiU6")
ACCOUNT = getattr(config, "ACCOUNT_ID", os.environ.get("FINAM_ACCOUNT", "1225953"))
DP_URL = getattr(config, "DP_URL", "http://localhost:5060")

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
    cfg_path = os.path.join(os.getcwd(), "of_config.json")
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

orders = OrderManager(dp_url=DP_URL, account=ACCOUNTS[ACTIVE_ACCOUNT_KEY], symbol=SYMBOL)
log.info(f"Trading account: {ACTIVE_ACCOUNT_KEY} ({ACCOUNTS[ACTIVE_ACCOUNT_KEY]})")

# Fill subscription thread tracking (for watchdog)
_fill_sub_thread: threading.Thread | None = None

# --- FinamPy connection ---
fp: FinamPy | None = None
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
            log.info(f"State loaded: dir={state.get('dir', 0)} lots={state.get('totalLots', 0)} mode={_mode}")
        except Exception as e:
            log.error(f"Load state error: {e}")


# ========== FinamPy subscriptions ==========

def connect_finam():
    """Connect FinamPy for Trades + OrderBook + Bars + Quotes."""
    global fp
    token = os.environ.get("FINAM_API_KEY")
    if not token:
        log.error("FINAM_API_KEY not set!")
        return False

    fp = FinamPy(token)
    log.info(f"FinamPy connected. Accounts: {fp.account_ids}")

    # Subscribe to latest trades (обезличенные сделки)
    try:
        fp.on_latest_trades.subscribe(_on_latest_trades)
        t_trades = threading.Thread(
            target=fp.subscribe_latest_trades_thread,
            args=(SYMBOL,),
            daemon=True,
            name="sub-trades",
        )
        t_trades.start()
        log.info(f"Subscribed to LatestTrades: {SYMBOL}")
    except AttributeError:
        log.warning("subscribe_latest_trades_thread not available in FinamPy — check API")

    # Subscribe to order book
    try:
        fp.on_order_book.subscribe(_on_order_book)
        t_ob = threading.Thread(
            target=fp.subscribe_order_book_thread,
            args=(SYMBOL,),
            daemon=True,
            name="sub-orderbook",
        )
        t_ob.start()
        log.info(f"Subscribed to OrderBook: {SYMBOL}")
    except AttributeError:
        log.warning("subscribe_order_book_thread not available in FinamPy — check API")

    # Subscribe to bars
    try:
        from FinamPy.grpc import marketdata_service_pb2 as md
        tf_map = {
            "M1": md.TimeFrame.TIME_FRAME_M1,
            "M5": md.TimeFrame.TIME_FRAME_M5,
            "M15": md.TimeFrame.TIME_FRAME_M15,
            "M30": md.TimeFrame.TIME_FRAME_M30,
        }
        finam_tf = tf_map.get(params.timeframe, md.TimeFrame.TIME_FRAME_M5)
        fp.on_new_bar.subscribe(_on_new_bar)
        t_bars = threading.Thread(
            target=fp.subscribe_bars_thread,
            args=(SYMBOL, finam_tf),
            daemon=True,
            name="sub-bars",
        )
        t_bars.start()
        log.info(f"Subscribed to Bars: {SYMBOL} {params.timeframe}")
    except AttributeError:
        log.warning("subscribe_bars_thread not available in FinamPy — check API")
    except Exception as e:
        log.error(f"Bars subscription error: {e}")

    # Subscribe to quotes for current price
    try:
        fp.on_quote.subscribe(_on_quote)
        t_quote = threading.Thread(
            target=fp.subscribe_quote_thread,
            args=((SYMBOL,),),
            daemon=True,
            name="sub-quote",
        )
        t_quote.start()
        log.info(f"Subscribed to Quotes: {SYMBOL}")
    except AttributeError:
        log.warning("subscribe_quote_thread not available in FinamPy — check API")

    # Subscribe to own trades (order executions) for real fill prices
    try:
        global _ignore_fills_until
        _ignore_fills_until = time.time() + 120.0  # ignore batch for 2 min
        fp.on_trade.subscribe(_on_my_trade)
        for acc_id in fp.account_ids:
            fp.subscribe_orders_trades(orders=False, trades=True, account_id=acc_id)
        global _fill_sub_thread
        t_ot = threading.Thread(
            target=fp.subscribe_orders_trades_thread,
            daemon=True,
            name="sub-orders-trades",
        )
        _fill_sub_thread = t_ot
        t_ot.start()
        log.info("Subscribed to own trades (OrderTrade stream)")
    except Exception as e:
        log.warning(f"subscribe_orders_trades failed: {e}")

    return True


# ========== Fill tracking (real broker prices) ==========
_last_fill_price: float = 0.0
_last_fill_time: float = 0.0
_last_fill_qty: int = 0
_fill_cb_count: int = 0
_ignore_fills_until: float = 0.0  # ignore batch fills until this timestamp


def _on_my_trade(trade):
    """Callback from FinamPy when our order is executed. Captures REAL fill price."""
    global _last_fill_price, _last_fill_time, _last_fill_qty, _fill_cb_count
    try:
        # Ignore batch fills delivered right after subscribe
        if time.time() < _ignore_fills_until:
            return
        if str(trade.symbol) != SYMBOL:
            return
        price = float(str(trade.price.value)) if hasattr(trade.price, 'value') else float(str(trade.price))
        qty = int(float(str(trade.size.value))) if hasattr(trade.size, 'value') else int(float(str(trade.size)))
        _last_fill_price = price
        _last_fill_time = time.time()
        _last_fill_qty = qty
        _fill_cb_count += 1
        log.info(f"FILL {_fill_cb_count}: price={price} qty={qty} symbol={trade.symbol}")
    except Exception as e:
        log.error(f"on_my_trade error: {e}")


def _wait_fill_price(timeout: float = 2.0) -> float:
    """Wait for real fill price from broker. Returns 0 if timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _last_fill_time > 0 and (time.time() - _last_fill_time) < 1.0:
            return _last_fill_price
        time.sleep(0.05)
    return 0.0


def _consume_fill_price() -> float:
    """Get last fill price and reset. Returns 0 if stale."""
    if _last_fill_time > 0 and (time.time() - _last_fill_time) < 2.0:
        return _last_fill_price
    return 0.0


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
    """Callback from SubscribeBars — bar closed.
    Note: FinamPy passes (event, finam_timeframe) — accept both.
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
    """Shutdown old FinamPy and reconnect all subscriptions."""
    global fp, _last_fp_reconnect
    now = time.time()
    if now - _last_fp_reconnect < 30:
        return  # don't reconnect more than once per 30s
    _last_fp_reconnect = now
    log.warning("[WATCHDOG] Price stale — reconnecting FinamPy...")
    try:
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        time.sleep(1)
        fp = None
        ok = connect_finam()
        if ok:
            log.info("[WATCHDOG] FinamPy reconnected OK")
        else:
            log.error("[WATCHDOG] FinamPy reconnect failed")
    except Exception as e:
        log.error(f"[WATCHDOG] Reconnect error: {e}")


def _set_current_price(price: float):
    """Thread-safe price update."""
    global _current_price
    with _price_lock:
        _current_price = price


def main_loop():
    """Main strategy loop — poll price + process ticks."""
    global _mode, _fill_sub_thread, _main_loop_start_ts

    _main_loop_start_ts = time.time()
    log.info("Main loop started")
    last_save = time.time()
    last_price_sync = 0.0

    while _running:
        try:
            if _mode != "running":
                time.sleep(1)
                continue

            # Get current price
            with _price_lock:
                price = _current_price

            if price <= 0:
                # Try fallback: get from DP
                try:
                    import requests
                    r = requests.get(f"{DP_URL}/quote", params={"symbol": SYMBOL}, timeout=2)
                    data = r.json()
                    price = float(data.get("last", 0))
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

            # Periodic state save (every 30 sec)
            if time.time() - last_save > 30:
                save_state()
                last_save = time.time()

            # === BROKER PRICE SYNC: avg_price + current_price only ===
            # Does NOT touch position state (lots, dir, lot_queue)
            if time.time() - last_price_sync > 30.0:
                try:
                    sync_account = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
                    import requests
                    r = requests.get(f"{DP_URL}/position",
                                     params={"account": sync_account, "ticker": SYMBOL},
                                     timeout=5)
                    if r.status_code == 200:
                        broker_data = r.json()
                        strategy.sync_from_broker(broker_data)
                        # Reconcile lot count with broker
                        if strategy._total_lots > 0 or broker_data.get('lots', 0) > 0:
                            broker_lots = broker_data.get('lots', 0)
                            broker_avg = broker_data.get('avg_price', 0.0)
                            broker_dir = broker_data.get('dir', 0)
                            if strategy._total_lots != broker_lots:
                                strategy.reconcile_with_broker(broker_lots, broker_avg, broker_dir)
                except Exception as e:
                    log.debug(f"Price sync: {e}")
                last_price_sync = time.time()

            # === WATCHDOG: reconnect FinamPy if price stale > 60s ===
            if strategy._is_price_stale(max_age_sec=60):
                _reconnect_finampy()

            # === WATCHDOG: reconnect if CVD/LatestTrades stream dead > 120s ===
            # Price can survive via Quotes while trade stream dies silently
            if hasattr(strategy, '_last_trade_ts') and strategy._last_trade_ts > 0:
                trade_age = time.time() - strategy._last_trade_ts
                if trade_age > 120:
                    log.warning(f"[WATCHDOG] LatestTrades stream dead for {trade_age:.0f}s — reconnecting")
                    _reconnect_finampy()

            # === WATCHDOG: re-subscribe fill stream if no callbacks ===
            # Thread can be alive but blocked on dead gRPC stream.
            # If we placed orders but got 0 fill callbacks in 60s — re-subscribe.
            global _fill_cb_count
            if _fill_sub_thread is not None:
                now_wd = time.time()
                # Check if thread appears dead (no callbacks after startup or after re-sub)
                fill_stream_ok = True
                if _fill_cb_count == 0 and (now_wd - _main_loop_start_ts) > 60:
                    fill_stream_ok = False
                elif _last_fill_time > 0 and (now_wd - _last_fill_time) > 120:
                    # Had fills before but stream went silent
                    fill_stream_ok = False
                if not fill_stream_ok:
                    log.warning(f"[WATCHDOG] Fill stream silent (cb_count={_fill_cb_count}, last_fill={_last_fill_time:.0f}) — re-subscribing")
                    try:
                        global _ignore_fills_until
                        _ignore_fills_until = time.time() + 120.0
                        if _fill_sub_thread.is_alive():
                            pass
                        fp.on_trade.subscribe(_on_my_trade)
                        for acc_id in fp.account_ids:
                            fp.subscribe_orders_trades(orders=False, trades=True, account_id=acc_id)
                        _fill_sub_thread = threading.Thread(
                            target=fp.subscribe_orders_trades_thread,
                            daemon=True,
                            name=f"sub-orders-trades-{int(now_wd)}",
                        )
                        _fill_sub_thread.start()
                        _fill_cb_count = 0
                        log.info("[WATCHDOG] Fill stream re-subscribed OK")
                    except Exception as e:
                        log.error(f"[WATCHDOG] Fill re-subscribe error: {e}")

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


def _get_broker_position():
    """Fetch actual position from broker. Returns (lots, avg_price) or (0, 0.0)."""
    try:
        import requests
        sync_account = ACCOUNTS.get(ACTIVE_ACCOUNT_KEY, ACCOUNT)
        r = requests.get(f"{DP_URL}/position", params={"account": sync_account, "ticker": SYMBOL}, timeout=3)
        if r.status_code == 200:
            d = r.json()
            return abs(d.get('lots', 0)), d.get('avg_price', 0.0), d.get('dir', 0)
    except Exception:
        pass
    return 0, 0.0, 0


def _execute_action(action: dict):
    """Execute a strategy action. Broker position is the source of truth."""
    act = action.get("action")
    side_str = action.get("side", "buy")
    qty = action.get("qty", 1)
    tag = f"of_{act}"

    if PAPER_MODE:
        log.info(f"PAPER {act}: {side_str} {qty} @ {action.get('price', 0):.0f}")
        return

    if act == "close_all":
        active = orders.get_active_orders(symbol=SYMBOL)
        if active:
            for o in active:
                oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                orders.cancel(oid)
            time.sleep(0.5)

        avg_for_record = strategy._avg_price
        lots_before, _, _ = _get_broker_position()

        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag=f"of_close_{action.get('reason', '')}")
        if result:
            time.sleep(0.5)
            lots_after, broker_avg_after, _ = _get_broker_position()
            if lots_after < lots_before:
                fill_price = broker_avg_after if broker_avg_after > 0 else action.get('price', strategy._current_price)
                action["fill_price"] = fill_price
                action['avgPrice'] = avg_for_record
                log.info(f"Executed CLOSE_ALL: {side_str} {qty} @ {fill_price:.0f} reason={action.get('reason')} (broker {lots_before}→{lots_after})")
                _record_broker_trade(action, fill_price)
                strategy._reset_position()
            else:
                log.error(f"CLOSE_ALL: order may not have executed (broker lots {lots_before}→{lots_after}) — still resetting")
                action['avgPrice'] = avg_for_record
                _record_broker_trade(action, action.get('price', strategy._current_price))
                strategy._reset_position()
        else:
            log.warning(f"CLOSE_ALL: order placement failed for {side_str} {qty}")
            strategy._reset_position()

    elif act in ("entry", "average", "pyramid"):
        lots_before, _, _ = _get_broker_position()

        side_int = BUY if side_str == "buy" else SELL
        result = orders.place_market(side_int, qty, tag=tag)
        if result:
            time.sleep(0.5)
            lots_after, broker_avg_after, broker_dir = _get_broker_position()
            if lots_after != lots_before:
                # Broker confirms — order executed
                fill_price = broker_avg_after if broker_avg_after > 0 else action.get('price', 0)
                action["fill_price"] = fill_price
                log.info(f"Executed {act.upper()}: {side_str} {qty} @ {fill_price:.0f} (broker {lots_before}→{lots_after})")
                strategy.update_fill_price(fill_price, act)
            else:
                # Broker says no change — order didn't execute
                log.error(f"{act.upper()}: order NOT executed at broker (lots still {lots_before}) — rolling back {qty} lot(s)")
                strategy.rollback_pending_entry(qty)
        else:
            log.error(f"{act.upper()}: order placement FAILED for {side_str} {qty} — rolling back {qty} lot(s)")
            strategy.rollback_pending_entry(qty)

    elif act == "partial_tp":
        lots_before, _, _ = _get_broker_position()

        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag="of_partial_tp")
        if result:
            time.sleep(0.5)
            lots_after, _, _ = _get_broker_position()
            if lots_after < lots_before:
                fill_price = action.get('price', strategy._current_price)
                action["fill_price"] = fill_price
                log.info(f"Executed PARTIAL_TP: {side_str} {qty} @ {fill_price:.0f} (broker {lots_before}→{lots_after})")
                _record_broker_trade(action, fill_price)
            else:
                log.error(f"PARTIAL_TP: order NOT executed (broker lots {lots_before}→{lots_after}) — trade NOT recorded")
        else:
            log.error(f"PARTIAL_TP: order placement failed for {side_str} {qty}")


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
            # Switch active trading account
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                account_key = data.get("account", "main")
                new_account = ACCOUNTS.get(account_key)
                if new_account:
                    ACTIVE_ACCOUNT_KEY = account_key
                    orders = OrderManager(dp_url=DP_URL, account=new_account, symbol=SYMBOL)
                    log.info(f"Account switched to {account_key} ({new_account})")
                    save_state()
                    self._json(200, {"ok": True, "account": account_key, "id": new_account})
                else:
                    self._json(400, {"error": f"Unknown account: {account_key}", "available": list(ACCOUNTS.keys())})
            else:
                self._json(400, {"error": "Missing request body"})

        elif path == "/load-state":
            load_state_from_disk()
            log.info("State reloaded from disk")
            self._json(200, {"ok": True, "mode": _mode, "dir": strategy.dir(), "lots": strategy.total_lots()})

        elif path == "/params":
            # Update parameters
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                for k, v in data.items():
                    if hasattr(params, k):
                        setattr(params, k, v)
                        log.info(f"Param updated: {k} = {v}")
                # Reconstruct VWEMA if toggle changed
                if "use_vwema" in data or any(k.startswith("vwema_") for k in data):
                    strategy._init_vwema()
                # Save to config
                cfg_path = os.path.join(os.getcwd(), "of_config.json")
                with open(cfg_path, "w") as f:
                    json.dump({k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}, f, indent=2)
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


# ========== VWEMA Warmup ==========

def _warmup_vwema():
    """Fetch historical bars and warm up VWEMA filter before live trading."""
    if not strategy.vwema:
        return

    try:
        from google.protobuf.timestamp_pb2 import Timestamp
        from google.type.interval_pb2 import Interval
        import FinamPy.grpc.marketdata_service_pb2 as md_pb2

        tf_map = {
            "M1": md_pb2.TimeFrame.TIME_FRAME_M1,
            "M5": md_pb2.TimeFrame.TIME_FRAME_M5,
            "M15": md_pb2.TimeFrame.TIME_FRAME_M15,
            "M30": md_pb2.TimeFrame.TIME_FRAME_M30,
        }
        finam_tf = tf_map.get(params.timeframe, md_pb2.TimeFrame.TIME_FRAME_M1)

        # Need enough bars for slow period (default 40) + buffer
        bar_seconds = TF_SECONDS.get(params.timeframe, 60)
        bars_needed = params.vwema_slow + 20
        lookback_seconds = bars_needed * bar_seconds

        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=lookback_seconds)

        resp = fp.call_function(
            fp.marketdata_stub.Bars,
            md_pb2.BarsRequest(
                symbol=SYMBOL,
                timeframe=finam_tf,
                interval=Interval(
                    start_time=Timestamp(seconds=int(start.timestamp())),
                    end_time=Timestamp(seconds=int(now.timestamp())),
                ),
            ),
        )

        if resp and resp.bars:
            bars = list(resp.bars)
            fed = 0
            for bar in bars:
                h = _to_float(bar.high)
                l = _to_float(bar.low)
                c = _to_float(bar.close)
                v = _to_float(bar.volume)
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
    if fp:
        try:
            fp.close_channel()
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

    # Connect FinamPy
    if not connect_finam():
        log.error("Failed to connect FinamPy — exiting")
        sys.exit(1)

    # VWEMA warmup: load historical bars so filter is ready immediately
    _warmup_vwema()

    # Warmup period (let subscriptions accumulate data)
    log.info("Warmup: waiting 10 sec for data streams...")
    time.sleep(10)
    log.info(f"Warmup done. OB has data: {strategy.ob_tracker.has_data}")

    # Start main loop
    t_main = threading.Thread(target=main_loop, daemon=True, name="main-loop")
    t_main.start()

    # Start HTTP server with SO_REUSEADDR to prevent "Address already in use" on restart
    import socket
    HTTPServer.address_family = socket.AF_INET
    HTTPServer.socket_type = socket.SOCK_STREAM
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
    server = ReusableHTTPServer(("0.0.0.0", PORT), APIHandler)
    log.info(f"API listening on :{PORT}")
    log.info(f"Endpoints: GET /status | GET /health | POST /start /stop /pause /params")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        on_shutdown(None, None)
