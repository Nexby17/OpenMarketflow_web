"""Arbitrage Robot — main entry point.

Subscribes to OrderBook + Quote for both instruments via FinamPy gRPC.
Runs ArbitrageStrategy, executes via ArbOrderManager (limit+market).

Usage: python3 main_arb.py [--paper] [--port 5090]
"""
import sys, os, json, argparse, logging, signal as sig_module, threading, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import socket

from finam_compat import FinamPyCompat as FinamPy
from finam_trade_api.proto.grpc.tradeapi.v1.accounts.accounts_service_pb2 import GetAccountRequest

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

# MOEX stock lot sizes (shares per lot)
_STOCK_LOTS = {
    'GAZP': 10, 'SBER': 10, 'LKOH': 10, 'ROSN': 10, 'TATN': 10,
    'GMKN': 10, 'ALRS': 10, 'VTBR': 10, 'MTSS': 10, 'NVTK': 10,
    'MOEX': 10, 'SNGS': 10, 'CHMF': 10, 'NLMK': 10,
    'POLY': 10, 'YNDX': 1, 'FIVE': 10, 'PLZL': 10,
}

def _lots_to_shares(symbol: str, lots: int) -> int:
    """Convert lots to shares for MOEX stocks. No-op for futures."""
    ticker = symbol.split('@')[0]
    ls = _STOCK_LOTS.get(ticker, 10)
    return lots * ls if ls > 1 else lots

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

def _attach_finam_to_orders():
    global fp
    if fp:
        orders_mgr.set_finam_py(fp)
        orders_mgr.start_trade_subscription()
        orders_mgr._on_unauthenticated = _reconnect_finam
        orders_mgr.cancel_pending_orders(params.symbol_a, params.symbol_b)
        log.info("Orders manager linked to FinamPy gRPC + trade subscription + reconnect")

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

_reconnect_count = 0
_last_reconnect_ts = 0


def _reconnect_finam():
    """Reconnect FinamPy when JWT refresh token expires.
    Called when order placement fails with UNAUTHENTICATED."""
    global fp, _reconnect_count, _last_reconnect_ts
    now = time.time()
    if now - _last_reconnect_ts < 30:
        log.debug(f"Reconnect throttled (last was {now - _last_reconnect_ts:.0f}s ago)")
        return False
    _last_reconnect_ts = now
    _reconnect_count += 1
    try:
        token = os.environ.get("FINAM_API_KEY")
        log.info(f"FinamPy reconnecting (#{_reconnect_count})...")
        fp = FinamPy(token)
        fp.connect()
        _attach_finam_to_orders()
        log.info(f"FinamPy reconnected (#{_reconnect_count}). Accounts: {fp.account_ids}")
        return True
    except Exception as e:
        log.error(f"FinamPy reconnect failed (#{_reconnect_count}): {e}")
        return False
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


def _quote_reconnect_loop(symbols):
    """Reconnect loop for FinamPy quote stream (it drops after snapshot)."""
    while _running:
        try:
            fp.subscribe_quote_thread(symbols)
        except Exception as e:
            if _running:
                if 'UNAUTHENTICATED' in str(e):
                    log.warning("Quote stream: JWT expired, reconnecting FinamPy...")
                    _reconnect_finam()
                else:
                    log.debug(f"Quote stream ended, reconnecting in 2s: {e}")
                    time.sleep(2)


