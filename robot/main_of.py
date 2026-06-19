"""Order Flow Robot — main entry point.

Subscribes to Trades + OrderBook + Bars via FinamPy gRPC,
runs OrderFlowStrategy, sends orders via DataProvider REST.

Usage: python3 main_of.py [--paper] [--port 5080]

v2 — fixed: bar callback signature, dynamic bar_start_ts, stale price check,
      entry lock reset on manual stop, config consolidation.
"""
import sys, os
sys.path.insert(0, os.getcwd())

import argparse
import json
import logging
import signal as sig_module
import threading
import time
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# --- Config ---
SYMBOL = getattr(config, "SYMBOL", "SiU6")
TICKER = getattr(config, "TICKER", "SiU6")
ACCOUNT = getattr(config, "ACCOUNT_ID", os.environ.get("FINAM_ACCOUNT", "1225953"))
DP_URL = getattr(config, "DP_URL", "http://localhost:5060")
PORT = args.port

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
orders = OrderManager(dp_url=DP_URL, account=ACCOUNT, symbol=SYMBOL)

# --- FinamPy connection ---
fp: FinamPy | None = None
_running = True
_mode = "stopped"  # stopped, running, paused

# --- Current price (thread-safe via lock) ---
_price_lock = threading.Lock()
_current_price: float = 0.0

# --- State persistence ---
STATE_FILE = os.path.join(os.getcwd(), "of_state.json")

def save_state():
    try:
        state = strategy.get_state()
        state["mode"] = _mode
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
    token = os.environ.get("FINAM_TOKEN")
    if not token:
        log.error("FINAM_TOKEN not set!")
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
    except Exception as e:
        log.error(f"Trades callback error: {e}")


def _on_order_book(event):
    """Callback from SubscribeOrderBook."""
    try:
        rows = []
        for ob in event.order_book:
            for r in ob.rows:
                price = float(r.price.value) if hasattr(r.price, "value") else float(r.price)
                buy = float(r.buy_size.value) if hasattr(r.buy_size, "value") else (float(r.buy_size) if r.buy_size else 0)
                sell = float(r.sell_size.value) if hasattr(r.sell_size, "value") else (float(r.sell_size) if r.sell_size else 0)
                action = int(r.action)
                rows.append((price, int(buy), int(sell), action))

        if rows:
            strategy.ob_tracker.update(rows)
    except Exception as e:
        log.error(f"OB callback error: {e}")


def _on_new_bar(event):
    """Callback from SubscribeBars — bar closed.
    Note: single argument (event) — FinamPy passes only event to the callback.
    """
    try:
        # Calculate bar duration from timeframe
        bar_secs = TF_SECONDS.get(params.timeframe, 300)

        for bar in event.bars:
            o = float(bar.open.value) if hasattr(bar.open, "value") else float(bar.open)
            h = float(bar.high.value) if hasattr(bar.high, "value") else float(bar.high)
            l = float(bar.low.value) if hasattr(bar.low, "value") else float(bar.low)
            c = float(bar.close.value) if hasattr(bar.close, "value") else float(bar.close)
            v = float(bar.volume.value) if hasattr(bar.volume, "value") else float(bar.volume)

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
            strategy.on_bar_close(metrics, h, l, c)
            log.debug(f"Bar close: O={o:.0f} H={h:.0f} L={l:.0f} C={c:.0f} V={v:.0f} delta={metrics.delta} cvd={metrics.cvd:.0f}")
    except Exception as e:
        log.error(f"Bar callback error: {e}")


def _on_quote(event):
    """Callback from SubscribeQuote — update current price."""
    try:
        for q in event.quote:
            bid = float(q.bid) if q.bid else 0
            ask = float(q.ask) if q.ask else 0
            last = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)

            if last > 0:
                _set_current_price(last)
                strategy.update_price(last)
    except Exception as e:
        log.error(f"Quote callback error: {e}")


# ========== Main loop ==========

