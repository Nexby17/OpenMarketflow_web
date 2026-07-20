"""Arbitrage Robot - main entry point.

Subscribes to OrderBook + Quote for both instruments via FinamPy gRPC.
Runs ArbitrageStrategy, executes via ArbOrderManager (limit+market).

Usage: python3 main_arb.py [--paper] [--port 5090]
"""
import sys, os, json, argparse, logging, signal as sig_module, threading, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import socket

from FinamPy import FinamPy
from FinamPy.grpc.accounts_service_pb2 import GetAccountRequest

import config_arb as config
from strategy_arb import ArbitrageStrategy, ArbParams, LONG_BASIS, SHORT_BASIS, FLAT
from orders_arb import ArbOrderManager, BUY, SELL
from arb_engine import BasisCalculator, OrderBookTracker

log = logging.getLogger("robot_arb")

MSK = timezone(timedelta(hours=3))

# --- Args ---
parser = argparse.ArgumentParser()
parser.add_argument("--paper", action="store_true", default=True)
parser.add_argument("--no-paper", dest="paper", action="store_false")
parser.add_argument("--port", type=int, default=config.PORT)
args, _ = parser.parse_known_args()

# --- Logging ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_log_file = LOG_DIR / f"arb_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, mode="a", encoding="utf-8"),
    ],
)