def _ob_poll_grpc_locked(sym, name):
    """Single OB poll under grpc_lock. Returns rows or raises.
    For stocks, converts RTSX→MISX for OB data (Finam gRPC OB requires correct MIC)."""
    from finam_trade_api.proto.grpc.tradeapi.v1.marketdata.marketdata_service_pb2 import OrderBookRequest
    # Stocks need MISX for OB data, futures use RTSX
    ob_sym = sym.replace("@RTSX", "@MISX") if any(sym.startswith(p) for p in ['GAZP','SBER','LKOH','ROSN','NVTK','GMKN','PLZL','YNDX','MTSS','MGNT','CHMF','NLMK','ALRS','RUAL','POLY','FIVE','RTKM','TATN','VTBR','SNGS','AFLT','AFKS','ASTR','PHOR','HYDR','IRAO','FEES','SMLT','TRNFP']) else sym
    resp, _ = fp.marketdata_stub.OrderBook.with_call(
        request=OrderBookRequest(symbol=ob_sym),
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
    """Poll FinamPy unary OrderBook for both instruments.
    Updates OB trackers with real bid/ask every ~1 sec.
    Detects token expiry and triggers reconnect."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
    _stale_count = 0
    while _running:
        try:
            if not fp:
                time.sleep(2)
                continue
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
                        # Rate limit (429) or other error — backoff
                        if "Too Many" in err_str or "429" in err_str:
                            log.warning(f"OB poll {name}: rate limited, backing off...")
                            time.sleep(30)  # wait for rate limit reset
                        else:
                            log.warning(f"OB poll {name} error: {err}")
                elif rows:
                    ob_tracker.update(rows)
                    _stale_count = 0
        except Exception as e:
            log.warning(f"OB poll outer error: {e}")
        time.sleep(1)
    log.warning("OB poller thread exited!")


def connect_finam():
    global fp
    token = os.environ.get("FINAM_API_KEY")
    if not token:
        log.error("FINAM_API_KEY not set!")
        return False

    fp = FinamPy(token)
    fp.connect()
    log.info(f"FinamPy connected. Accounts: {fp.account_ids}")
    _attach_finam_to_orders()

    sym_a = params.symbol_a
    sym_b = params.symbol_b

    # OB polling (replaces streaming — detects token expiry)
    t = threading.Thread(target=_ob_poller, daemon=True, name="ob")
    t.start()
    log.info(f"OB poller started: {sym_a}, {sym_b}")

    # Quote subscription
    try:
        fp.on_quote.subscribe(_on_quote)
        t = threading.Thread(target=_quote_reconnect_loop,
                             args=((sym_a, sym_b),), daemon=True, name="quote")
        t.start()
        log.info(f"Subscribed Quotes: {sym_a}, {sym_b}")
    except AttributeError:
        log.warning("subscribe_quote_thread not available")
    except Exception as e:
        log.error(f"Quote subscription error: {e}")

    return True


# ========== Callbacks ==========

def _on_order_book(event):
    """Route orderbook updates to correct tracker based on symbol."""
    try:
        _update_data_ts()
        for ob in event.order_book:
            symbol = ob.secsi.secsym if hasattr(ob, 'secsi') else ""
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
    except Exception as e:
        log.error(f"OB callback error: {e}")


def _on_quote(event):
    """Route quote updates to correct price tracker."""
    try:
        _update_data_ts()
        for q in event.quote:
            bid = _to_float(q.bid)
            ask = _to_float(q.ask)
            last = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)
            if last <= 0:
                continue
            # Route by symbol or price magnitude
            symbol = q.secsi.secsym if hasattr(q, 'secsi') else ""
            if symbol and (params.ticker_a in symbol or symbol in params.ticker_a):
                strategy.basis_calc.update_price_a(last)
            elif symbol and (params.ticker_b in symbol or symbol in params.ticker_b):
                strategy.basis_calc.update_price_b(last)
            else:
                # Fallback: GAZP ~98, GZM6 ~980
                if last < 1000:
                    strategy.basis_calc.update_price_a(last)
                else:
                    strategy.basis_calc.update_price_b(last)
    except Exception as e:
        log.error(f"Quote callback error: {e}")


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

            # --- Stock (instrument A) ---
            ticker_a = params.ticker_a
            if not any(c.isdigit() for c in ticker_a):  # stock
                url_a = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker_a}.json"
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
                                # Update L2 orderbook tracker
                                rows = []
                                if bid > 0:
                                    rows.append((bid, max(bid_vol, 1), 0, 1))
                                if ask > 0:
                                    rows.append((ask, 0, max(ask_vol, 1), 1))
                                if rows:
                                    strategy.ob_a.update(rows)
                                if last != last_log_a:
                                    last_log_a = last
                                    log.debug(f"MOEX {ticker_a}: {last:.2f} bid={bid:.2f} ask={ask:.2f}")
                except Exception as e:
                    log.debug(f"MOEX stock poll error: {e}")

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
                                # Use LAST for orderbook if bid/ask missing
                                eff_bid = bid if bid > 0 else last
                                eff_ask = ask if ask > 0 else last
                                rows = []
                                rows.append((eff_bid, max(bid_vol, 1), 0, 1))
                                rows.append((eff_ask, 0, max(ask_vol, 1), 1))
                                strategy.ob_b.update(rows)
                                if last != last_log_b:
                                    last_log_b = last
                                    log.debug(f"MOEX {ticker_b}: {last:.2f} bid={bid:.2f} ask={ask:.2f}")
                except Exception as e:
                    log.debug(f"MOEX futures poll error: {e}")

        except Exception as e:
            log.debug(f"MOEX poll error: {e}")
        time.sleep(2)



# ========== Main loop ==========

def main_loop():
    global _mode, _last_dev_push
    log.info("Main loop started")
    last_save = time.time()
    last_basis_push = 0.0
    _last_dev_push = 0.0

    while _running:
        try:
            # Update basis tracking (push to history) — always, even when stopped
            now = time.time()
            if now - last_basis_push > 1.0:  # push raw spread every 1 sec (spread mode stats only)
                strategy.basis_calc._push_spread()  # push directly, don't read zscore
                last_basis_push = now
            # Fix #1: deviation-from-fair is pushed at SLOW cadence (default 60s)
            # inside _push_deviation(); 1-second pushes must not feed deviation stats.
            if now - _last_dev_push > max(1.0, params.dev_push_interval):
                strategy.basis_calc._push_deviation(force=True)
                _last_dev_push = now

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
                log.info(f"Z={z_now:.2f} (threshold={params.entry_z}) — no signal (dup_check={strategy._is_duplicate_entry('short_basis', z_now) if not strategy.entry_lock else 'LOCKED'})")

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


# ========== Telegram (stub — not configured) ==========
def _send_telegram(msg: str):
    try:
        pass  # TODO: integrate with Telegram bot when needed
    except Exception:
        pass


# ========== Entry/Exit execution ==========

def _market_fill_price(side: str) -> float:
    """Compute realistic market fill price from L2 orderbook + slippage.
    BUY  → best_ask + slippage  (pay more)
    SELL → best_bid - slippage  (receive less)
    Fallback to mid/last if no L2.
    """
    slippage = 0.0
    ref_price = 0.0
    if side == BUY:
        ref_price = strategy.ob_b.best_ask
    else:
        ref_price = strategy.ob_b.best_bid

    if ref_price <= 0:
        # No L2 for B — fallback to basis_calc price (mid/last)
        ref_price = strategy.basis_calc.price_b

    if ref_price > 0:
        slippage = ref_price * params.slippage_bps / 10000.0
        if side == BUY:
            ref_price += slippage
        else:
            ref_price -= slippage

    return ref_price


def _execute_entry(signal: dict):
    """Execute entry: limit on A + market on B."""
    side = signal["side"]
    z = signal["z"]

    if side == "long_basis":
        # LONG_BASIS: buy B (cheap), sell A (expensive)
        # Leg A: SELL limit @ best_bid (passive)
        # Leg B: BUY market @ best_ask + slippage (aggressive)
        side_a = SELL
        side_b = BUY
        limit_price = strategy.ob_a.best_bid
    else:
        # SHORT_BASIS: sell B (expensive), buy A (cheap)
        # Leg A: BUY limit @ best_ask (passive)
        # Leg B: SELL market @ best_bid - slippage (aggressive)
        side_a = BUY
        side_b = SELL
        limit_price = strategy.ob_a.best_ask

    if limit_price <= 0:
        log.warning(f"Entry signal but no L2 data for {params.symbol_a} — skipping")
        strategy._set_lock(5.0)
        return

    # Compute market fill price for leg B from L2 + slippage
    market_price_b = _market_fill_price(side_b)
    if market_price_b <= 0:
        log.warning(f"No price data for {params.symbol_b} — skipping entry")
        strategy._set_lock(5.0)
        return

    # Check liquidity
    if PAPER_MODE:
        total_vol = strategy.ob_a.total_volume + strategy.ob_b.total_volume
        if total_vol < 1:
            log.warning(f"No market data — skipping entry")
            strategy._set_lock(5.0)
            return
    else:
        total_vol = strategy.ob_a.total_volume + strategy.ob_b.total_volume
        if total_vol < 1:
            log.warning(f"Low liquidity: total_vol={total_vol} — skipping entry")
            strategy._set_lock(10.0)
            return

    log.info(f"ENTRY SIGNAL {side} | Z={z:.2f} dev_ann={signal.get('dev_ann', 0):+.2f}% basis={signal['basis']:.2f} | "
             f"A={signal['price_a']:.2f} B={signal['price_b']:.2f} | "
             f"limit {side_a} {params.lots_a} @ {limit_price:.2f} | "
             f"market {side_b} {params.lots_b} @ {market_price_b:.2f}")

    result = orders_mgr.execute_entry(
        symbol_a=params.symbol_a,
        symbol_b=params.symbol_b,
        side_a=side_a,
        side_b=side_b,
        lots_a=params.lots_a,
        lots_b=params.lots_b,
        limit_price_a=limit_price,
        timeout=params.leg_a_timeout,
        min_fill_ratio=params.min_fill_ratio,
        paper=PAPER_MODE,
        market_price_b=market_price_b,
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
            dev_ann=signal.get("dev_ann", 0.0),
        )
        log.info(
            f"LAYER OPEN {side.upper()} #{strategy.layers[-1].layer_id} | "
            f"A={fill_a.price:.2f} ×{fill_a.quantity} | "
            f"B={fill_b.price:.2f} ×{fill_b.quantity} | "
            f"basis={strategy.basis_calc.basis:.2f} Z={z:.2f} | "
            f"layers={len(strategy.layers)} used_capital={strategy._used_capital():,.0f}"
        )
        _send_telegram(
            f"📈 LAYER #{strategy.layers[-1].layer_id} {side.upper()}\n"
            f"A: {fill_a.price:.2f} ×{fill_a.quantity}\n"
            f"B: {fill_b.price:.2f} ×{fill_b.quantity}\n"
            f"Z={z:.2f} layers={len(strategy.layers)}"
        )
    else:
        err = result.get('error', '')
        log.warning(f"Entry failed: {err}")
        # Auto-reconnect if UNAUTHENTICATED (JWT expired)
        if 'UNAUTHENTICATED' in err or 'Invalid access key' in err:
            log.warning("Detected expired JWT — reconnecting FinamPy...")
            if _reconnect_finam():
                log.info("FinamPy reconnected — next entry attempt will use fresh token")
        # No cooldown — retry on next tick if Z still valid


def _execute_exit(signal: dict, force_market: bool = False):
    """Execute exit for a specific layer (or all layers for risk stop).
    force_market=True for manual_stop/cleanup — use market orders."""
    layer_id = signal.get("layer_id")

    # Exit ALL layers (risk stop / manual stop)
    if layer_id is None and signal.get("action") == "exit_all":
        if not strategy.layers:
            return
        log.info(f"EXIT ALL {len(strategy.layers)} layers — reason={signal['reason']}")
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


def _execute_single_exit(layer, reason: str, force_market: bool = False):
    """Execute exit for one layer: reverse legs.
    force_market=True for manual_stop/cleanup — use market orders to guarantee fill."""
    if layer.side == LONG_BASIS:
        side_a = BUY
        side_b = SELL
    else:
        side_a = SELL
        side_b = BUY

    if force_market:
        # Market exit: guaranteed fill, no timeout
        log.info(f"EXIT layer #{layer.layer_id} reason={reason} | MARKET {side_a} {layer.lots_a} + {side_b} {layer.lots_b}")
        est_a = strategy.basis_calc.price_a or (strategy.ob_a.best_bid + strategy.ob_a.best_ask) / 2 or 0
        est_b = strategy.basis_calc.price_b or (strategy.ob_b.best_bid + strategy.ob_b.best_ask) / 2 or 0
        if est_a <= 0 or est_b <= 0:
            log.error(f"Cannot market-exit layer #{layer.layer_id} — no price data")
            return
        res_a = orders_mgr._place_market(params.symbol_a, side_a, _lots_to_shares(params.symbol_a, layer.lots_a), tag=f"arb_exit_{layer.layer_id}")
        res_b = orders_mgr._place_market(params.symbol_b, side_b, layer.lots_b, tag=f"arb_exit_{layer.layer_id}")
        # Get real fill prices from broker
        real_a = orders_mgr._get_real_fill_price(res_a.get('order_id',''), est_a) if res_a else est_a
        real_b = orders_mgr._get_real_fill_price(res_b.get('order_id',''), est_b) if res_b else est_b
        if res_a and res_b:
            strategy.close_layer(layer, real_a, real_b, reason)
            log.info(f"Market exit OK layer #{layer.layer_id} A@{real_a:.2f} B@{real_b:.2f}")
        else:
            log.error(f"Market exit failed layer #{layer.layer_id}: A={'OK' if res_a else 'FAIL'} B={'OK' if res_b else 'FAIL'}")
        return

    # Limit exit: normal profit-taking exit
    limit_price = strategy.ob_a.best_ask
    if limit_price <= 0:
        limit_price = strategy.basis_calc.price_a
        if limit_price <= 0:
            log.error(f"Cannot exit layer #{layer.layer_id} — no price data")
            return

    market_price_b = _market_fill_price(side_b)
    if market_price_b <= 0:
        log.error(f"Cannot exit layer #{layer.layer_id} — no B price data")
        return

    log.info(f"EXIT layer #{layer.layer_id} reason={reason} | "
             f"limit {side_a} {layer.lots_a} @ {limit_price:.2f} | "
             f"market {side_b} {layer.lots_b} @ {market_price_b:.2f}")

    result = orders_mgr.execute_exit(
        symbol_a=params.symbol_a,
        symbol_b=params.symbol_b,
        side_a=side_a,
        side_b=side_b,
        lots_a=layer.lots_a,
        lots_b=layer.lots_b,
        limit_price_a=limit_price,
        timeout=params.leg_a_timeout,
        min_fill_ratio=params.min_fill_ratio,
        paper=PAPER_MODE,
        market_price_b=market_price_b,
    )

    if result["success"]:
        fill_a = result["leg_a"]
        fill_b = result["leg_b"]
        strategy.close_layer(layer, fill_a.price, fill_b.price, reason)
    else:
        log.error(f"Exit failed layer #{layer.layer_id}: {result['error']} — retrying next tick")


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
                    account_ids = [config.ACCOUNT_ID]
                    accounts = []
                    for aid in account_ids:
                        try:
                            resp, _ = fp.accounts_stub.GetAccount.with_call(
                                request=GetAccountRequest(account_id=aid),
                                timeout=10, metadata=(fp.metadata,))
                            eq_val = resp.equity.value if hasattr(resp.equity, 'value') else str(resp.equity or '')
                            equity = float(eq_val) if eq_val else 0.0
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
                            log.warning(f"Account {aid}: {e}")
                            accounts.append({
                                "id": aid, "name": f"{aid} (EDP)",
                                "balance": 0.0, "free": 0.0,
                                "margin": 0.0, "go": 0.0,
                                "pnlToday": 0.0, "pnlTotal": 0.0,
                                "positions": []
                            })
                    _accounts_cache = {"accounts": accounts}
                    _accounts_cache_ts = now
                    self._json(200, {**_accounts_cache, "active": _active_account})
                except Exception as e:
                    log.warning(f"Failed to get account info: {e}")
                    if _accounts_cache:
                        self._json(200, {**_accounts_cache, "active": _active_account})
                    else:
                        self._json(200, {"accounts": [
                            {"id": "1225953", "name": "1225953"},
                            {"id": "1225953-EDP", "name": "КлФ-2049688 (EDP)"},
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
                # PROTECT: only allow configured account for arb robot
                if new_acc != config.ACCOUNT_ID:
                    log.warning(f"Account change to {new_acc} BLOCKED — locked to {config.ACCOUNT_ID}")
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
                # Fair-value deviation mode wiring (Fix #1/#2)
                strategy.basis_calc._history_mode = "dev"
                strategy.basis_calc.dev_lookback = params.dev_lookback
                strategy.basis_calc.dev_push_interval = params.dev_push_interval
                strategy.basis_calc.set_dividends(getattr(params, "dividends", []) or [])
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
    log.info(f"Signal {signum} — shutting down...")
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
    log.info(f"Params: entry_mode={params.entry_mode} dev_ann=[{params.dev_ann_low}, {params.dev_ann_high}]% "
             f"entry_z={params.entry_z} lookback={params.lookback} "
             f"lots_a={params.lots_a} lots_b={params.lots_b} "
             f"hedge_ratio={params.hedge_ratio}")
    log.info(f"Min profit: {params.min_profit_type}={params.min_profit_value}")
    log.info(f"Risk: {params.risk_type}={params.risk_value}")

    load_state_from_disk()

    if not connect_finam():
        log.error("Failed to connect FinamPy — running without L2 (paper simulation only)")

    log.info("Warmup: waiting 10 sec for data streams...")
    time.sleep(10)
    log.info(f"Warmup done. OB_A: {strategy.ob_a.has_data} OB_B: {strategy.ob_b.has_data}")

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