def _set_current_price(price: float):
    """Thread-safe price update."""
    global _current_price
    with _price_lock:
        _current_price = price


def main_loop():
    """Main strategy loop — poll price + process ticks."""
    global _mode

    log.info("Main loop started")
    last_save = time.time()

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

            now = datetime.now(timezone.utc)

            # Process tick
            actions = strategy.process_tick(price, now)

            # Execute actions
            for action in actions:
                _execute_action(action)

            # Periodic state save (every 30 sec)
            if time.time() - last_save > 30:
                save_state()
                last_save = time.time()

            # Tick rate: 500ms (2x per second)
            time.sleep(0.5)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(1)

    log.info("Main loop stopped")


def _execute_action(action: dict):
    """Execute a strategy action via OrderManager."""
    act = action.get("action")
    side_str = action.get("side", "buy")
    qty = action.get("qty", 1)
    tag = f"of_{act}"

    if act == "close_all":
        # First cancel all orders
        active = orders.get_active_orders(symbol=SYMBOL)
        if active:
            for o in active:
                oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                orders.cancel(oid)
            time.sleep(0.5)  # Wait for cancel

        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag=f"of_close_{action.get('reason', '')}")
        if result:
            log.info(f"Executed CLOSE_ALL: {side_str} {qty} reason={action.get('reason')}")

    elif act in ("entry", "average", "pyramid"):
        side_int = BUY if side_str == "buy" else SELL
        result = orders.place_market(side_int, qty, tag=tag)
        if result:
            log.info(f"Executed {act.upper()}: {side_str} {qty}")

    elif act == "partial_tp":
        side_int = SELL if side_str == "sell" else BUY
        result = orders.place_market(side_int, qty, tag="of_partial_tp")
        if result:
            log.info(f"Executed PARTIAL_TP: {side_str} {qty} realized={action.get('realized', 0):.0f}")


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
        with _price_lock:
            price = _current_price
        path = self.path.split("?")[0]

        if path == "/status":
            status = strategy.get_status()
            status["mode"] = _mode
            status["symbol"] = SYMBOL
            status["currentPrice"] = price
            status["params"] = {k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}
            self._json(200, status)

        elif path == "/health":
            self._json(200, {"ok": True, "symbol": SYMBOL, "mode": _mode})

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global _mode
        path = self.path.split("?")[0]

        if path == "/start":
            _mode = "running"
            strategy._force_unlock()  # Reset entry lock on manual start
            save_state()
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/stop":
            # Stop = cancel orders + close position
            _mode = "stopped"
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
                    side = SELL if strategy.direction == LONG else BUY
                    orders.place_market(side, strategy.total_lots, tag="of_stop")
                    strategy._close_all(price, "manual_stop")
            strategy._force_unlock()  # Reset entry lock after manual stop
            save_state()
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/pause":
            # Pause = cancel orders, keep position
            _mode = "paused"
            active = orders.get_active_orders(symbol=SYMBOL)
            if active:
                for o in active:
                    oid = o.get("order_id", "") if isinstance(o, dict) else str(o)
                    orders.cancel(oid)
            save_state()
            self._json(200, {"ok": True, "mode": _mode})

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
                # Save to config
                cfg_path = os.path.join(os.getcwd(), "of_config.json")
                with open(cfg_path, "w") as f:
                    json.dump({k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}, f, indent=2)
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


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

    # Warmup period (let subscriptions accumulate data)
    log.info("Warmup: waiting 10 sec for data streams...")
    time.sleep(10)
    log.info(f"Warmup done. OB has data: {strategy.ob_tracker.has_data}")

    # Start main loop
    t_main = threading.Thread(target=main_loop, daemon=True, name="main-loop")
    t_main.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", PORT), APIHandler)
    log.info(f"API listening on :{PORT}")
    log.info(f"Endpoints: GET /status | GET /health | POST /start /stop /pause /params")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        on_shutdown(None, None)