# --- Load params ---
def load_params() -> ArbParams:
    p = ArbParams()
    # from config_arb.py
    for attr in dir(config):
        val = getattr(config, attr)
        if attr.isupper():
            key = attr.lower()
            if hasattr(p, key):
                setattr(p, key, val)
    # override from arb_config.json
    cfg_path = os.path.join(os.getcwd(), "arb_config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(p, k):
                setattr(p, k, v)
    return p

params = load_params()
PAPER_MODE = args.paper or params.capital <= 0  # default paper for safety

# Override with arg if explicitly set
if args.paper:
    PAPER_MODE = True

DP_URL = config.DP_URL
ACCOUNT = config.ACCOUNT_ID
PORT = args.port

strategy = ArbitrageStrategy(params)
orders_mgr = ArbOrderManager(dp_url=DP_URL, account=ACCOUNT)
execution_lock = threading.Lock()  # prevents concurrent entry/exit from different threads
grpc_lock = threading.Lock()  # FinamPy gRPC is NOT thread-safe

def _attach_finam_to_orders():
    global fp
    if fp:
        orders_mgr.set_finam_py(fp)
        orders_mgr.start_trade_subscription()
        log.info("Orders manager linked to FinamPy gRPC + trade subscription")

# Active account (can be changed via API)
_active_account = ACCOUNT
_accounts_cache = None
_accounts_cache_ts = 0

# Load saved account from config
cfg_path = os.path.join(os.getcwd(), "arb_config.json")
if os.path.exists(cfg_path):
    try:
        with open(cfg_path) as f:
            _cfg = json.load(f)
        _saved_acc = _cfg.get("active_account", "")
        if _saved_acc:
            _active_account = _saved_acc
            ACCOUNT = _saved_acc
            orders_mgr = ArbOrderManager(dp_url=DP_URL, account=ACCOUNT)
            _attach_finam_to_orders()
    except Exception:
        pass

# --- FinamPy ---
fp: FinamPy | None = None
_running = True
_mode = "stopped"
_last_data_ts = 0.0  # timestamp of last price/data update


def _update_data_ts():
    global _last_data_ts
    _last_data_ts = time.time()

STATE_FILE = os.path.join(os.getcwd(), "arb_state.json")


def save_state():
    try:
        state = strategy.save_state()
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
            log.info(f"State loaded: pnl={state.get('realized_pnl', 0):.0f} trades={len(state.get('trade_history', []))} mode={_mode}")
        except Exception as e:
            log.error(f"Load state error: {e}")


def save_config():
    cfg_path = os.path.join(os.getcwd(), "arb_config.json")
    data = {k: getattr(params, k) for k in dir(params) if not k.startswith("_") and not callable(getattr(params, k))}
    with open(cfg_path, "w") as f:
        json.dump(data, f, indent=2)


# ========== FinamPy subscriptions ==========

def _to_float(val) -> float:
    if val is None or val == "":
        return 0.0
    if hasattr(val, "value"):
        s = val.value
        return float(s) if s else 0.0
    return float(val)


def _quote_reconnect_loop(symbol):
    """Reconnect loop for FinamPy quote stream (one symbol per thread)."""
    while _running:
        try:
            with grpc_lock:
                fp.subscribe_quote_thread(symbol)
        except Exception as e:
            if _running:
                log.warning(f"Quote stream ended [{symbol}], reconnecting in 2s: {e}")
                time.sleep(2)


def _ob_poll_grpc_locked(sym, name):
    """Single OB poll under grpc_lock. Returns rows or raises."""
    from FinamPy.grpc.marketdata_service_pb2 import OrderBookRequest
    resp, _ = fp.marketdata_stub.OrderBook.with_call(
        request=OrderBookRequest(symbol=sym),
        timeout=5, metadata=(fp.metadata,))
    rows = []
    for r in resp.orderbook.rows:
        price = float(r.price.value) if r.price and r.price.value else 0
        buy = float(r.buy_size.value) if r.buy_size and r.buy_size.value else 0
        sell = float(r.sell_size.value) if r.sell_size and r.sell_size.value else 0
        action = int(r.action)
        rows.append((price, int(buy), int(sell), action))
    return rows


def _ob_poller():
    """Poll FinamPy unary OrderBook for both instruments (streaming doesn't work for FORTS).
    Updates OB trackers with real bid/ask every ~1 sec.
    Uses concurrent.futures to prevent gRPC hangs from blocking the poller."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
    _stale_count = 0
    while _running:
        try:
            if not fp:
                time.sleep(2)
                continue
            # Use thread pool with hard timeout to prevent gRPC hangs
            with ThreadPoolExecutor(max_workers=1) as executor:
                fa = executor.submit(lambda: _ob_poll_grpc_locked(params.symbol_a, "A"))
                fb = executor.submit(lambda: _ob_poll_grpc_locked(params.symbol_b, "B"))
                try:
                    rows_a = fa.result(timeout=8)
                    err_a = None
                except Exception as e:
                    rows_a = None
                    err_a = e
                try:
                    rows_b = fb.result(timeout=8)
                    err_b = None
                except Exception as e:
                    rows_b = None
                    err_b = e
            # Process results
            for name, rows, err, ob_tracker in [("A", rows_a, err_a, strategy.ob_a),
                                                 ("B", rows_b, err_b, strategy.ob_b)]:
                if err:
                    err_str = str(err)
                    if "UNAUTHENTICATED" in err_str or "expired" in err_str.lower():
                        log.warning(f"OB poll {name}: token expired, reconnecting...")
                        _reconnect_finam()
                    elif "TimeoutError" in type(err).__name__ or "FuturesTimeout" in type(err).__name__:
                        log.warning(f"OB poll {name}: gRPC timeout (stale)")
                        _stale_count += 1
                        if _stale_count >= 3:
                            log.warning(f"OB poll stale {_stale_count}x, forcing reconnect...")
                            _reconnect_finam()
                            _stale_count = 0
                    else:
                        log.warning(f"OB poll {name} error: {err}")
                elif rows:
                    ob_tracker.update(rows)
                    _push_market()
                    _update_data_ts()
                    _stale_count = 0
        except Exception as e:
            log.warning(f"OB poll outer error: {e}")
        time.sleep(1)
    log.warning("OB poller thread exited!")


_reconnect_count = 0
_last_reconnect_ts = 0
_last_ob_poll_ts = 0

def _reconnect_finam():
    """Reconnect FinamPy when JWT refresh token expires.
    Must NOT hold grpc_lock — FinamPy() constructor may block on gRPC handshake."""
    global fp, _reconnect_count, _last_reconnect_ts
    import time as _time
    now = _time.time()
    if now - _last_reconnect_ts < 30:
        log.debug(f"Reconnect throttled (last was {now - _last_reconnect_ts:.0f}s ago)")
        return False
    _last_reconnect_ts = now
    _reconnect_count += 1
    try:
        token = os.environ.get("FINAM_API_KEY")
        log.info(f"FinamPy reconnecting (#{_reconnect_count})...")
        fp = FinamPy(token)
        with grpc_lock:
            _attach_finam_to_orders()
        log.info(f"FinamPy reconnected (#{_reconnect_count}). Accounts: {fp.account_ids}")
        return True
    except Exception as e:
        log.error(f"FinamPy reconnect failed (#{_reconnect_count}): {e}")
        return False


def connect_finam():
    global fp
    token = os.environ.get("FINAM_API_KEY")
    if not token:
        log.error("FINAM_API_KEY not set!")
        return False

    fp = FinamPy(token)
    log.info(f"FinamPy connected. Accounts: {fp.account_ids}")
    _attach_finam_to_orders()

    sym_a = params.symbol_a
    sym_b = params.symbol_b

    # OrderBook — use unary polling (streaming doesn't work for FORTS)
    t_ob = threading.Thread(target=_ob_poller, daemon=True, name="ob-poll")
    t_ob.start()
    log.info(f"OrderBook polling started: {sym_a}, {sym_b} (unary, 1s interval)")

    # Quote subscription — DISABLED for FORTS pairs.
    # FinamPy quote streams don't work for FORTS (constantly reconnect every 2s).
    # They also block gRPC channel and prevent OB unary polling from working.
    # For futures: OB polling + MOEX ISS fallback provides all needed data.
    log.info(f"Quote streams disabled (FORTS pair — using OB polling + MOEX fallback)")

    return True


# ========== Callbacks ==========

def _on_order_book(event):
    """Route orderbook updates to correct tracker based on symbol."""
    try:
        _update_data_ts()
        for ob in event.order_book:
            symbol = getattr(ob, 'symbol', '') or ''
            rows = []
            for r in ob.rows:
                price = _to_float(r.price)
                buy = _to_float(r.buy_size)
                sell = _to_float(r.sell_size)
                action = int(r.action)
                rows.append((price, int(buy), int(sell), action))
            if rows:
                # Route by symbol or price level
                if symbol and (params.ticker_a in symbol or symbol in params.ticker_a or params.symbol_a in symbol):
                    strategy.ob_a.update(rows)
                elif symbol and (params.ticker_b in symbol or symbol in params.ticker_b or params.symbol_b in symbol):
                    strategy.ob_b.update(rows)
                else:
                    # Fallback: route by price magnitude
                    avg_price = sum(r[0] for r in rows) / len(rows) if rows else 0
                    if avg_price < 1000:
                        strategy.ob_a.update(rows)
                    else:
                        strategy.ob_b.update(rows)
                # Push market bid/ask to basis calculator
                _push_market()
    except Exception as e:
        log.error(f"OB callback error: {e}")


def _on_quote(event):
    """Route quote updates — use q.last (real last trade price), not mid-price.
    For FORTS futures, bid/ask may be 0 — rely on MOX poller instead."""
    try:
        _update_data_ts()
        for q in event.quote:
            symbol = q.secsi.secsym if hasattr(q, 'secsi') else ""
            bid = _to_float(q.bid)
            ask = _to_float(q.ask)
            last_q = _to_float(q.last)  # real last trade price from proto
            # Use last_q as primary, fallback to (bid+ask)/2
            last = last_q if last_q > 0 else ((bid + ask) / 2 if bid > 0 and ask > 0 else 0)
            if last <= 0:
                continue
            # Route ONLY by symbol — NO fallback by price magnitude
            matched = False
            if symbol and (params.ticker_a in symbol or symbol in params.ticker_a):
                strategy.basis_calc.update_price_a(last)
                if bid > 0 or ask > 0:
                    strategy.basis_calc.update_market(bid_a=bid, ask_a=ask)
                matched = True
            elif symbol and (params.ticker_b in symbol or symbol in params.ticker_b):
                strategy.basis_calc.update_price_b(last)
                if bid > 0 or ask > 0:
                    strategy.basis_calc.update_market(bid_b=bid, ask_b=ask)
                matched = True
            if not matched and symbol:
                log.debug(f"Quote unmatched symbol: {symbol} last={last:.2f}")
    except Exception as e:
        log.warning(f"Quote callback error: {e}")


# ========== MOEX ISS Fallback (for stocks not streamed by FinamPy) ==========

def _moex_poller():
    """Poll MOEX ISS API for prices + L2 orderbook (bid/ask).
    Stocks: TQBR board. Futures: FORTS engine.
    """
    import urllib.request
    last_log_a = 0.0
    last_log_b = 0.0
    while _running:
        try:
            # Always poll (even when stopped) so UI shows live data
            import json as _json

            # --- Instrument A (stock or futures) ---
            ticker_a = params.ticker_a
            is_stock_a = not any(c.isdigit() for c in ticker_a)
            if is_stock_a:  # stock
                url_a = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker_a}.json"
                         f"?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,BID,OFFER,BIDDEPTH,OFFERDEPTH")
            else:  # futures (e.g. BRQ6)
                url_a = (f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{ticker_a}/marketdata.json"
                         f"?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,BID,OFFER,BIDDEPTH,OFFERDEPTH")
            try:
                req = urllib.request.Request(url_a)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = _json.loads(resp.read())
                    md = data.get("marketdata", {})
                    if md.get("data") and len(md["data"]) > 0:
                        rowdict = dict(zip(md["columns"], md["data"][0]))
                        last = float(rowdict.get("LAST") or 0)
                        bid = float(rowdict.get("BID") or 0)
                        ask = float(rowdict.get("OFFER") or 0)
                        bid_vol = int(rowdict.get("BIDDEPTH") or 0)
                        ask_vol = int(rowdict.get("OFFERDEPTH") or 0)
                        if last > 0:
                            _update_data_ts()
                            strategy.basis_calc.update_price_a(last)
                            # Only update OB when MOEX has real bid/ask (stocks).
                            # For futures MOEX returns bid=0/ask=0 — don't overwrite
                            # FinamPy OB polling which has real L2 data.
                            if bid > 0 and ask > 0:
                                rows = []
                                rows.append((bid, max(bid_vol, 1), 0, 1))
                                rows.append((ask, 0, max(ask_vol, 1), 1))
                                strategy.ob_a.update(rows)
                                _push_market()
                            else:
                                _push_market()
                            if last != last_log_a:
                                last_log_a = last
                                log.info(f"MOEX {ticker_a}: {last:.2f} bid={bid:.2f} ask={ask:.2f}")
            except Exception as e:
                log.warning(f"MOEX {ticker_a} poll error: {e}")

            # --- Futures (instrument B) ---
            ticker_b = params.ticker_b
            if any(c.isdigit() for c in ticker_b):  # futures
                url_b = (f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{ticker_b}/marketdata.json"
                         f"?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,BID,OFFER,BIDDEPTH,OFFERDEPTH")
                try:
                    req = urllib.request.Request(url_b)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = _json.loads(resp.read())
                        md = data.get("marketdata", {})
                        if md.get("data") and len(md["data"]) > 0:
                            rowdict = dict(zip(md["columns"], md["data"][0]))
                            last = float(rowdict.get("LAST") or 0)
                            bid = float(rowdict.get("BID") or 0)
                            ask = float(rowdict.get("OFFER") or 0)
                            bid_vol = int(rowdict.get("BIDDEPTH") or 0)
                            ask_vol = int(rowdict.get("OFFERDEPTH") or 0)
                            if last > 0:
                                strategy.basis_calc.update_price_b(last)
                                # Only update OB when MOEX has real bid/ask (stocks).
                                # For futures MOEX returns bid=0/ask=0 — don't overwrite
                                # FinamPy OB polling which has real L2 data.
                                if bid > 0 and ask > 0:
                                    rows = []
                                    rows.append((bid, max(bid_vol, 1), 0, 1))
                                    rows.append((ask, 0, max(ask_vol, 1), 1))
                                    strategy.ob_b.update(rows)
                                    _push_market()
                                else:
                                    _push_market()
                                if last != last_log_b:
                                    last_log_b = last
                                    log.info(f"MOEX {ticker_b}: {last:.2f} bid={bid:.2f} ask={ask:.2f}")
                except Exception as e:
                    log.warning(f"MOEX {ticker_b} poll error: {e}")

        except Exception as e:
            log.warning(f"MOEX poll outer error: {e}")
        time.sleep(2)



# ========== Main loop ==========

def main_loop():
    global _mode
    log.info("Main loop started")
    last_save = time.time()
    last_basis_push = 0.0

    while _running:
        try:
            # Update basis tracking (push to history) - always, even when stopped
            now = time.time()
            if now - last_basis_push > 1.0:  # push every 1 sec
                strategy.basis_calc._push_spread()  # push directly, don't read zscore
                last_basis_push = now

            if _mode != "running":
                # Even when stopped/paused, attempt to close remaining layers
                if strategy.layers:
                    log.info(f"Cleanup: {len(strategy.layers)} open layers in mode={_mode}, attempting close...")
                    with execution_lock:
                        _execute_exit({"action": "exit_all", "reason": "cleanup", "pnl": 0, "layer_id": None}, force_market=True)
                    if not strategy.layers:
                        save_state()
                    time.sleep(1)
                else:
                    time.sleep(1)
                continue

            # Release expired locks
            strategy.update_lock()

            # Check session (skip in paper mode)
            if not PAPER_MODE:
                if not strategy.is_in_session():
                    time.sleep(1)
                    continue
            else:
                # Paper mode: skip session check but still check lock
                pass

            # Debug: log Z every 10 sec
            if now - last_basis_push < 1.5:  # right after push
                pass  # could add debug log here

            # Check entry
            z_now = strategy.basis_calc.zscore_no_push
            entry_signal = strategy.check_entry()
            if entry_signal and not strategy.entry_active:
                log.info(f"⚠️ CHECK_ENTRY returned signal: {entry_signal}")
                strategy.set_entry_active()
                try:
                    with execution_lock:
                        _execute_entry(entry_signal)
                finally:
                    strategy.clear_entry_active()
                    strategy._set_lock(2.0)  # cooldown after any entry attempt
            elif z_now > params.entry_z - 0.2:
                log.info(f"Z={z_now:.2f} (threshold={params.entry_z}) - no signal (dup_check={strategy._is_duplicate_entry('short_basis', z_now) if not strategy.entry_lock else 'LOCKED'})")

            # Check exit (per-layer or all)
            exit_signal = strategy.check_exit()
            if exit_signal:
                with execution_lock:
                    _execute_exit(exit_signal)
                time.sleep(0.5)  # brief pause after exit
                continue  # re-check entry on next tick

            # Periodic save
            if time.time() - last_save > 30:
                save_state()
                last_save = time.time()

            time.sleep(0.5)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(1)

    log.info("Main loop stopped")


# ========== Telegram (stub - not configured) ==========
def _send_telegram(msg: str):
    try:
        pass  # TODO: integrate with Telegram bot when needed
    except Exception:
        pass


# ========== Entry/Exit execution ==========

_push_market_count = 0
def _push_market():
    """Push current OB bid/ask to basis_calc for market spread calculation."""
    global _push_market_count
    ba = strategy.ob_a.best_bid
    aa = strategy.ob_a.best_ask
    bb = strategy.ob_b.best_bid
    ab = strategy.ob_b.best_ask
    strategy.basis_calc.update_market(ba, aa, bb, ab)
    _push_market_count += 1
    if _push_market_count <= 20 or _push_market_count % 100 == 0:
        log.info(f"_push_market #{_push_market_count}: ba={ba:.3f} aa={aa:.3f} bb={bb:.3f} ab={ab:.3f} -> bid_a={strategy.basis_calc.bid_a:.3f}")


def _market_fill_price(side: str) -> float:
    """DEPRECATED: kept for backward compat. Use basis_calc bid/ask directly."""
    return 0.0


def _execute_entry(signal: dict):
    """Execute entry: LIMIT on both legs (by OB), MARKET fallback."""
    side = signal["side"]
    z = signal["z"]

    # Prices from OB
    ob_a = strategy.ob_a
    ob_b = strategy.ob_b
    price_a = strategy.basis_calc.price_a
    price_b = strategy.basis_calc.price_b

    if side == "long_basis":
        side_a = SELL
        side_b = BUY
        # SELL A → limit at best_bid (passive)
        # BUY B → limit at best_ask (passive)
        limit_a = ob_a.best_bid if ob_a.best_bid > 0 else price_a
        limit_b = ob_b.best_ask if ob_b.best_ask > 0 else price_b
    else:
        side_a = BUY
        side_b = SELL
        # BUY A → limit at best_ask (passive)
        # SELL B → limit at best_bid (passive)
        limit_a = ob_a.best_ask if ob_a.best_ask > 0 else price_a
        limit_b = ob_b.best_bid if ob_b.best_bid > 0 else price_b

    if price_a <= 0 or price_b <= 0:
        log.warning(f"No price data — skipping entry (A={price_a:.2f} B={price_b:.2f})")
        strategy._set_lock(5.0)
        return

    if limit_a <= 0 or limit_b <= 0:
        log.warning(f"No OB data — falling back to MARKET")
        result = orders_mgr.execute_both_limit(
            symbol_a=params.symbol_a,
            symbol_b=params.symbol_b,
            side_a=side_a,
            side_b=side_b,
            lots_a=params.lots_a,
            lots_b=params.lots_b,
            limit_price_a=price_a,
            limit_price_b=price_b,
            paper=PAPER_MODE,
            est_price_a=price_a,
            est_price_b=price_b,
            force_market=True,
        )
    else:
        spread = limit_b - limit_a if side == "short_basis" else limit_a - limit_b
        log.info(f"ENTRY {side.upper()} | Z={z:.2f} | "
                 f"A {side_a} {params.lots_a} @ {limit_a:.2f} (ob={ob_a.best_bid}/{ob_a.best_ask}) | "
                 f"B {side_b} {params.lots_b} @ {limit_b:.2f} (ob={ob_b.best_bid}/{ob_b.best_ask}) | "
                 f"ob spread={spread:.2f}")

        result = orders_mgr.execute_both_limit(
            symbol_a=params.symbol_a,
            symbol_b=params.symbol_b,
            side_a=side_a,
            side_b=side_b,
            lots_a=params.lots_a,
            lots_b=params.lots_b,
            limit_price_a=limit_a,
            limit_price_b=limit_b,
            paper=PAPER_MODE,
            est_price_a=price_a,
            est_price_b=price_b,
            limit_timeout=3.0,
        )

    if result["success"]:
        fill_a = result["leg_a"]
        fill_b = result["leg_b"]
        side_int = LONG_BASIS if side == "long_basis" else SHORT_BASIS
        strategy.open_layer(
            side=side_int,
            price_a=fill_a.price,
            price_b=fill_b.price,
            lots_a=fill_a.quantity,
            lots_b=fill_b.quantity,
            z=z,
        )
        log.info(f"LAYER OPEN {side.upper()} #{strategy.layers[-1].layer_id} | "
                 f"A={fill_a.price:.2f} ×{fill_a.quantity} | "
                 f"B={fill_b.price:.2f} ×{fill_b.quantity} | "
                 f"Z={z:.2f} layers={len(strategy.layers)}")
    else:
        log.warning(f"Entry failed: {result['error']}")
        strategy._set_lock(10.0)


def _execute_exit(signal: dict, force_market: bool = False):
    """Execute exit for a specific layer (or all layers for risk stop).
    force_market=True for manual_stop/cleanup - use market orders."""
    layer_id = signal.get("layer_id")

    # Exit ALL layers (risk stop / manual stop)
    if layer_id is None and signal.get("action") == "exit_all":
        if not strategy.layers:
            return
        log.info(f"EXIT ALL {len(strategy.layers)} layers - reason={signal['reason']}")
        if PAPER_MODE:
            pa = strategy.basis_calc.price_a
            pb = strategy.basis_calc.price_b
            if pa > 0 and pb > 0:
                strategy.close_all_layers(pa, pb, signal["reason"])
        else:
            # Close each layer sequentially
            for layer in list(strategy.layers):
                _execute_single_exit(layer, signal["reason"], force_market=force_market)
        return

    # Exit single layer
    layer = None
    if layer_id is not None:
        layer = strategy.get_layer(layer_id)
    else:
        # Fallback: exit first profitable layer
        for l in strategy.layers:
            if strategy._layer_unrealized_pnl(l) > 0:
                layer = l
                break
        if layer is None and strategy.layers:
            layer = strategy.layers[0]

    if not layer:
        return

    _execute_single_exit(layer, signal["reason"], force_market=force_market)


def _execute_single_exit(layer, reason: str, force_market: bool = True):
    """Execute exit for one layer: LIMIT (normal) or MARKET (force_market=emergency)."""
    price_a = strategy.basis_calc.price_a
    price_b = strategy.basis_calc.price_b
    ob_a = strategy.ob_a
    ob_b = strategy.ob_b

    if layer.side == LONG_BASIS:
        side_a = BUY
        side_b = SELL
        # BUY A → limit at best_ask, SELL B → limit at best_bid
        limit_a = ob_a.best_ask if ob_a.best_ask > 0 else price_a
        limit_b = ob_b.best_bid if ob_b.best_bid > 0 else price_b
    else:
        side_a = SELL
        side_b = BUY
        # SELL A → limit at best_bid, BUY B → limit at best_ask
        limit_a = ob_a.best_bid if ob_a.best_bid > 0 else price_a
        limit_b = ob_b.best_ask if ob_b.best_ask > 0 else price_b

    if price_a <= 0 or price_b <= 0:
        log.error(f"Cannot exit layer #{layer.layer_id} — no price data")
        return

    if force_market or limit_a <= 0 or limit_b <= 0:
        log.info(f"EXIT #{layer.layer_id} MARKET reason={reason} | A {side_a} {layer.lots_a} @ {price_a:.2f} | B {side_b} {layer.lots_b} @ {price_b:.2f}")
        result = orders_mgr.execute_both_limit(
            symbol_a=params.symbol_a,
            symbol_b=params.symbol_b,
            side_a=side_a,
            side_b=side_b,
            lots_a=layer.lots_a,
            lots_b=layer.lots_b,
            limit_price_a=price_a,
            limit_price_b=price_b,
            paper=PAPER_MODE,
            est_price_a=price_a,
            est_price_b=price_b,
            force_market=True,
        )
    else:
        log.info(f"EXIT #{layer.layer_id} LIMIT reason={reason} | A {side_a} {layer.lots_a} @ {limit_a:.2f} | B {side_b} {layer.lots_b} @ {limit_b:.2f}")
        result = orders_mgr.execute_both_limit(
            symbol_a=params.symbol_a,
            symbol_b=params.symbol_b,
            side_a=side_a,
            side_b=side_b,
            lots_a=layer.lots_a,
            lots_b=layer.lots_b,
            limit_price_a=limit_a,
            limit_price_b=limit_b,
            paper=PAPER_MODE,
            est_price_a=price_a,
            est_price_b=price_b,
            limit_timeout=3.0,
        )

    if result["success"]:
        # Use actual fill prices from broker, not quote stream
        fill_a_price = result["leg_a"].price
        fill_b_price = result["leg_b"].price
        strategy.close_layer(layer, fill_a_price, fill_b_price, reason)
        # Log slippage vs quote
        slip_a = fill_a_price - price_a
        slip_b = fill_b_price - price_b
        if abs(slip_a) > 0.01 or abs(slip_b) > 0.01:
            log.info(f"Exit slippage: A quote={price_a:.2f} fill={fill_a_price:.2f} ({slip_a:+.2f}) B quote={price_b:.2f} fill={fill_b_price:.2f} ({slip_b:+.2f})")
        log.info(f"Exit OK layer #{layer.layer_id} reason={reason}")
    else:
        log.error(f"Exit failed layer #{layer.layer_id}: {result['error']} — retry next tick")


# ========== API ==========

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

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
        global _accounts_cache, _accounts_cache_ts
        from urllib.parse import urlparse, parse_qs
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/status":
                status = strategy.get_status()
                status["mode"] = _mode
                status["paper"] = PAPER_MODE
                status["lastDataTs"] = _last_data_ts
                status["account"] = _active_account
                status["symbolA"] = params.symbol_a
                status["symbolB"] = params.symbol_b
                status["tickerA"] = params.ticker_a
                status["tickerB"] = params.ticker_b
                status["params"] = {k: getattr(params, k) for k in dir(params)
                                    if not k.startswith("_") and not callable(getattr(params, k))}
                self._json(200, status)

            elif path == "/health":
                self._json(200, {"ok": True, "mode": _mode, "paper": PAPER_MODE})

            elif path == "/accounts":
                try:
                    now = time.time()
                    if now - _accounts_cache_ts < 60 and _accounts_cache:
                        self._json(200, {**_accounts_cache, "active": _active_account})
                        return
                    account_ids = list(fp.account_ids) if hasattr(fp, 'account_ids') else ["1225953"]
                    accounts = []
                    for aid in account_ids:
                        try:
                            resp, _ = fp.accounts_stub.GetAccount.with_call(
                                request=GetAccountRequest(account_id=aid),
                                timeout=10, metadata=(fp.metadata,))
                            eq_val = resp.equity.value if hasattr(resp.equity, 'value') else str(resp.equity or '')
                            if not eq_val:
                                continue
                            equity = float(eq_val)
                            unreal = float(resp.unrealized_profit.value) if hasattr(resp.unrealized_profit, 'value') else float(resp.unrealized_profit or 0)
                            free_cash = 0.0
                            margin = 0.0
                            if resp.HasField('portfolio_forts'):
                                free_cash = float(resp.portfolio_forts.available_cash.value) if hasattr(resp.portfolio_forts.available_cash, 'value') else 0.0
                                margin = float(resp.portfolio_forts.money_reserved.value) if hasattr(resp.portfolio_forts.money_reserved, 'value') else 0.0
                            elif resp.HasField('portfolio_mc'):
                                free_cash = float(resp.portfolio_mc.available_cash.value) if hasattr(resp.portfolio_mc.available_cash, 'value') else 0.0
                                margin = float(resp.portfolio_mc.initial_margin.value) if hasattr(resp.portfolio_mc.initial_margin, 'value') else 0.0
                            positions = []
                            daily_pnl = 0.0
                            for p in resp.positions:
                                qty = int(float(p.quantity.value)) if hasattr(p.quantity, 'value') else 0
                                dpnl = float(p.daily_pnl.value) if hasattr(p.daily_pnl, 'value') else 0
                                daily_pnl += dpnl
                                if qty != 0:
                                    positions.append({"symbol": p.symbol, "qty": qty, "dailyPnl": round(dpnl, 2)})
                            accounts.append({
                                "id": aid, "name": f"{aid}",
                                "balance": round(equity, 2), "free": round(free_cash, 2),
                                "margin": round(margin, 2), "go": round(margin, 2),
                                "pnlToday": round(daily_pnl, 2), "pnlTotal": round(unreal, 2),
                                "positions": positions
                            })
                        except Exception as e:
                            log.debug(f"Account {aid}: {e}")
                    _accounts_cache = {"accounts": accounts}
                    _accounts_cache_ts = now
                    # Always add EDP account if not present
                    edp_ids = [a["id"] for a in accounts]
                    # Always ensure Main account is in the list
                    if "1225953" not in edp_ids:
                        _accounts_cache["accounts"].insert(0, {
                            "id": "1225953", "name": "1225953 (Main)",
                            "balance": None, "free": None, "margin": None, "go": None,
                            "pnlToday": None, "pnlTotal": None, "positions": []
                        })
                    self._json(200, {**_accounts_cache, "active": _active_account})
                except Exception as e:
                    log.warning(f"Failed to get account info: {e}")
                    if _accounts_cache:
                        self._json(200, {**_accounts_cache, "active": _active_account})
                    else:
                        self._json(200, {"accounts": [
                            {"id": "1225953", "name": "1225953 (Main)"},
                        ], "active": _active_account})

            elif path == "/instruments":
                spots = ["GAZP", "SBER", "LKOH", "ROSN", "TATN", "GMKN", "ALRS", "VTBR", "MTSS", "NVTK"]
                futures = ["GZM6", "GZU6", "GZH6", "SRM6", "SRU6", "SRH6", "LKM6", "LKU6", "RNM6", "RNU6",
                           "TTM6", "TTU6", "MXM6", "MXU6", "SiM6", "SiU6", "SiH6", "RIM6", "RIU6",
                           "GDM6", "GDU6", "BRK6", "BRU6"]
                self._json(200, {"spots": spots, "futures": futures})

            elif path == "/params":
                data = {k: getattr(params, k) for k in dir(params)
                        if not k.startswith("_") and not callable(getattr(params, k))}
                self._json(200, data)

            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            log.error(f"GET error: {e}", exc_info=True)
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def do_POST(self):
        global _mode, _active_account
        path = self.path.split("?")[0]

        if path == "/start":
            _mode = "running"
            strategy.force_unlock()
            save_state()
            log.info("Mode → RUNNING")
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/stop":
            _mode = "stopped"
            # Close all layers if any (with lock to prevent race with main loop)
            if strategy.layers:
                if PAPER_MODE:
                    pa = strategy.basis_calc.price_a
                    pb = strategy.basis_calc.price_b
                    if pa > 0 and pb > 0:
                        strategy.close_all_layers(pa, pb, "manual_stop")
                else:
                    acquired = execution_lock.acquire(timeout=10)
                    try:
                        _execute_exit({"action": "exit_all", "reason": "manual_stop", "pnl": 0, "layer_id": None}, force_market=True)
                    finally:
                        if acquired:
                            execution_lock.release()
            save_state()
            log.info("Mode → STOPPED")
            self._json(200, {"ok": True, "mode": _mode, "paper": PAPER_MODE})

        elif path == "/pause":
            _mode = "paused"
            save_state()
            log.info("Mode → PAUSED")
            self._json(200, {"ok": True, "mode": _mode})

        elif path == "/account":
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                log.warning(f"POST /account received: {data}")
                new_acc = data.get("account", "")
                # BR Calendar: both legs are FORTS futures - allow 1225953 and EDP
                if new_acc != "1225953":
                    log.warning(f"Account change to {new_acc} BLOCKED")
                    self._json(200, {"ok": True, "account": _active_account, "warning": f"Account locked to {_active_account}"})
                    return
                if new_acc:
                    _active_account = new_acc
                    orders_mgr._account = new_acc
                    # Persist to config
                    try:
                        cfg_path = os.path.join(os.getcwd(), "arb_config.json")
                        with open(cfg_path) as f:
                            cfg = json.load(f)
                        cfg["active_account"] = new_acc
                        with open(cfg_path, "w") as f:
                            json.dump(cfg, f, indent=2)
                    except Exception:
                        pass
                    log.info(f"Account changed → {_active_account}")
            self._json(200, {"ok": True, "account": _active_account})

        elif path == "/params":
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                for k, v in data.items():
                    if hasattr(params, k):
                        old_val = getattr(params, k)
                        setattr(params, k, v)
                        log.info(f"Param updated: {k} = {v} (was {old_val})")
                # Update strategy references
                strategy.p = params
                strategy.basis_calc.lookback = params.lookback
                strategy.basis_calc.hedge_ratio = params.hedge_ratio
                strategy.basis_calc.rate = params.risk_free_rate
                strategy.basis_calc.expiration_date = params.expiration_date
                strategy.basis_calc.contract_size = params.contract_size
                save_config()
            self._json(200, {"ok": True})

        elif path == "/copy":
            # Clone config for a new robot instance
            import shutil
            length = int(self.headers.get("Content-Length", 0))
            new_ticker_a = "GAZP"
            new_ticker_b = "GZM6"
            if length > 0:
                body = self.rfile.read(length)
                data = json.loads(body)
                new_ticker_a = data.get("ticker_a", "GAZP")
                new_ticker_b = data.get("ticker_b", "GZM6")

            base = os.path.dirname(os.path.abspath(__file__))
            new_dir = os.path.join(base, f"arb_{new_ticker_a.lower()}_{new_ticker_b.lower()}")
            os.makedirs(new_dir, exist_ok=True)

            # Copy robot files
            for f in ["main_arb.py", "strategy_arb.py", "orders_arb.py", "arb_engine.py", "config_arb.py"]:
                src = os.path.join(base, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(new_dir, f))

            # Create new config
            new_config = {}
            cfg_path = os.path.join(base, "arb_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path) as cf:
                    new_config = json.load(cf)
            new_config["ticker_a"] = new_ticker_a
            new_config["symbol_a"] = new_ticker_a + "@RTSX"
            new_config["ticker_b"] = new_ticker_b
            new_config["symbol_b"] = new_ticker_b + "@RTSX"
            with open(os.path.join(new_dir, "arb_config.json"), "w") as cf:
                json.dump(new_config, cf, indent=2)

            log.info(f"Robot copied: {new_dir} ({new_ticker_a}/{new_ticker_b})")
            self._json(200, {"ok": True, "path": new_dir, "dir": os.path.basename(new_dir),
                              "ticker_a": new_ticker_a, "ticker_b": new_ticker_b})

        elif path == "/reset-stats":
            strategy.realized_pnl = 0.0
            strategy.peak_pnl = 0.0
            strategy.max_dd = 0.0
            strategy.trade_history = []
            save_state()
            log.info("Statistics reset: realizedPnl=0, trades=0")
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


# ========== Shutdown ==========

def on_shutdown(signum, frame):
    global _running, _mode
    log.info(f"Signal {signum} - shutting down...")
    _running = False
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
    log.info(f"=== Arbitrage Robot v1 ===")
    log.info(f"Pair: {params.ticker_a} / {params.ticker_b}")
    log.info(f"Account: {ACCOUNT} | Port: {PORT} | Paper: {PAPER_MODE}")
    log.info(f"Params: entry_z={params.entry_z} lookback={params.lookback} "
             f"lots_a={params.lots_a} lots_b={params.lots_b} "
             f"hedge_ratio={params.hedge_ratio}")
    log.info(f"Min profit: {params.min_profit_type}={params.min_profit_value}")
    log.info(f"Risk: {params.risk_type}={params.risk_value}")

    load_state_from_disk()

    if not connect_finam():
        log.error("Failed to connect FinamPy - running without L2 (paper simulation only)")

    log.info("Warmup: waiting 10 sec for data streams...")
    time.sleep(10)
    log.info(f"Warmup done. OB_A: {strategy.ob_a.has_data} OB_B: {strategy.ob_b.has_data}")

    # Cancel any pending orders from previous run
    if not PAPER_MODE:
        log.info("Cancelling leftover orders from previous run...")
        try:
            import requests as _req
            r = _req.post(f"{DP_URL}/api/orders/cancel-all", timeout=10)
            res = r.json()
            log.info(f"Cancel leftover: {res}")
        except Exception as e:
            log.warning(f"Cancel leftover failed: {e}")

    # Start MOEX ISS fallback poller for stock prices
    t_moex = threading.Thread(target=_moex_poller, daemon=True, name="moex-poller")
    t_moex.start()
    log.info("MOEX ISS poller started (fallback for stock prices)")

    t_main = threading.Thread(target=main_loop, daemon=True, name="main-loop")
    t_main.start()

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    server = ReusableHTTPServer(("0.0.0.0", PORT), APIHandler)
    log.info(f"API listening on :{PORT}")
    log.info(f"Endpoints: GET /status /health /params | POST /start /stop /pause /params")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        on_shutdown(None, None)
